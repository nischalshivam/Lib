"""Final ffmpeg pass: video + overlay playlist -> output MP4 (audio copied)."""
from __future__ import annotations

import os
import re
import subprocess

from .util import ffmpeg_exe


def compose(video: str, playlist: str, out: str, has_audio: bool,
            duration: float, crf: int = 18, preset: str = "medium",
            progress=None):
    args = [ffmpeg_exe(), "-hide_banner", "-y",
            "-i", video,
            "-f", "concat", "-safe", "0", "-i", playlist,
            "-filter_complex", "[0:v][1:v]overlay=0:0:eof_action=pass[v]",
            "-map", "[v]"]
    if has_audio:
        args += ["-map", "0:a", "-c:a", "copy"]
    args += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf),
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]
    p = subprocess.Popen(args, cwd=os.path.dirname(playlist),
                         stderr=subprocess.PIPE, text=True)
    tail = []
    for line in p.stderr:
        tail.append(line)
        tail = tail[-25:]
        m = re.search(r"time=(\d+):(\d+):(\d+\.?\d*)", line)
        if m and progress and duration > 0:
            t = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            progress(min(1.0, t / duration))
    p.wait()
    if p.returncode != 0:
        raise RuntimeError("ffmpeg compose failed:\n" + "".join(tail))
    return out
