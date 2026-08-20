"""Attach a folder of downloaded .srt files to the right videos.

A season subtitle pack does not arrive named the way your videos are named,
and it usually contains several versions of each episode:

    Breaking Bad - 1x01 - Pilot.DVDRip.ORPHEUS.en.srt
    Breaking Bad - 1x01 - Pilot.DSR.0TV.en.srt
    Breaking Bad - 1x01 - Pilot.DVDRip.en.srt

Three files, one episode, and they are timed for three different releases.
Renaming thirteen episodes by hand is tedious; picking the wrong version of
each is worse, because the result looks fine and every clip lands beside its
line.

So this matches by episode NUMBER rather than by filename, and when several
versions exist it plays each one against the video's own audio and keeps the
one that actually fits. That is the same measurement the sync detector makes,
used here to answer a different question: not "how far out is this?" but
"which of these belongs to my copy?"
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field

from . import naming, subtitles, sync

SUB_EXT = (".srt", ".vtt", ".ass", ".ssa")
# Enough audio to tell two releases apart without decoding a whole episode.
VERIFY_SECONDS = 420.0
# A version this far out is a different cut, not a small drift.
MAX_SANE_OFFSET_MS = 60_000


@dataclass
class Match:
    video: str
    label: str = ""
    season: int | None = None
    episode: int | None = None
    candidates: list = field(default_factory=list)
    chosen: str = ""
    written: str = ""
    score: float = 0.0
    offset_ms: int = 0
    status: str = "none"      # linked | already | none | unverified
    note: str = ""

    @property
    def icon(self) -> str:
        from . import term
        return {"linked": term.sym("ok"), "already": term.sym("ok"),
                "unverified": term.sym("warn"),
                "none": term.sym("fail")}[self.status]


def episode_of(name: str) -> tuple | None:
    """(season, episode) — one shared implementation, in subtitles.

    This was a second copy of the same logic, and it had drifted ahead of the
    one in subtitles.py: it knew the "Season 2 Episode 1" spelling and the
    other did not. Attaching a pack therefore worked while finding the same
    file as a sidecar did not, for no reason a user could ever see.
    """
    return subtitles.episode_key(name)


def collect(subs_dir: str) -> dict:
    """{(season, episode): [paths]} for every subtitle under a folder."""
    found: dict = {}
    for dirpath, _dirs, files in os.walk(subs_dir):
        for fn in sorted(files):
            if not fn.lower().endswith(SUB_EXT):
                continue
            key = episode_of(fn)
            if key:
                found.setdefault(key, []).append(os.path.join(dirpath, fn))
    return found


TIERS = {"high": 3, "medium": 2, "low": 1, "unknown": 0}


def _rank(video: str, candidates: list, verify: bool,
          log=lambda *a: None) -> tuple:
    """(best_path, score, offset_ms, confidence) for the version that fits.

    Ranked on the detector's own verdict rather than on how small the offset
    is. Distance from zero looked like a sensible tie-breaker and was not: a
    version that cannot be aligned at all comes back pinned to the end of the
    search range, so it read as an enormous offset, every candidate hit the
    same ceiling, and the penalty stopped separating anything. What matters is
    whether the two ends of the sample agree — which is exactly what the
    confidence now means.
    """
    if len(candidates) == 1 and not verify:
        return candidates[0], 0.0, 0, "unknown"

    scored = []
    for path in candidates:
        cues = subtitles.parse_file(path)
        if not cues:
            continue
        if not verify:
            scored.append((0, 0.0, 0, "unknown", path))
            continue
        try:
            r = sync.detect(video, cues, max_seconds=VERIFY_SECONDS)
        except Exception as exc:
            log(f"        {os.path.basename(path)}: could not test ({exc})")
            continue
        verdict = (f"{r.offset_ms:+d} ms" if r.confidence != "low"
                   else "does not line up")
        log(f"        {os.path.basename(path)[:52]:<52} "
            f"score {r.score:.2f}  {r.confidence:<6} {verdict}")
        scored.append((TIERS[r.confidence], r.score, r.offset_ms,
                       r.confidence, path))

    if not scored:
        return "", 0.0, 0, "unknown"
    scored.sort(key=lambda t: (-t[0], -t[1], abs(t[2])))
    tier, score, offset, confidence, path = scored[0]
    # An offset nothing could confirm is not a measurement to pass downstream.
    return path, score, (offset if tier > TIERS["low"] else 0), confidence


def link(video_dir: str, subs_dir: str | None = None, verify: bool = True,
         overwrite: bool = False, log=print) -> list[Match]:
    """Give every video in `video_dir` the subtitle that belongs to it."""
    subs_dir = subs_dir or video_dir
    pool = collect(subs_dir)
    log(f"{sum(len(v) for v in pool.values())} subtitle file(s) covering "
        f"{len(pool)} episode(s) found under {subs_dir}")

    out = []
    for video in naming.walk_media(video_dir):
        mid = naming.parse(video)
        m = Match(video=video, label=mid.label,
                  season=mid.season, episode=mid.episode)
        target = os.path.splitext(video)[0] + ".en.srt"

        if os.path.isfile(target) and not overwrite:
            m.status, m.chosen, m.written = "already", target, target
            m.note = "already has a subtitle"
            out.append(m)
            continue

        key = (mid.season, mid.episode) if mid.season is not None else None
        m.candidates = pool.get(key, []) if key else []
        if not m.candidates:
            m.note = ("no subtitle for this episode in the pack"
                      if key else "could not tell which episode this file is")
            out.append(m)
            continue

        log(f"  {mid.label}: {len(m.candidates)} candidate(s)")
        chosen, score, offset, confidence = _rank(
            video, m.candidates, verify, log)
        if not chosen:
            m.note = "none of the candidates could be read"
            out.append(m)
            continue

        shutil.copyfile(chosen, target)
        m.chosen, m.written, m.score, m.offset_ms = chosen, target, score, offset
        m.status = "linked" if verify else "unverified"
        m.note = os.path.basename(chosen)
        if verify and abs(offset) > 1000:
            m.note += f"  (runs {offset:+d} ms out — build will correct it)"
        elif verify and confidence == "low":
            # Nothing here lined up. The best of a bad set is still linked, so
            # the episode is not silently dropped, but calling that "linked"
            # without saying so would be the quiet failure this tool exists to
            # stop. An offset of zero because a track is already in sync is a
            # different thing entirely, and still counts as linked.
            m.status = "unverified"
            m.note += "  (could not confirm against the audio — check this one)"
        out.append(m)
    return out


def format_results(matches: list[Match]) -> str:
    from . import term
    if not matches:
        return "  no videos found"
    lines = ["", "SUBTITLES", ""]
    width = max(len(m.label or os.path.basename(m.video)) for m in matches)
    for m in matches:
        name = m.label or os.path.basename(m.video)
        lines.append(f"  {m.icon} {name:<{width}}  {m.note}")
    linked = sum(1 for m in matches if m.status == "linked")
    already = sum(1 for m in matches if m.status == "already")
    missing = sum(1 for m in matches if m.status == "none")
    lines += ["", f"  {linked} linked {term.sym('dot')} {already} already had one "
                  f"{term.sym('dot')} {missing} still missing"]
    if missing:
        lines.append(f"  {term.sym('arrow')} those episodes need a subtitle "
                     "downloading separately")
    else:
        lines.append(f"  {term.sym('ok')} every episode has a subtitle — "
                     "run 'check' to confirm, then 'build'")
    return "\n".join(lines)
