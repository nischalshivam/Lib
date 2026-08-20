"""Pull many good stills out of a located scene.

A twenty-minute video at a 50-60% image split needs well over a hundred
stills. Nobody is going to list a hundred images in a script, and no search
engine is going to return a hundred correct ones. But the footage already
contains them: once a scene has been located, the stills are simply frames of
it, and the only real questions are which frames and how many.

"Which" matters more than it sounds. Sampling every N seconds gives motion
blur, half-blinks, black frames between cuts, and — worst — five frames of the
same static shot that look identical on the timeline. So candidates are
scored and de-duplicated:

  * **sharpness**  a variance-of-Laplacian, which rejects motion blur
  * **exposure**   drops black frames from fades and blown-out flashes
  * **similarity** a perceptual hash, so two near-identical frames never both
    survive

All of it runs on 32x32 grayscale frames streamed out of ffmpeg in a single
call — 240 frames in half a second — so scoring a whole scene costs less than
seeking to it would.
"""
from __future__ import annotations

import math
import os
import subprocess
from dataclasses import dataclass

from .probe import ProbeError, require_ffmpeg

SCAN_FPS = 2.0          # candidates per second of scene
GRID = 32               # analysis resolution; small on purpose
DARK = 12.0             # mean below this is a black frame
BLOWN = 246.0           # mean above this is a white flash
MIN_GAP_S = 0.8         # two stills this close are the same moment
PHASH_DISTANCE = 6      # hamming distance below this looks like the same frame
COLOUR_DISTANCE = 8.0   # ...and it must also agree on colour to be a duplicate
# A perceptual hash is a comparison of cell brightness against the frame's own
# average, so on a dark, evenly lit frame the bits are close to noise and two
# near-identical frames can differ by more than PHASH_DISTANCE. Requiring both
# tests then lets duplicates through exactly where they are most likely — a
# dim interior, which is most of this kind of footage. Agreement on colour
# this close is conclusive on its own.
SAME_COLOUR = 2.5


@dataclass
class Candidate:
    time: float
    sharpness: float
    brightness: float
    phash: int
    colour: tuple = ()      # mean RGB of a 4x4 grid, 48 numbers

    @property
    def usable(self) -> bool:
        return DARK < self.brightness < BLOWN

    @property
    def score(self) -> float:
        if not self.usable:
            return 0.0
        # mid-grey frames carry more visible detail than very dark or very
        # bright ones, so exposure nudges the ranking rather than deciding it
        centred = 1.0 - abs(self.brightness - 128.0) / 128.0
        return self.sharpness * (0.75 + 0.25 * centred)


def _laplacian_variance(gray: bytes, size: int = GRID) -> float:
    """Sharpness. A blurred frame has little second-derivative energy."""
    total = 0.0
    sq = 0.0
    n = 0
    for y in range(1, size - 1):
        row = y * size
        up = row - size
        dn = row + size
        for x in range(1, size - 1):
            lap = (gray[up + x] + gray[dn + x] + gray[row + x - 1]
                   + gray[row + x + 1] - 4 * gray[row + x])
            total += lap
            sq += lap * lap
            n += 1
    if not n:
        return 0.0
    mean = total / n
    return max(0.0, sq / n - mean * mean)


def _phash(gray: bytes, size: int = GRID) -> int:
    """64-bit perceptual hash from an 8x8 reduction of the frame."""
    step = size // 8
    cells = []
    for by in range(8):
        for bx in range(8):
            acc = 0
            for y in range(by * step, (by + 1) * step):
                base = y * size
                for x in range(bx * step, (bx + 1) * step):
                    acc += gray[base + x]
            cells.append(acc / (step * step))
    avg = sum(cells) / 64.0
    bits = 0
    for i, c in enumerate(cells):
        if c > avg:
            bits |= 1 << i
    return bits


def _hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _colour_signature(rgb: bytes, size: int = GRID) -> tuple:
    """Mean RGB per cell of a 4x4 grid.

    A luminance hash cannot tell a red room from a green one of the same
    brightness — on flat frames it barely differs at all. Two frames only
    count as duplicates when they agree on colour as well.
    """
    step = size // 4
    out = []
    for by in range(4):
        for bx in range(4):
            r = g = b = 0
            for y in range(by * step, (by + 1) * step):
                base = y * size * 3
                for x in range(bx * step, (bx + 1) * step):
                    i = base + x * 3
                    r += rgb[i]; g += rgb[i + 1]; b += rgb[i + 2]
            n = step * step
            out += [r / n, g / n, b / n]
    return tuple(out)


