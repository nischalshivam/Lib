"""Read technical facts about a media file.

Prefers `ffprobe` (structured JSON). Falls back to parsing `ffmpeg -i` stderr,
because some ffmpeg installs ship without ffprobe — and a pre-flight check must
never fail just because one binary is missing.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache


class ProbeError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def ffmpeg_bin() -> str | None:
    return shutil.which("ffmpeg")


@lru_cache(maxsize=1)
def ffprobe_bin() -> str | None:
    return shutil.which("ffprobe")


def require_ffmpeg() -> str:
    exe = ffmpeg_bin()
    if not exe:
        raise ProbeError("ffmpeg not found on PATH — install it and retry")
    return exe


@dataclass
class SubStream:
    index: int          # index *within the subtitle streams* (for -map 0:s:N)
    lang: str = ""
    codec: str = ""
    title: str = ""
    forced: bool = False


@dataclass
class AudioStream:
    index: int          # index within the audio streams (for -map 0:a:N)
    lang: str = ""
    codec: str = ""
    title: str = ""
    channels: int = 0


@dataclass
class Chapter:
    index: int
    start: float             # seconds
    end: float
    title: str = ""


@dataclass
class MediaInfo:
    path: str
    duration: float = 0.0        # seconds
    width: int = 0
    height: int = 0
    fps: float = 0.0
    vcodec: str = ""
    acodec: str = ""
    has_audio: bool = False
    audios: list = field(default_factory=list)    # [AudioStream]
    subs: list = field(default_factory=list)      # [SubStream]
    chapters: list = field(default_factory=list)  # [Chapter]

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}" if self.width else "?"

    @property
    def is_hd(self) -> bool:
        return self.width >= 1280


def _run(cmd: list[str], timeout=180) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          errors="replace", timeout=timeout)


def _fraction(text: str) -> float:
    """'24000/1001' -> 23.976"""
    try:
        if "/" in text:
            a, b = text.split("/", 1)
            return float(a) / float(b) if float(b) else 0.0
        return float(text)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _probe_with_ffprobe(path: str) -> MediaInfo:
    out = _run([ffprobe_bin(), "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", path])
    if out.returncode != 0:
        raise ProbeError(out.stderr.strip()[:300] or "ffprobe failed")
    data = json.loads(out.stdout or "{}")
    info = MediaInfo(path=path)
    info.duration = float(data.get("format", {}).get("duration") or 0)

    sub_n = 0
    for st in data.get("streams", []):
        kind = st.get("codec_type")
        if kind == "video" and not info.width:
            info.width = int(st.get("width") or 0)
            info.height = int(st.get("height") or 0)
            info.vcodec = st.get("codec_name", "")
            info.fps = _fraction(st.get("avg_frame_rate")
                                 or st.get("r_frame_rate") or "0")
            if not info.duration:
                info.duration = float(st.get("duration") or 0)
        elif kind == "audio":
            tags = st.get("tags") or {}
            info.audios.append(AudioStream(
                index=len(info.audios), lang=(tags.get("language") or "").lower(),
                codec=st.get("codec_name", ""), title=tags.get("title", ""),
                channels=int(st.get("channels") or 0)))
            if not info.has_audio:
                info.has_audio = True
                info.acodec = st.get("codec_name", "")
        elif kind == "subtitle":
            tags = st.get("tags") or {}
            info.subs.append(SubStream(
                index=sub_n, lang=(tags.get("language") or "").lower(),
                codec=st.get("codec_name", ""), title=tags.get("title", ""),
                forced=bool((st.get("disposition") or {}).get("forced"))))
            sub_n += 1
    return info


_RE_DUR = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2})\.(\d+)")
_RE_VIDEO = re.compile(
    r"Stream #\d+:(\d+).*?:\s*Video:\s*([\w0-9]+).*?(\d{2,5})x(\d{2,5})")
_RE_FPS = re.compile(r"([\d.]+)\s+fps")
_RE_AUDIO = re.compile(
    r"Stream #\d+:\d+(?:\((\w+)\))?.*?:\s*Audio:\s*([\w0-9]+)(.*)")
_RE_SUB = re.compile(
    r"Stream #\d+:\d+(?:\((\w+)\))?.*?:\s*Subtitle:\s*([\w0-9]+)(.*)")


def _probe_with_ffmpeg(path: str) -> MediaInfo:
    """ffmpeg prints a full stream summary to stderr and exits non-zero."""
    out = _run([require_ffmpeg(), "-hide_banner", "-i", path])
    text = out.stderr or ""
    if "Invalid data" in text or "No such file" in text:
        raise ProbeError(f"cannot read {path}")
    info = MediaInfo(path=path)

    m = _RE_DUR.search(text)
    if m:
        h, mi, s, frac = m.groups()
        info.duration = (int(h) * 3600 + int(mi) * 60 + int(s)
                         + float("0." + frac))
    m = _RE_VIDEO.search(text)
    if m:
        _, info.vcodec, w, h = m.groups()
        info.width, info.height = int(w), int(h)
        line = text[m.start():m.end() + 120]
        f = _RE_FPS.search(line)
        if f:
            info.fps = float(f.group(1))
    for i, am in enumerate(_RE_AUDIO.finditer(text)):
        lang, codec, tail = am.groups()
        info.audios.append(AudioStream(index=i, lang=(lang or "").lower(),
                                       codec=codec))
        # ffmpeg prints the track title on the following metadata line
        tm = re.search(r"(?m)^\s*title\s*:\s*(.+)$", text[am.end():am.end() + 220])
        if tm:
            info.audios[-1].title = tm.group(1).strip()
        if not info.has_audio:
            info.has_audio = True
            info.acodec = codec
    for i, sm in enumerate(_RE_SUB.finditer(text)):
        lang, codec, tail = sm.groups()
        info.subs.append(SubStream(index=i, lang=(lang or "").lower(),
                                   codec=codec,
                                   forced="forced" in (tail or "").lower()))
    if not info.duration and not info.width:
        raise ProbeError(f"could not parse media info for {path}")
    return info


def probe(path: str) -> MediaInfo:
    """Technical facts about a media file. Raises ProbeError on unreadable."""
    if ffprobe_bin():
        try:
            return _probe_with_ffprobe(path)
        except (ProbeError, json.JSONDecodeError, ValueError):
            pass                                   # fall through to the parser
    return _probe_with_ffmpeg(path)


# Words that mark an audio/subtitle track as English when the language tag is
# missing — releases label tracks in the title far more reliably than in tags.
_EN_WORDS = re.compile(r"(?i)\b(eng|english)\b")


def pick_audio(info: MediaInfo, prefer_lang="en") -> int:
    """Index of the best audio track for analysis (-map 0:a:N).

    A Hindi-dubbed release lists the dub FIRST, so taking track 0 analyses the
    dub while the subtitles are English. The timings are close (dubs follow lip
    sync) but not identical, and any transcription would come out in the wrong
    language entirely.
    """
    if not info.audios:
        return 0
    for a in info.audios:
        if a.lang.startswith(prefer_lang):
            return a.index
    for a in info.audios:
        if _EN_WORDS.search(a.title or ""):
            return a.index
    return info.audios[0].index


def chapters(path: str, timeout=120) -> list:
    """Chapter markers, if the container has them.

    Season packs muxed by a release group very often carry one chapter per
    episode. When they do, a timestamp inside a seven-hour file can be
    attributed to the right episode instead of being reported as an offset
    into an anonymous blob. Empty list when there are none (or no ffprobe).
    """
    if not ffprobe_bin():
        return _chapters_with_ffmpeg(path, timeout)
    out = _run([ffprobe_bin(), "-v", "error", "-print_format", "json",
                "-show_chapters", path], timeout=timeout)
    if out.returncode != 0:
        return _chapters_with_ffmpeg(path, timeout)
    try:
        data = json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        return []
    result = []
    for i, ch in enumerate(data.get("chapters", [])):
        try:
            start = float(ch.get("start_time") or 0)
            end = float(ch.get("end_time") or 0)
        except (TypeError, ValueError):
            continue
        title = (ch.get("tags") or {}).get("title", "")
        result.append(Chapter(index=i, start=start, end=end, title=title))
    return result


_RE_CHAP = re.compile(
    r"Chapter #\d+[:.](\d+):\s*start\s*([\d.]+),\s*end\s*([\d.]+)")
_RE_CHAP_TITLE = re.compile(r"(?m)^\s*title\s*:\s*(.+)$")


def _chapters_with_ffmpeg(path: str, timeout=120) -> list:
    """Chapters as printed by `ffmpeg -i`, for installs without ffprobe."""
    if not ffmpeg_bin():
        return []
    out = _run([ffmpeg_bin(), "-hide_banner", "-i", path], timeout=timeout)
    text = out.stderr or ""
    result = []
    for i, m in enumerate(_RE_CHAP.finditer(text)):
        title = ""
        tail = text[m.end():m.end() + 220]
        tm = _RE_CHAP_TITLE.search(tail)
        if tm:
            title = tm.group(1).strip()
        result.append(Chapter(index=i, start=float(m.group(2)),
                              end=float(m.group(3)), title=title))
    return result


def keyframes(path: str, start: float = 0.0, window: float = 60.0) -> list[float]:
    """Keyframe timestamps in [start, start+window]. Empty without ffprobe.

    Cutting on a keyframe is what makes a stream-copy clip start cleanly
    instead of with a smear of grey blocks.
    """
    if not ffprobe_bin():
        return []
    out = _run([ffprobe_bin(), "-v", "error",
                "-read_intervals", f"{max(0.0, start)}%+{window}",
                "-select_streams", "v:0", "-skip_frame", "nokey",
                "-show_entries", "frame=pts_time", "-of", "csv=p=0", path])
    times = []
    for line in out.stdout.splitlines():
        line = line.strip().rstrip(",")
        try:
            times.append(float(line))
        except ValueError:
            continue
    return sorted(times)
