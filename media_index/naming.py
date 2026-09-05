"""Filename -> (show, season, episode) / (movie, year).

Release filenames are messy. We parse the *filename* for an episode pattern,
then recover the show name from the folder tree (which is far more reliable
than the filename, because release groups mangle titles).

Zero dependencies. `guessit` is a stronger drop-in upgrade later; this covers
the common shapes without pulling a dependency into a turnkey Windows tool.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

VIDEO_EXT = (".mkv", ".mp4", ".avi", ".mov", ".m4v", ".webm", ".ts", ".wmv")

# Files below this are thumbnails, trailers or stray fragments — not content.
# Kept low on purpose: a legitimate short film or clip must not be discarded
# just because a bigger number looked safer.
MIN_MEDIA_BYTES = 200_000

# Release junk we strip out of a title. Order matters: longest first.
# The season markers matter as much as the codec names: a season folder is
# usually called "Show.SEASON.01.S01.COMPLETE.1080p...-GROUP", and without
# these the show name comes out as "Show SEASON 01 S01 6CH".
_JUNK = r"""(?ix)
    \b(
      2160p|1080p|720p|480p|4k|uhd|hdr10\+?|hdr|dolby\s?vision|dv|sdr
    | x264|x265|h\.?264|h\.?265|hevc|avc|xvid|divx
    | bluray|blu-ray|brrip|bdrip|bdremux|remux|webrip|web-?dl|web|hdtv|dvdrip|dvd|hdrip
    | aac|ac3|eac3|dts(-hd)?|truehd|atmos|flac|mp3|opus|ddp?5\.1|dd\+?|5\.1|7\.1|2\.0
    | \d{1,2}ch
    | 10bit|8bit|hi10p|dual\s?audio|multi|repack|proper|extended|remastered|uncut
    | complete|combined|internal|limited|imax|theatrical|directors?\.?cut
    | (season|series)\s*\d{1,2}|season|series|episodes?|s\d{1,2}|e\d{1,3}
    | ita|eng|english|hindi|tamil|telugu|dual|subbed|dubbed|esub|msub
    | psa|zee\s?caf[eé]
    )\b
