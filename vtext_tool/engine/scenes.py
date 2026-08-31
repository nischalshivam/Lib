"""Shot-cut detection from low-res grayscale frame differences."""
from __future__ import annotations

import subprocess

import numpy as np

from .util import ffmpeg_exe

_W, _H, _FPS = 64, 36, 6.0


def detect_cuts(video: str, duration: float, log=print) -> list:
    """Return sorted list of cut times (seconds)."""
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
           "-i", video, "-vf", f"scale={_W}:{_H},fps={_FPS}",
           "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    p = subprocess.run(cmd, capture_output=True)
    buf = p.stdout
    n = len(buf) // (_W * _H)
    if n < 3:
        return []
    frames = np.frombuffer(buf[: n * _W * _H], dtype=np.uint8)
    frames = frames.reshape(n, _H, _W).astype(np.int16)
    diffs = np.abs(frames[1:] - frames[:-1]).mean(axis=(1, 2))
    med = float(np.median(diffs))
    thresh = max(22.0, med * 4.0)
    cuts = []
    for i, d in enumerate(diffs):
        if d > thresh:
            t = (i + 1) / _FPS
            if not cuts or t - cuts[-1] > 0.4:
                cuts.append(round(t, 3))
    log(f"[scenes] {len(cuts)} shot cuts detected")
    return cuts
