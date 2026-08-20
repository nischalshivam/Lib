"""Generate a real video file with known ground truth, for testing.

We need a file where we already know, to the millisecond:
  * when the audio is speaking      -> proves the sync detector
  * when the picture cuts           -> proves shot-boundary snapping
  * what is on screen at any time   -> proves the cutter grabbed the right part

So the video is a sequence of solid-colour segments (each cut is a real scene
change) with the segment number burned in, and the audio is a tone burst at
exactly the timings of the accompanying subtitle cues.

Run:  python -m media_index.demo.make_demo_video out.mkv
"""
from __future__ import annotations

import os
import subprocess
import sys

from ..probe import require_ffmpeg
from .make_demo_library import srt

# (name, ffmpeg colour) — visually distinct so scene detection sees each cut
COLORS = [("red", "0xB03030"), ("green", "0x2E8B57"), ("blue", "0x2B4F81"),
          ("yellow", "0xC9A227"), ("purple", "0x6A4C93"), ("teal", "0x1F7A8C"),
          ("orange", "0xC1663B"), ("grey", "0x555555")]

# Ground truth used by the tests. Dialogue is invented.
CUES = [
    (5_000,   8_000,  "The first line lands on the red segment."),
    (12_000,  15_500, "A second line, still red."),
    (22_000,  25_000, "Green now, and the tone moves with it."),
    (31_000,  34_500, "The third speaker answers on green."),
    (42_000,  45_000, "Blue segment, a single sentence."),
    (52_000,  54_500, "I never wanted the harvest."),
    (54_800,  57_500, "I wanted the land it grew on."),
    (63_000,  66_000, "Yellow, and the argument turns."),
    (74_000,  77_000, "Purple. Nobody walks out of this clean."),
    (85_000,  88_000, "Teal, and the last warning."),
    (95_000,  98_000, "Orange, and it is already too late."),
    (106_000, 109_000, "Grey. Then we burn the field."),
]

SEGMENT_SECONDS = 15.0          # one colour per 15 s -> cuts at 15, 30, 45 ...
DURATION = 120.0
WIDTH, HEIGHT, FPS = 640, 360, 25


def scene_cut_times() -> list[float]:
    """Exact times where the picture changes."""
    n = int(DURATION // SEGMENT_SECONDS)
    return [i * SEGMENT_SECONDS for i in range(1, n)]


def n_segments() -> int:
    return int(DURATION // SEGMENT_SECONDS)


def segment_color(i: int) -> tuple:
    """(name, '0xRRGGBB', (r, g, b)) for segment i."""
    name, hexs = COLORS[i % len(COLORS)]
    v = int(hexs, 16)
    return name, hexs, ((v >> 16) & 255, (v >> 8) & 255, v & 255)


def color_at(t: float) -> tuple:
    """Ground truth: the RGB on screen at time `t`."""
    return segment_color(min(int(t // SEGMENT_SECONDS), n_segments() - 1))[2]


def _audio_filter(cues) -> str:
    """A tone that is only audible during the cue intervals."""
    gate = "+".join(f"between(t,{a/1000:.3f},{b/1000:.3f})" for a, b, _ in cues)
    return f"volume=volume='if({gate},1,0)':eval=frame"


def build(out_path: str, cues=None, write_srt=True,
          srt_offset_ms=0, srt_scale=1.0, log=print) -> str:
    """Render the video (+ a matching .srt) and return the video path.

    `srt_offset_ms` / `srt_scale` deliberately mistime the subtitle file, which
    is how the sync detector is tested against a known answer.
    """
    cues = cues or CUES
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    # One colour source per segment, concatenated -> every join is a real cut.
    n = n_segments()
    cmd = [require_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error"]
    for i in range(n):
        _, hexs, _ = segment_color(i)
        cmd += ["-f", "lavfi", "-i",
                f"color=c={hexs}:s={WIDTH}x{HEIGHT}:r={FPS}:d={SEGMENT_SECONDS}"]
    cmd += ["-f", "lavfi", "-i", f"sine=frequency=320:duration={DURATION}"]
    concat = "".join(f"[{i}:v]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v]"
    cmd += ["-filter_complex", concat,
            "-map", "[v]", "-map", f"{n}:a",
            "-af", _audio_filter(cues),
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-g", "50", "-c:a", "aac", "-b:a", "64k",
            "-t", str(DURATION), out_path]
    log(f"rendering {os.path.basename(out_path)} ({DURATION:.0f}s)…")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if r.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError(f"ffmpeg failed: {r.stderr[-600:]}")

    if write_srt:
        shifted = [(int(a * srt_scale) + srt_offset_ms,
                    int(b * srt_scale) + srt_offset_ms, t) for a, b, t in cues]
        sub = os.path.splitext(out_path)[0] + ".srt"
        with open(sub, "w", encoding="utf-8") as f:
            f.write(srt(shifted))
        log(f"wrote {os.path.basename(sub)} "
            f"(offset {srt_offset_ms:+d} ms, scale {srt_scale})")
    return out_path


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "demo_video.mkv"
    off = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    build(out, srt_offset_ms=off)
    print("scene cuts at:", scene_cut_times())