"""

# A single file holding a whole season (or a run of episodes). These are common
# and must NOT be silently mistaken for "episode 1" — that would attribute
# every line in a 7-hour file to the first episode.
_EP_RANGE = [
    # S01E01-E07 / S01E01-07 / S01.E01-E10
    re.compile(r"(?i)\bs(?P<season>\d{1,2})\s*[\._\- ]?\s*e(?P<ep_from>\d{1,3})"
               r"\s*[-–~]\s*e?(?P<ep_to>\d{1,3})\b"),
    # S03E23E24 — a two-parter joined with NO separator at all. Without this the
    # single-episode pattern below cannot match either (it needs a word boundary
    # after the number, and "E23E24" has none), so the file fell through to
    # "movie" and its episodes disappeared from the index entirely.
    re.compile(r"(?i)\bs(?P<season>\d{1,2})\s*[\._\- ]?\s*e(?P<ep_from>\d{1,3})"
               r"\s*e(?P<ep_to>\d{1,3})\b"),
    # E01-E13 with the season elsewhere
    re.compile(r"(?i)\be(?P<ep_from>\d{1,3})\s*[-–~]\s*e(?P<ep_to>\d{1,3})\b"),
]
# S03 with no episode number, plus a "whole season" word
_SEASON_PACK = re.compile(
    r"(?i)\bs(?:eason)?\s*[\._\- ]?\s*(?P<season>\d{1,2})\b(?=.*\b"
    r"(complete|combined|full|all[\._\- ]?episodes|pack|batch)\b)")
# Trailing release-group tag. Deliberately strict: it must be hyphen-attached
# ("-KOGi") or bracketed. A looser rule eats the last word of real titles —
# "The Long Winter" would become "The".
_GROUP_TAIL = re.compile(r"(?:-[A-Za-z0-9]{2,12}$|\[[^\]]*\]$)")

_YEAR = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")

# Episode patterns, tried in order. Each must expose 'season' and 'episode'.
_EP_PATTERNS = [
    re.compile(r"(?i)\bs(?P<season>\d{1,2})\s*[\._\- ]?\s*e(?P<episode>\d{1,3})\b"),
    re.compile(r"(?i)\b(?P<season>\d{1,2})\s*x\s*(?P<episode>\d{1,3})\b"),
    re.compile(r"(?i)\bseason\s*(?P<season>\d{1,2})\D{0,12}?episode\s*(?P<episode>\d{1,3})\b"),
    re.compile(r"(?i)\bs(?P<season>\d{1,2})\b\D{0,8}?\bep?\s*(?P<episode>\d{1,3})\b"),
]
# "Season 03" / "S3" style directory
_SEASON_DIR = re.compile(r"(?i)^(?:season|series|s)\s*[\._\- ]?\s*(\d{1,2})$")
# Directories that carry no title information
_SKIP_DIR = re.compile(r"(?i)^(subs?|subtitles|extras|specials|sample|media|movies|"
                       r"tv|shows|series|anime|video|videos|downloads?)$")


@dataclass
class MediaId:
    kind: str                 # "episode" | "movie" | "season_pack"
    show: str                 # series name, or movie title
    year: int | None = None
    season: int | None = None
    episode: int | None = None      # first episode for a season pack
    episode_to: int | None = None   # last episode, when it is a range
    confidence: str = "high"  # high | medium | low

    @property
    def is_combined(self) -> bool:
        return self.kind == "season_pack"

    @property
    def label(self) -> str:
        if self.kind == "season_pack":
            span = ""
            if self.episode and self.episode_to:
                span = f"E{self.episode:02d}-E{self.episode_to:02d}"
            return f"{self.show} S{self.season:02d} {span}".strip() + " [combined]"
        if self.kind == "episode":
            return f"{self.show} S{self.season:02d}E{self.episode:02d}"
        return f"{self.show}" + (f" ({self.year})" if self.year else "")


def _clean_title(raw: str) -> str:
    """'Breaking.Bad.S01E01.1080p.BluRay.x265-GROUP' -> 'Breaking Bad'."""
    s = raw.replace("_", " ").replace(".", " ")
    s = re.sub(r"[\[\]\{\}]", " ", s)
    s = re.sub(_JUNK, " ", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" -_,")
    # a stray release group can survive as a trailing token
    for _ in range(2):
        stripped = _GROUP_TAIL.sub("", s).strip(" -_,")
        if stripped and stripped != s and len(stripped) >= 3:
            s = stripped
        else:
            break
    return s.strip(" -_,")


def _title_from_dirs(path: str) -> tuple[str | None, int | None]:
    """Walk up the folder tree for the show/movie title (and year)."""
    parts = os.path.normpath(path).split(os.sep)[:-1]  # drop the filename
    for name in reversed(parts):
        if not name or _SEASON_DIR.match(name) or _SKIP_DIR.match(name):
            continue
        year = None
        m = _YEAR.search(name)
        if m:
            year = int(m.group(1))
            name = name[:m.start()] + " " + name[m.end():]
        title = _clean_title(name)
        if len(title) >= 2:
            return title, year
    return None, None


def _season_from_dirs(path: str) -> int | None:
    for name in reversed(os.path.normpath(path).split(os.sep)[:-1]):
        m = _SEASON_DIR.match(name.strip())
        if m:
            return int(m.group(1))
    return None


_SEPS = re.compile(r"[._]")


def parse(path: str) -> MediaId:
    """Best-effort identification of a media file."""
    raw_stem = os.path.splitext(os.path.basename(path))[0]
    # "Breaking_Bad_S03_COMBINED" has no word boundary before S03, because "_"
    # is itself a word character — every \b pattern below would silently miss.
    # This is a 1:1 character swap, so match offsets still index into raw_stem.
    stem = _SEPS.sub(" ", raw_stem)
    dir_title, dir_year = _title_from_dirs(path)

    def _show_for(match_start: int) -> str:
        head = _clean_title(stem[:match_start])
        # A filename title that is already substantial is trusted. Only a short
        # one (an abbreviation like "GoT", or nothing at all) defers to the
        # folder — "longest wins" let a random parent directory beat a perfectly
        # good "Iron Harvest".
        if len(head) >= 8:
            return head
        if dir_title and len(dir_title) > len(head):
            return dir_title
        return head if len(head) >= 2 else (dir_title or stem)

    # --- a single file holding several episodes -----------------------------
    # Checked BEFORE the single-episode patterns: "S01E01-E07" also matches
    # "S01E01", and taking that would attribute seven hours to episode one.
    for pat in _EP_RANGE:
        m = pat.search(stem)
        if not m:
            continue
        g = m.groupdict()
        season = int(g["season"]) if g.get("season") else _season_from_dirs(path)
        ep_from, ep_to = int(g["ep_from"]), int(g["ep_to"])
        if ep_to <= ep_from:                     # "E05-E02" is not a range
            continue
        return MediaId("season_pack", _show_for(m.start()), dir_year,
                       season, ep_from, ep_to,
                       "high" if season is not None else "medium")

    m = _SEASON_PACK.search(stem)
    if m and not any(p.search(stem) for p in _EP_PATTERNS):
        return MediaId("season_pack", _show_for(m.start()), dir_year,
                       int(m.group("season")), None, None, "medium")

    for pat in _EP_PATTERNS:
        m = pat.search(stem)
        if not m:
            continue
        season = int(m.group("season"))
        episode = int(m.group("episode"))
        return MediaId("episode", _show_for(m.start()), dir_year, season,
                       episode, None, "high")

    # No episode marker in the filename: maybe the season is only in the folder
    season = _season_from_dirs(path)
    if season is not None:
        m = re.search(r"(?<!\d)(\d{1,3})(?!\d)", stem)
        if m:
            return MediaId("episode", dir_title or _clean_title(stem), dir_year,
                           season, int(m.group(1)), "medium")

    # Treat as a movie
    year = None
    title_src = stem
    m = _YEAR.search(stem)
    if m:
        year = int(m.group(1))
        title_src = stem[:m.start()]
    title = _clean_title(title_src)
    if len(title) < 2:
        title = dir_title or _clean_title(stem) or stem
        year = year or dir_year
    return MediaId("movie", title, year or dir_year, None, None,
                   "high" if year else "medium")


def walk_media(root: str):
    """Yield every video file under `root`, skipping sample/extras noise."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if not re.match(r"(?i)^(sample|extras?|featurettes?)$", d)]
        for fn in sorted(filenames):
            if not fn.lower().endswith(VIDEO_EXT):
                continue
            if re.search(r"(?i)\bsample\b", fn):
                continue
            full = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(full) < MIN_MEDIA_BYTES:
                    continue
            except OSError:
                continue
            yield full
