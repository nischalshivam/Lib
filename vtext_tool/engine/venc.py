"""Encoder pick for the VText finisher: NVIDIA NVENC (GPU) when the ffmpeg vtext
actually uses can run it, else libx264 (CPU). Probes the SAME binary as
util.ffmpeg_exe() so the decision matches what compose will run. NVENC needs an
ffmpeg that supports it AND a new-enough driver — point VTEXT_FFMPEG at the
system ffmpeg (the imageio-bundled one may lack nvenc). Force CPU: VIDEO_CPU=1.
"""
from __future__ import annotations

import functools
import os
import subprocess
import tempfile

from .util import ffmpeg_exe

_PRESET = {"ultrafast": "p3", "superfast": "p3", "veryfast": "p4",
           "faster": "p4", "fast": "p5", "medium": "p5",
           "slow": "p6", "slower": "p7", "veryslow": "p7"}


@functools.lru_cache(maxsize=1)
def nvenc_ok() -> bool:
    if os.environ.get("VIDEO_CPU", "").strip().lower() in ("1", "true", "yes", "on"):
        return False
    try:
        ff = ffmpeg_exe()
        enc = subprocess.run([ff, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=20)
        if "h264_nvenc" not in (enc.stdout or ""):
            return False
        out = os.path.join(tempfile.gettempdir(), "_vtext_nvenc_probe.mp4")
        p = subprocess.run(
            [ff, "-nostdin", "-y", "-v", "error", "-f", "lavfi",
             "-i", "testsrc2=size=256x256:rate=30", "-t", "0.3",
             "-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "28",
             "-b:v", "0", "-pix_fmt", "yuv420p", out],
            capture_output=True, text=True, timeout=30)
        good = p.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 0
        try:
            os.remove(out)
        except OSError:
            pass
        return good
    except Exception:
        return False


def video_codec(crf=None, preset="medium") -> list:
    if nvenc_ok():
        cq = 20 if crf is None else int(crf)
        return ["-c:v", "h264_nvenc", "-preset", _PRESET.get(preset, "p5"),
                "-rc", "vbr", "-cq", str(cq), "-b:v", "0"]
    args = ["-c:v", "libx264", "-preset", preset]
    if crf is not None:
        args += ["-crf", str(crf)]
    return args


def label() -> str:
    return "h264_nvenc (GPU)" if nvenc_ok() else "libx264 (CPU)"
