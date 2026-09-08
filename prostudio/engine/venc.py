"""Pick the video encoder once per process: NVIDIA NVENC (GPU) when this machine
can ACTUALLY run it, otherwise libx264 (CPU).

Why a real probe and not just `ffmpeg -encoders`: an ffmpeg build can list
h264_nvenc while the installed driver is too old for it (e.g. ffmpeg needing
NVENC API 13.1 on a 13.0 driver) — listing succeeds, the real encode fails. So
we run one tiny throwaway encode and believe only that. Cached for the process.

Force CPU (e.g. to compare, or if the GPU misbehaves) with  VIDEO_CPU=1 .
"""
from __future__ import annotations

import functools
import os
import subprocess
import tempfile

# x264 preset name -> NVENC preset. p1 fastest/worst .. p7 slowest/best. On an
# NVENC card even p5-p6 is far faster than x264, so we lean to quality.
_PRESET = {"ultrafast": "p3", "superfast": "p3", "veryfast": "p4",
           "faster": "p4", "fast": "p5", "medium": "p5",
           "slow": "p6", "slower": "p7", "veryslow": "p7"}


@functools.lru_cache(maxsize=1)
def nvenc_ok() -> bool:
    """True only if a real h264_nvenc encode succeeds on this machine."""
    if os.environ.get("VIDEO_CPU", "").strip().lower() in ("1", "true", "yes", "on"):
        return False
    try:
        enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=20)
        if "h264_nvenc" not in (enc.stdout or ""):
            return False
        out = os.path.join(tempfile.gettempdir(), "_prostudio_nvenc_probe.mp4")
        p = subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi",
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


def video_codec(crf=None, preset="veryfast") -> list:
    """ffmpeg video-codec args. Mirrors an x264 `-crf/-preset` request onto NVENC
    (`-cq` constant-quality VBR) when the GPU is usable, else stays on libx264.
    Does NOT emit -pix_fmt (callers keep their own)."""
    if nvenc_ok():
        cq = 23 if crf is None else int(crf)
        return ["-c:v", "h264_nvenc", "-preset", _PRESET.get(preset, "p4"),
                "-rc", "vbr", "-cq", str(cq), "-b:v", "0"]
    args = ["-c:v", "libx264", "-preset", preset]
    if crf is not None:
        args += ["-crf", str(crf)]
    return args


def label() -> str:
    return "h264_nvenc (GPU)" if nvenc_ok() else "libx264 (CPU)"
