"""Turn a timestamp into a usable clip — or a still frame.

Two things stand between "the line is at 14:32.5" and a clip you can put on a
timeline:

  1. **Shot boundaries.** A cut that starts mid-shot and runs across a camera
     change looks like a mistake. We find the real shot boundaries around the
     line and keep the clip inside one shot wherever possible.
  2. **Seek accuracy.** Stream copy can only start on a keyframe, so it may
     begin up to a GOP early. Re-encoding is frame-accurate and, for a 3-5 s
     clip, fast. Accurate is the default; fast is available when it matters.

Shot detection uses ffmpeg's own `scene` score, so there is no extra
dependency. PySceneDetect is a drop-in upgrade later if finer control is
wanted.

The still-frame path matters as much as the clip path: pulling the frame
straight out of the scene you already located beats searching the web for an
image of that scene — it is the right shot by construction, at source
resolution, with no watermark.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

from .probe import ProbeError, probe, require_ffmpeg

# ffmpeg's scene score for the *same kind* of cut varies enormously — measured
# 0.03 to 0.74 across identical hard cuts, because the score reflects how
# different the two frames happen to look, not whether an edit occurred.
# 0.15 is the usual working value; it must be re-validated on real footage,
# where camera motion (absent from synthetic tests) creates false positives.
SCENE_THRESHOLD = 0.15
PAD_BEFORE = 1.5             # seconds of air before the line
PAD_AFTER = 1.0
MIN_CLIP = 2.0
MAX_CLIP = 8.0

_RE_PTS = re.compile(r"pts_time:([\d.]+)")


@dataclass
class Cut:
    path: str                # source file
    start: float             # seconds
    end: float
    out: str = ""
    snapped_start: bool = False
    snapped_end: bool = False
    crossed_shots: int = 0
    note: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


# ---------------------------------------------------------------------------
# shot boundaries
# ---------------------------------------------------------------------------

def detect_shots(path: str, start: float = 0.0, end: float | None = None,
                 threshold: float = SCENE_THRESHOLD, timeout=600) -> list[float]:
    """Times (seconds, absolute) where the picture changes, within [start, end].

    Scanning a whole film is wasteful when we only care about the ten seconds
    around a line, so the window is seeked to first.
    """
    start = max(0.0, start)
    cmd = [require_ffmpeg(), "-hide_banner", "-nostats", "-ss", f"{start:.3f}"]
    if end and end > start:
        cmd += ["-t", f"{end - start:.3f}"]
    cmd += ["-i", path, "-map", "0:v:0",
            "-vf", f"select='gt(scene,{threshold})',showinfo",
            "-an", "-f", "null", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProbeError(f"shot detection failed: {exc}") from exc

    times = []
    for m in _RE_PTS.finditer(out.stderr or ""):
        t = float(m.group(1)) + start        # showinfo pts are window-relative
        if not times or t - times[-1] > 0.20:   # ignore duplicate reports
            times.append(t)
    return times


def shot_bounds(boundaries: list[float], t: float,
                window_start: float, window_end: float) -> tuple[float, float]:
    """The shot containing `t`, clipped to the scanned window."""
    lo = window_start
    hi = window_end
    for b in boundaries:
        if b <= t:
            lo = max(lo, b)
        else:
            hi = min(hi, b)
            break
    return lo, hi


def snap(boundaries: list[float], start: float, end: float,
         window_start: float, window_end: float,
         min_clip=MIN_CLIP, max_clip=MAX_CLIP) -> Cut:
    """Pull [start, end] inside a single shot where that is possible.

    Preference order:
      1. the whole request already sits in one shot  -> unchanged
      2. the shot is long enough                     -> clamp to the shot
      3. the shot is too short                       -> keep the request,
         report how many boundaries it crosses so the caller can decide
    """
    mid = (start + end) / 2
    lo, hi = shot_bounds(boundaries, mid, window_start, window_end)
    crossed = sum(1 for b in boundaries if start < b < end)

    cut = Cut(path="", start=start, end=end, crossed_shots=crossed)
    if crossed == 0:
        cut.note = "already inside one shot"
        return cut

    if (hi - lo) >= min_clip:
        new_start = max(start, lo + 0.06)      # a hair off the cut itself
        new_end = min(end, hi - 0.06)
        if new_end - new_start < min_clip:     # grow within the shot
            want = min(max_clip, max(min_clip, end - start))
            new_start = max(lo + 0.06, min(new_start, hi - want - 0.06))
            new_end = min(hi - 0.06, new_start + want)
        if new_end - new_start >= min_clip:
            cut.start, cut.end = new_start, new_end
            cut.snapped_start = new_start != start
            cut.snapped_end = new_end != end
            cut.crossed_shots = 0
            cut.note = f"snapped into shot [{lo:.2f}, {hi:.2f}]"
            return cut

    cut.note = (f"shot is only {hi - lo:.1f}s — clip still crosses "
                f"{crossed} boundary/ies")
    return cut


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------

def cut_clip(path: str, start: float, end: float, out: str,
             mode: str = "accurate", height: int | None = None,
             with_audio: bool = False, crf: int = 18,
             timeout=900) -> str:
    """Write [start, end] of `path` to `out`.

    mode="accurate" re-encodes and lands on the exact frame (default).
    mode="fast" stream-copies — instant, but starts at the nearest keyframe.
    Narration replaces the original audio in this pipeline, so audio is
    dropped unless asked for.
    """
    if end <= start:
        raise ValueError(f"empty range {start}..{end}")
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    dur = end - start
    cmd = [require_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
           "-ss", f"{start:.3f}", "-i", path, "-t", f"{dur:.3f}", "-map", "0:v:0"]
    if with_audio:
        cmd += ["-map", "0:a:0?"]
    else:
        cmd += ["-an"]

    if mode == "fast":
        cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
    else:
        vf = f"scale=-2:{height}" if height else None
        if vf:
            cmd += ["-vf", vf]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                "-pix_fmt", "yuv420p"]
        if with_audio:
            cmd += ["-c:a", "aac", "-b:a", "160k"]
    cmd += [out]

    r = subprocess.run(cmd, capture_output=True, text=True,
                       errors="replace", timeout=timeout)
    if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
        raise ProbeError(f"cut failed: {(r.stderr or '')[-400:]}")
    return out


def extract_frame(path: str, t: float, out: str, width: int | None = None,
                  quality: int = 2, timeout=180) -> str:
    """Write a single still from `t`. This is the images half of the pipeline."""
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    cmd = [require_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
           "-ss", f"{max(0.0, t):.3f}", "-i", path, "-frames:v", "1"]
    if width:
        cmd += ["-vf", f"scale={width}:-2"]
    if out.lower().endswith((".jpg", ".jpeg")):
        cmd += ["-q:v", str(quality)]
    cmd += [out]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       errors="replace", timeout=timeout)
    if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
        raise ProbeError(f"frame extraction failed: {(r.stderr or '')[-400:]}")
    return out


def average_rgb(path: str, t: float, timeout=120) -> tuple:
    """Mean colour of the frame at `t`, with no image library involved.

    Used to verify a cut landed on the intended shot; also a cheap way to
    reject a black or blank frame before it reaches the timeline.
    """
    cmd = [require_ffmpeg(), "-hide_banner", "-loglevel", "error",
           "-ss", f"{max(0.0, t):.3f}", "-i", path, "-frames:v", "1",
           "-vf", "scale=1:1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if r.returncode != 0 or len(r.stdout) < 3:
        raise ProbeError("could not sample frame colour")
    return tuple(r.stdout[:3])


# ---------------------------------------------------------------------------
# the whole job: a Hit from search -> a clip on disk
# ---------------------------------------------------------------------------

def clip_for_hit(hit, out: str, target_seconds: float = 4.0,
                 mode: str = "accurate", height: int | None = None,
                 scan_window: float = 12.0, cover_full_line: bool = False,
                 with_audio: bool = False, log=lambda *a: None) -> Cut:
    """Take a search Hit and produce a shot-aware clip.

    The dialogue tells us *where*; shot detection tells us *how much*.

    `target_seconds` is honoured even when the matched dialogue runs longer.
    The clip is silent b-roll under the user's own narration, so its length is
    an editing decision, not something the original line gets to dictate — a
    5.5 s quote must not silently become an 8 s clip. Pass
    `cover_full_line=True` when the whole line really is wanted.

    Audio is dropped by default because narration replaces it downstream.
    Pass `with_audio=True` when a human is going to watch the clip: hearing
    the line is the only way to confirm the timing, and a silent clip of the
    right scene proves only that the scene was found.
    """
    line_start = hit.start_ms / 1000.0
    line_end = hit.end_ms / 1000.0

    start = max(0.0, line_start - PAD_BEFORE)
    if cover_full_line:
        end = line_end + PAD_AFTER
        end = min(end, start + MAX_CLIP)
    else:
        end = start + max(MIN_CLIP, min(target_seconds, MAX_CLIP))

    info = probe(hit.path)
    if info.duration:
        end = min(end, info.duration - 0.05)
    w0 = max(0.0, start - scan_window / 2)
    w1 = min(end + scan_window / 2, info.duration or (end + scan_window))

    try:
        boundaries = detect_shots(hit.path, w0, w1)
    except ProbeError as exc:
        boundaries = []
        log(f"  shot detection unavailable ({exc}) — cutting without snapping")

    cut = snap(boundaries, start, end, w0, w1)
    cut.path = hit.path
    log(f"  {os.path.basename(hit.path)} {cut.start:.2f}s → {cut.end:.2f}s "
        f"({cut.duration:.1f}s) — {cut.note}")
    cut.out = cut_clip(hit.path, cut.start, cut.end, out, mode=mode,
                       height=height, with_audio=with_audio)
    return cut
