"""Times a person states, which outrank everything the tool can guess.

Every other module in this package is an argument about evidence: a quoted
line is exact, a picture match is a measurement against a noise floor, a
window is twenty weak agreements added up. All of that exists because nobody
had told the tool where the scene was.

Somebody usually knows. The person making the video has watched the episode;
the model that wrote the script has read a hundred summaries of it. "The box
cutter scene is 29:30 to 33:40 of S04E01" is one line to type and it is
worth more than the entire visual index, because it is not a guess at all.

Measured on the build this module was written for:

    Breaking Bad S04E01: 85 shot(s), no quoted line at all
    Breaking Bad S04E01: only 2 of 84 shot(s) beat what an unrelated caption
                         scores here — that is chance, not a match
    Breaking Bad S04E01: the picture has no opinion about where this run
                         happens

Eighty-five shots — the entire first half of an eleven-minute video — with
no dialogue to anchor to, no picture the model could place, and no opinion
about which four minutes of a forty-seven minute episode they came from.
There was nothing left for the tool to be clever with. One typed line
removes the whole problem.

Two ways in, because the two suit different moments:

  - **In the script.** A shot may state `at`, and a run may state
    `scene_range`. The model writing the visual script fills these in, and
    they travel with the script forever.
  - **Typed into New Video.** One small box, a line per scene:

        S04E01 29:30-33:40
        Breaking Bad S03E13 30:05

    Nothing to edit, nothing to regenerate, and it is the fastest way to
    rescue a script that is already written.

A stated time is never checked, never scored, and never overruled.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import align, subtitles
from .library import normalize

# A stated moment with no end is a point, not a range. The run around it
# still needs room, so the point is opened out by this much either side —
# comfortably more than any single scene, and far less than an episode.
POINT_PAD_S = 90.0


def parse_timecode(text) -> float | None:
    """Seconds from "29:47", "1:29:47", "29:47.5", "1787", "29m47s".

    Deliberately generous. This is read from things people type at midnight
    and from things a language model wrote, and refusing "29.47" on a
    technicality helps nobody. What it will NOT do is guess: anything it
    cannot read confidently comes back None and is ignored, because a
    misread timecode is worse than no timecode — it is a confident wrong
    answer wearing the one label this tool promises never to check.
    """
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text) if float(text) >= 0 else None
    s = str(text).strip().lower()
    if not s:
        return None

    got = re.fullmatch(r"(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:([\d.]+)s)?", s)
    if got and any(got.groups()):
        h, m, sec = got.groups()
        return (float(h or 0) * 3600.0 + float(m or 0) * 60.0
                + float(sec or 0))

    parts = s.replace(";", ":").split(":")
    try:
        if len(parts) == 1:
            # A bare number is seconds. "29.47" is 29 seconds, not 29:47 —
            # anyone writing minutes writes a colon.
            return max(0.0, float(parts[0]))
        if len(parts) == 2:
            return max(0.0, float(parts[0]) * 60.0 + float(parts[1]))
        if len(parts) == 3:
            return max(0.0, float(parts[0]) * 3600.0 + float(parts[1]) * 60.0
                       + float(parts[2]))
    except ValueError:
        return None
    return None


def parse_range(text) -> tuple | None:
    """(lo, hi) from "29:30-33:40", "29:30 to 33:40", or a single "29:30"."""
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    # An en dash is what a word processor turns a hyphen into, and a script
    # that has been through one should not silently lose its ranges.
    s = s.replace("–", "-").replace("—", "-")
    s = re.sub(r"\s+to\s+", "-", s, flags=re.I)
    bits = [b for b in s.split("-") if b.strip()]
    if len(bits) >= 2:
        lo, hi = parse_timecode(bits[0]), parse_timecode(bits[1])
        if lo is None or hi is None:
            return None
        return (min(lo, hi), max(lo, hi)) if hi != lo else (lo, lo)
    at = parse_timecode(bits[0] if bits else s)
    if at is None:
        return None
    return (at, at)


@dataclass
class Stated:
    """One line somebody typed, or one range a script declared."""
    show: str = ""              # "" means "whatever episode this run uses"
    season: int | None = None
    episode: int | None = None
    lo: float = 0.0
    hi: float = 0.0
    source: str = "typed"       # typed | script

    @property
    def window(self) -> tuple:
        if self.hi > self.lo:
            return (max(0.0, self.lo), self.hi)
        return (max(0.0, self.lo - POINT_PAD_S), self.lo + POINT_PAD_S)

    @property
    def label(self) -> str:
        key = (f"S{self.season:02d}E{self.episode:02d}"
               if self.season is not None and self.episode is not None
               else "?")
        return f"{self.show + ' ' if self.show else ''}{key}"


# "S04E01 29:30-33:40", "Breaking Bad S03E13 30:05", "4x01 29:30 - 33:40"
_LINE = re.compile(r"""^\s*
    (?P<show>.*?)\s*
    (?P<key>s\d{1,2}\s*e\d{1,3}|\d{1,2}\s*x\s*\d{1,3})\s*
    [:\-–,]?\s*
    (?P<time>[\d:;.\s].*?)\s*$""", re.I | re.X)


def parse_lines(text: str) -> list[Stated]:
    """Every readable line of the New Video timings box.

    Unreadable lines are skipped rather than raised on. Someone pasting six
    lines from a chat window will have a stray heading in there, and losing
    the whole box to it — right before a two-hour build — would be a poor
    trade for strictness nobody asked for.
    """
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        got = _LINE.match(line)
        if not got:
            continue
        span = parse_range(got.group("time"))
        if span is None:
            continue
        key = subtitles.episode_key(got.group("key"))
        if not key:
            continue
        season, episode = key
        out.append(Stated(show=(got.group("show") or "").strip(),
                          season=season, episode=episode,
                          lo=span[0], hi=span[1], source="typed"))
    return out


# What a script may call the fields. Written wide on purpose: the visual
# script comes out of a language model, the prompt asks for one name, and
# the model will occasionally use a synonym. Accepting the synonym costs one
# line here; rejecting it costs a whole run of the video.
SHOT_TIME_KEYS = ("at", "timestamp", "time", "episode_time", "at_seconds")
RANGE_KEYS = ("scene_range", "episode_range", "range", "scene_time",
              "scene_timestamp")


def shot_time(shot: dict) -> float | None:
    """The moment a single shot states it happens at, if it states one."""
    for key in SHOT_TIME_KEYS:
        if key in (shot or {}):
            at = parse_timecode(shot.get(key))
            if at is not None:
                return at
    return None


def shot_times(beats: list) -> dict:
    """{(beat, shot index from 1): seconds} for every shot that states one."""
    out: dict = {}
    for i, beat in enumerate(beats or [], 1):
        beat_no = beat.get("beat", i)
        for n, shot in enumerate(beat.get("shots") or [], 1):
            at = shot_time(shot)
            if at is not None:
                out[(beat_no, n)] = at
    return out


def from_script(beats: list) -> list[Stated]:
    """Ranges the script itself declares, one per run that declares one.

    Read off any shot of the run: the model is asked to put it on the first
    shot, and a range repeated on all of them says the same thing.
    """
    out = []
    for run in align.runs(beats or []):
        key = subtitles.episode_key(run.season_episode or "")
        span = None
        for entry in run.entries:
            for name in RANGE_KEYS:
                if name in (entry.data or {}):
                    span = parse_range((entry.data or {}).get(name))
                    if span:
                        break
            if span:
                break
        if not span:
            continue
        out.append(Stated(show=run.source or "",
                          season=key[0] if key else None,
                          episode=key[1] if key else None,
                          lo=span[0], hi=span[1], source="script"))
    return out


def _matches(run, said: Stated) -> bool:
    """Is this typed line talking about this run's episode?"""
    key = subtitles.episode_key(run.season_episode or "")
    if said.season is not None and said.episode is not None:
        if not key or key != (said.season, said.episode):
            return False
    if said.show:
        want, have = normalize(said.show), normalize(run.source or "")
        if want and have and want not in have and have not in want:
            return False
    return True


