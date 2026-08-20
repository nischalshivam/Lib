"""Which titles does this script need, and do we have them?

A video essay very often draws on more than one source. Saul Goodman appears
in both Breaking Bad and Better Call Saul; a Batman video pulls from several
films; an Avengers video from a dozen. The script knows which title each shot
belongs to — this module collects that up front and checks it against the
library, so the answer to "what do I still need to download?" arrives before
any work starts rather than halfway through a render at 3 a.m.

It reports two levels:

  * **titles** — "Better Call Saul is not in the library"
  * **episodes** — "you have Breaking Bad, but this script needs S02E08 and
    S05E14, and S05 is missing"

The second is what saves a 250 GB download when 2 episodes were the ask.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import term
from .library import connect, normalize
from .search import find

# Short forms that appear in scripts. Extend freely — a miss here only means
# the title is reported as missing, never that the wrong file is used.
ALIASES = {
    "bb": "breaking bad",
    "bcs": "better call saul",
    "got": "game of thrones",
    "asoiaf": "game of thrones",
    "lotr": "the lord of the rings",
    "tdk": "the dark knight",
    "tdkr": "the dark knight rises",
    "aot": "attack on titan",
    "fmab": "fullmetal alchemist brotherhood",
    "hxh": "hunter x hunter",
    "mha": "my hero academia",
    "bcs el camino": "el camino",
}

_LEADING_ARTICLE = re.compile(r"(?i)^(the|a|an)\s+")
_YEAR_SUFFIX = re.compile(r"\s*\(?(19\d{2}|20\d{2})\)?\s*$")


def canonical(title: str) -> str:
    """Normalised form used for matching ('The Dark Knight' -> 'dark knight')."""
    t = normalize(_YEAR_SUFFIX.sub("", title or ""))
    t = ALIASES.get(t, t)
    return _LEADING_ARTICLE.sub("", t).strip()


@dataclass
class TitleRequirement:
    title: str                       # as the script wrote it
    shots: int = 0
    beats: list = field(default_factory=list)
    episodes_declared: set = field(default_factory=set)   # (season, episode)
    episodes_resolved: set = field(default_factory=set)
    status: str = "missing"          # present | partial | missing | no_text_subs
    library_titles: list = field(default_factory=list)
    have_episodes: set = field(default_factory=set)
    note: str = ""

    @property
    def missing_episodes(self) -> set:
        want = self.episodes_declared | self.episodes_resolved
        return {e for e in want if e not in self.have_episodes}

    @property
    def icon(self) -> str:
        return {"present": term.sym("ok"), "partial": term.sym("warn"),
                "no_text_subs": term.sym("warn"),
                "missing": term.sym("fail")}[self.status]


def _parse_se(value) -> tuple | None:
    m = re.match(r"(?i)\s*s(\d{1,2})\s*e(\d{1,3})\s*$", str(value or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def requirements(beats: list) -> list[TitleRequirement]:
    """Collect every title the script asks for, with shot counts."""
    by_key: dict[str, TitleRequirement] = {}
    for b in beats:
        beat_no = b.get("beat")
        for shot in (b.get("shots") or []):
            title = (shot.get("source") or "").strip()
            if not title:
                continue
            # The same rule the `images` list has always had, applied where
            # the model actually puts these: a shot marked as coming from
            # outside the film, or one naming no episode of a series it
            # claims to be from, is a press photo or a piece of stock — and
            # looking those up in a library of episodes reports titles
            # missing on a script that needs none of them.
            kind = (shot.get("type") or "from_source").strip().lower()
            if kind and kind != "from_source":
                continue
            key = canonical(title)
            req = by_key.get(key)
            if req is None:
                req = by_key[key] = TitleRequirement(title=title)
            req.shots += 1
            if beat_no is not None and beat_no not in req.beats:
                req.beats.append(beat_no)
            se = _parse_se(shot.get("season_episode"))
            if se:
                req.episodes_declared.add(se)
        # Images can name a source too — but only the ones taken FROM the
        # film. An actor's press photo or a piece of stock b-roll is fetched
        # from elsewhere, and the model writes the description in the source
        # field: "real-world press photo", "stock imagery". Looking those up
        # in a library of films reports four titles missing on a script that
        # needs none of them, and enough of that blocks a job outright.
        for img in (b.get("images") or []):
            kind = (img.get("type") or "from_source").strip().lower()
            if kind and kind != "from_source":
                continue
            title = (img.get("source") or "").strip()
            if not title:
                continue
            key = canonical(title)
            if key not in by_key:
                by_key[key] = TitleRequirement(title=title)
    return sorted(by_key.values(), key=lambda r: -r.shots)


def _library_titles(con) -> list:
    return [dict(r) for r in con.execute(
        """SELECT show, show_norm, kind, year,
                  COUNT(*) files, SUM(cue_count) lines,
                  SUM(CASE WHEN cue_count=0 THEN 1 ELSE 0 END) no_sub_files,
                  MIN(season) s_min, MAX(season) s_max
             FROM media GROUP BY show_norm""")]


def _matches(req_key: str, lib_rows: list) -> list:
    """Library titles that plausibly are this script title."""
    out = []
    for row in lib_rows:
        lib_key = canonical(row["show"])
        if not lib_key or not req_key:
            continue
        if lib_key == req_key or req_key in lib_key or lib_key in req_key:
            out.append(row)
    return out


def check(db_path: str, beats: list, resolve_dialogue: bool = True,
          log=lambda *a: None) -> list[TitleRequirement]:
    """Full report: what the script needs vs what the library holds."""
    reqs = requirements(beats)
    con = connect(db_path)
    try:
        lib_rows = _library_titles(con)

        # Which episodes does the dialogue actually land in? A script hint can
        # be wrong; the library is the authority, so both are collected.
        if resolve_dialogue:
            by_key = {canonical(r.title): r for r in reqs}
            for b in beats:
                for shot in (b.get("shots") or []):
                    q = (shot.get("exact_dialogue")
                         or shot.get("nearest_dialogue") or "").strip()
                    req = by_key.get(canonical(shot.get("source") or ""))
                    if not q or req is None:
                        continue
                    hits = find(db_path, q, show=shot.get("source"),
                                limit=1, con=con)
                    h = hits[0] if hits else None
                    if h and h.confidence in ("high", "medium") \
                            and h.season is not None:
                        ep = h.episode
                        if h.is_combined and h.chapter_index is not None:
                            ep = (h.episode or 1) + h.chapter_index
                        req.episodes_resolved.add((h.season, ep))

        for req in reqs:
            key = canonical(req.title)
            rows = _matches(key, lib_rows)
            req.library_titles = [r["show"] for r in rows]
            if not rows:
                req.status = "missing"
                req.note = "not in the library — download it"
                continue

            have = set()
            for r in rows:
                for row in con.execute(
                        """SELECT season, episode, episode_to, is_combined
                             FROM media WHERE show_norm=?""", (r["show_norm"],)):
                    if row["season"] is None:
                        continue
                    if row["is_combined"] and row["episode"] and row["episode_to"]:
                        for e in range(row["episode"], row["episode_to"] + 1):
                            have.add((row["season"], e))
                    elif row["is_combined"]:
                        have.add((row["season"], None))     # whole season
                    elif row["episode"] is not None:
                        have.add((row["season"], row["episode"]))
            # a whole-season pack satisfies any episode of that season
            seasons_whole = {s for s, e in have if e is None}
            req.have_episodes = have | {
                (s, e) for s, e in (req.episodes_declared | req.episodes_resolved)
                if s in seasons_whole}

            total_lines = sum(r["lines"] or 0 for r in rows)
            no_sub = sum(r["no_sub_files"] or 0 for r in rows)
            if total_lines == 0:
                req.status = "no_text_subs"
                req.note = ("present but no readable subtitles — the dialogue "
                            "index is empty for this title")
            elif req.missing_episodes:
                req.status = "partial"
                miss = ", ".join(f"S{s:02d}E{e:02d}"
                                 for s, e in sorted(req.missing_episodes)
                                 if e is not None)
                req.note = f"missing {miss}" if miss else "some episodes missing"
            else:
                req.status = "present"
                req.note = (f"{sum(r['files'] for r in rows)} file(s), "
                            f"{total_lines:,} lines")
                if no_sub:
                    req.note += f" · {no_sub} file(s) without subtitles"
        return reqs
    finally:
        con.close()


def format_report(reqs: list[TitleRequirement]) -> str:
    lines = ["SOURCES REQUIRED BY THIS SCRIPT", ""]
    if not reqs:
        return "no sources named in this script"
    width = max(len(r.title) for r in reqs)
    for r in reqs:
        lines.append(f"  {r.icon} {r.title:<{width}}  {r.shots:>3} shot(s)   "
                     f"{r.note}")
        want = sorted(e for e in (r.episodes_declared | r.episodes_resolved)
                      if e[1] is not None)
        if want and r.status != "present":
            lines.append(f"        needs: " + ", ".join(
                f"S{s:02d}E{e:02d}" for s, e in want))
    blocked = [r for r in reqs if r.status == "missing"]
    partial = [r for r in reqs if r.status in ("partial", "no_text_subs")]
    lines.append("")
    if blocked:
        lines.append(f"  {term.sym('fail')} {len(blocked)} title(s) missing - "
                     "these shots cannot be cut until downloaded")
    if partial:
        lines.append(f"  {term.sym('warn')} {len(partial)} title(s) incomplete")
    if not blocked and not partial:
        lines.append(f"  {term.sym('ok')} every source this script needs "
                     "is in the library")
    return "\n".join(lines)