def _colour_distance(a: tuple, b: tuple) -> float:
    if not a or not b or len(a) != len(b):
        return 999.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def scan(path: str, start: float = 0.0, end: float | None = None,
         fps: float = SCAN_FPS, timeout=900) -> list[Candidate]:
    """Score every candidate frame in a range, in one ffmpeg pass."""
    start = max(0.0, start)
    cmd = [require_ffmpeg(), "-v", "error", "-ss", f"{start:.3f}"]
    if end and end > start:
        cmd += ["-t", f"{end - start:.3f}"]
    cmd += ["-i", path, "-vf", f"fps={fps},scale={GRID}:{GRID},format=rgb24",
            "-f", "rawvideo", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProbeError(f"frame scan failed: {exc}") from exc
    if r.returncode != 0 and not r.stdout:
        raise ProbeError(f"frame scan failed: {(r.stderr or b'')[-300:]!r}")

    frame_bytes = GRID * GRID * 3
    out = []
    for i in range(len(r.stdout) // frame_bytes):
        rgb = r.stdout[i * frame_bytes:(i + 1) * frame_bytes]
        # luma, so sharpness and the hash see the picture the way an eye does
        gray = bytes(min(255, (54 * rgb[j] + 183 * rgb[j + 1]
                               + 19 * rgb[j + 2]) >> 8)
                     for j in range(0, frame_bytes, 3))
        out.append(Candidate(
            time=start + i / fps,
            sharpness=_laplacian_variance(gray),
            brightness=sum(gray) / (GRID * GRID),
            phash=_phash(gray),
            colour=_colour_signature(rgb)))
    return out


def pick(candidates: list[Candidate], n: int,
         min_gap: float = MIN_GAP_S,
         phash_distance: int = PHASH_DISTANCE,
         colour_distance: float = COLOUR_DISTANCE,
         exclude: list | None = None) -> list[Candidate]:
    """The best `n` frames that are neither too close nor too alike.

    Greedy by quality: take the sharpest usable frame, then the next one that
    is far enough away in time AND different enough to look like another shot.
    De-duplication is what stops a static scene yielding five identical stills.

    `exclude` carries the frames already used elsewhere in the same video, as
    (phash, colour) pairs. Without it every scene de-duplicates only against
    itself, and a face that appears in six scenes is picked six times — which
    on a twenty-minute timeline reads as the same still recycled, the exact
    thing the image half of this pipeline exists to avoid.
    """
    ranked = sorted((c for c in candidates if c.usable),
                    key=lambda c: -c.score)
    chosen: list[Candidate] = []
    taken = list(exclude or [])
    for c in ranked:
        if len(chosen) >= n:
            break
        if any(abs(c.time - k.time) < min_gap for k in chosen):
            continue
        if any((_hamming(c.phash, h) < phash_distance
                and _colour_distance(c.colour, col) < colour_distance)
               or _colour_distance(c.colour, col) < SAME_COLOUR
               for h, col in taken):
            continue
        chosen.append(c)
        taken.append((c.phash, c.colour))
    return sorted(chosen, key=lambda c: c.time)


def extract_stills(path: str, out_dir: str, n: int, start: float = 0.0,
                   end: float | None = None, prefix: str = "image",
                   width: int = 1920, log=lambda *a: None) -> list[str]:
    """Write the best `n` distinct stills of a range. Returns the file paths."""
    from .cutter import extract_frame
    cands = scan(path, start, end)
    best = pick(cands, n)
    if not best:
        return []
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for i, c in enumerate(best, 1):
        out = os.path.join(out_dir, f"{prefix}_{i:02d}.jpg")
        try:
            extract_frame(path, c.time, out, width=width)
            written.append(out)
        except ProbeError as exc:
            log(f"      still at {c.time:.1f}s failed: {exc}")
    log(f"      {len(written)} still(s) from {len(cands)} candidate(s)")
    return written


def describe(candidates: list[Candidate], chosen: list[Candidate]) -> str:
    usable = [c for c in candidates if c.usable]
    dark = sum(1 for c in candidates if c.brightness <= DARK)
    blown = sum(1 for c in candidates if c.brightness >= BLOWN)
    return (f"{len(candidates)} candidates, {len(usable)} usable "
            f"({dark} black, {blown} blown), {len(chosen)} distinct kept")