def windows_for(beats: list, stated: list, log=lambda *a: None) -> dict:
    """{(beat, shot): (lo, hi)} for every run somebody stated a time for.

    Keyed by SHOT, not by beat, and that is not a detail. A beat routinely
    draws from several episodes — on a real 34-beat script, **24 of the 34
    beats did** — so one window per beat means the last episode to be
    processed silently overwrites every other episode's window in that beat.

    Measured on that build: S04E01 was told 5:00-8:00 and S03E01 was told
    10:00-15:00, and both runs were laid out at 39.7 minutes, because
    S03E13's window (38:00-42:00) had been written into the beats they
    shared. Three episodes, one window, two of them completely wrong. It is
    the reason that build came back as "kuch bhi clips".

    Typed lines are applied after script ranges, so the box in front of
    someone wins over a field written days ago by a model. That is the right
    way round: the box is what they reach for when the script is wrong.
    """
    if not stated:
        return {}
    out: dict = {}
    for said in sorted(stated, key=lambda s: s.source == "typed"):
        for run in align.runs(beats or []):
            if not _matches(run, said):
                continue
            for entry in run.entries:
                out[(entry.beat, entry.shot)] = said.window
            lo, hi = said.window
            log(f"      {run.label}: you said this is at "
                f"{int(lo//60)}:{int(lo%60):02d}-{int(hi//60)}:{int(hi%60):02d}"
                " — nothing will look elsewhere")
    return out


