"""Pick the video encoder once per process: NVIDIA NVENC (GPU) when this machine
can ACTUALLY run it, otherwise libx264 (CPU).

Twin of prostudio/engine/venc.py, but it probes with the SAME ffmpeg binary the
rest of media_index uses (probe.require_ffmpeg), so the decision matches reality.
An ffmpeg build can list h264_nvenc while the driver is too old to run it, so we
believe only a real throwaway encode. Cached for the process.

Force CPU (to compare, or if the GPU misbehaves) with  VIDEO_CPU=1 .
"""
from __future__ import annotations

import functools
import os
import subprocess
import tempfile

_PRESET = {"ultrafast": "p3", "superfast": "p3", "veryfast": "p4",
           "faster": "p4", "fast": "p5", "medium": "p5",
           "slow": "p6", "slower": "p7", "veryslow": "p7"}


def _ffmpeg() -> str:
    try:
        from .probe import require_ffmpeg
        return require_ffmpeg()
    except Exception:
        return "ffmpeg"


@functools.lru_cache(maxsize=1)
def nvenc_ok() -> bool:
    """True only if a real h264_nvenc encode succeeds on this machine."""
    if os.environ.get("VIDEO_CPU", "").strip().lower() in ("1", "true", "yes", "on"):
        return False
    try:
        ff = _ffmpeg()
        enc = subprocess.run([ff, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=20)
        if "h264_nvenc" not in (enc.stdout or ""):
            return False
        out = os.path.join(tempfile.gettempdir(), "_mi_nvenc_probe.mp4")
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


def video_codec(crf=None, preset="veryfast") -> list:
    """ffmpeg video-codec args. Mirrors an x264 -crf/-preset request onto NVENC
    (-cq constant-quality VBR) when the GPU is usable, else stays on libx264.
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
