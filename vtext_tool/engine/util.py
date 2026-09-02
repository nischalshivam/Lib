"""Shared helpers: ffmpeg access, media probing, word normalization."""
from __future__ import annotations

import os
import re
import subprocess

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT_DIR = os.path.join(BASE_DIR, "assets", "fonts")


def ffmpeg_exe() -> str:
    env = os.environ.get("VTEXT_FFMPEG")
    if env and os.path.exists(env):
        return env
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"  # hope it's on PATH


def run_ffmpeg(args: list, **kw) -> subprocess.CompletedProcess:
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y"] + args
    return subprocess.run(cmd, check=True, capture_output=True, **kw)


def probe(video: str) -> dict:
    """Parse duration / resolution / fps / has_audio from ffmpeg -i output."""
    # ffmpeg echoes the (UTF-8) filename in its banner; on Windows text=True
    # would decode as cp1252 and a non-ASCII path char (e.g. a curly quote)
    # crashes the reader thread, leaving stderr=None. Force UTF-8 + replace.
    p = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", video],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    err = p.stderr or ""
    info = {"duration": 0.0, "width": 0, "height": 0, "fps": 30.0,
            "has_audio": False}
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", err)
    if m:
        info["duration"] = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                            + float(m.group(3)))
    m = re.search(r"Video:.*?\s(\d{2,5})x(\d{2,5})", err)
    if m:
        info["width"], info["height"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d+\.?\d*)\s*fps", err)
    if m:
        info["fps"] = float(m.group(1))
    info["has_audio"] = "Audio:" in err
    if not info["duration"] or not info["width"]:
        raise RuntimeError(f"Could not probe video: {video}\n{err[-400:]}")
    return info


_WORD_RE = re.compile(r"[^a-z0-9']+")


def norm_word(w: str) -> str:
    return _WORD_RE.sub("", w.lower())


def norm_words(text: str) -> list:
    return [w for w in (_WORD_RE.sub(" ", text.lower()).split()) if w]


def extract_audio(video: str, out_wav: str) -> str:
    run_ffmpeg(["-i", video, "-vn", "-ac", "1", "-ar", "16000", out_wav])
    return out_wav