# How much wider than the run's own screen time a stated window may be
# before it is worth saying something. A run asking for seventy seconds of
# footage inside a ten-minute window is not being placed by that window; it
# is being spread across it.
WIDE_FACTOR = 3.0


def too_wide(beats: list, stated: list) -> list:
    """Stated windows far wider than the run inside them, worst first.

    Written after a real script came back with `S03E13 40:00-47:00` for a
    six-shot run — seven minutes of episode for thirty seconds of video —
    and `20:00-30:00`, `40:00-50:00`, `30:00-45:00` for three others. Round
    numbers, every one of them, which is what a guess looks like written
    down.

    A range like that is not wrong, and the tool will use it. It is just
    barely worth having, and the person can fix it in ten seconds if
    somebody tells them which one to look at.
    """
    out, seen = [], set()
    for said in stated or []:
        lo, hi = said.window
        room = hi - lo
        for run in align.runs(beats or []):
            if not _matches(run, said) or run.label in seen:
                continue
            seen.add(run.label)
            wanted = 0.0
            for entry in run.entries:
                try:
                    wanted += float((entry.data or {})
                                    .get("duration_target_sec") or 4.0)
                except (TypeError, ValueError, AttributeError):
                    wanted += 4.0
            if wanted > 0 and room > wanted * WIDE_FACTOR:
                out.append((room / wanted, run.label, len(run.entries),
                            room, wanted))
    out.sort(reverse=True)
    return out


def placeable(run) -> bool:
    """Is this a run somebody could state a time for at all?

    A press portrait of Vince Gilligan has no episode and no timecode. It
    was appearing in the pre-flight as `unknown 29:30-33:40 — koi timing
    nahi`, asking a person to supply a time for a photograph, which is not
    a thing that exists.
    """
    return subtitles.episode_key(run.season_episode or "") is not None


def honour(beats: list, placements: list, windows: dict,
           log=lambda *a: None) -> dict:
    """Stated windows, minus the ones a quoted line contradicts.

    A stated time outranks every guess in this package. It does not outrank
    a *measurement*, and a line matched in the real subtitle file is one: it
    is a millisecond somebody can go and check.

    This is not a hypothetical conflict. On a real script the model filled
    in `scene_range` for eight runs and four of the five that could be
    checked were wrong by seven to fifteen minutes:

        S04E01  said 40:00-46:00   the quoted line is at 30:36
        S03E13  said 42:00-47:00   the quoted line is at 29:50
        S04E08  said 36:00-42:00   the quoted line is at 43:43
        S04E11  said 35:00-42:00   the quoted line is at 20:10
        S04E13  said 30:00-38:00   the quoted lines agree

    The build came out well because alignment used the lines and ignored the
    windows — but the windows were still steering the filler, which is how
    five scenes of that build got footage from 40-42 minutes for a scene
    that happens at 30-38. And the log said "nothing will look elsewhere"
    the whole time, which was not true.

    So a contradicted window is dropped, loudly. The person can then fix
    their line or delete it, which is a thing they can act on.
    """
    if not windows:
        return {}
    by_key = {(p.beat, p.shot): p for p in placements or []}
    out = dict(windows)
    for run in align.runs(beats or []):
        if not run.entries:
            continue
        span = windows.get((run.entries[0].beat, run.entries[0].shot))
        if not span or span[1] <= span[0]:
            continue
        found = [by_key[(e.beat, e.shot)].start_ms / 1000.0
                 for e in run.entries
                 if (e.beat, e.shot) in by_key
                 and by_key[(e.beat, e.shot)].method == "anchor"]
        if not found:
            continue                     # nothing measured; the window stands
        lo, hi = span
        if any(lo <= at <= hi for at in found):
            continue                     # they agree
        at = sorted(found)[len(found) // 2]
        log(f"      {run.label}: you said "
            f"{int(lo//60)}:{int(lo%60):02d}-{int(hi//60)}:{int(hi%60):02d}, "
            f"but the line this run quotes is really at "
            f"{int(at//60)}:{int(at%60):02d} — "
            "using the line, and ignoring the time you gave. Fix it or "
            "delete it.")
        for e in run.entries:
            out.pop((e.beat, e.shot), None)
    return out


def derive(beats: list, placements: list, pad: float = 60.0) -> list:
    """The timings the build WORKED OUT, as lines for the box.

    This is the answer to "how will I know the times for the next video".
    Mostly, you will not have to: a run with quoted lines has already told
    the tool where it is, to the millisecond, and those milliseconds can be
    written back out in the same form the box takes.

    So the loop is: build once, read the derived lines, paste them in, and
    every run that had an anchor is now stated exactly rather than guessed.
    The only runs left to look up by hand are the ones with no line at all —
    and the pre-flight names those separately.

    Returns [(shots, "S04E01 30:20-38:10", how many lines it rests on)],
    biggest run first.
    """
    by_key = {(p.beat, p.shot): p for p in placements or []}
    out = []
    for run in align.runs(beats or []):
        key = subtitles.episode_key(run.season_episode or "")
        if not key:
            continue
        found = [by_key[(e.beat, e.shot)].start_ms / 1000.0
                 for e in run.entries
                 if (e.beat, e.shot) in by_key
                 and by_key[(e.beat, e.shot)].method == "anchor"]
        if not found:
            continue
        lo, hi = max(0.0, min(found) - pad), max(found) + pad
        out.append((len(run.entries),
                    f"S{key[0]:02d}E{key[1]:02d} "
                    f"{int(lo//60)}:{int(lo%60):02d}-{int(hi//60)}:{int(hi%60):02d}",
                    len(found)))
    out.sort(reverse=True)
    return out


def unstated(beats: list, stated: list) -> list:
    """Runs nobody has stated a time for, worst first.

    This is the whole point of asking for timings: telling someone which
    six lines to type is a far better use of a pre-flight than telling them
    the video will be 40% guesswork.
    """
    have = set()
    for said in stated or []:
        for run in align.runs(beats or []):
            if _matches(run, said):
                have.add(run.label)
    out = []
    for run in align.runs(beats or []):
        if run.label not in have and placeable(run):
            out.append((len(run.entries), run.label, run.season_episode))
    out.sort(reverse=True)
    return out
