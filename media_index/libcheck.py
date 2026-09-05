"""Does the library exist for a script? — the launcher's pre-build gate.

Given a visual/clue script and the movies root, work out exactly which
(show, season, episode) it draws from, then check — straight off the per-episode
`*.catalog.json` files on disk, no database needed — which are already
catalogued and which still have to be built. This is the check the launcher runs
before it starts a video: green (all present) → go; otherwise it names the exact
episodes to build, which is the list the user pastes back to have them made.
"""
from __future__ import annotations

import glob
import json
import os
import re

from . import jobs
from . import subtitles


class ScriptUnreadable(RuntimeError):
    """The visual/clue script JSON is malformed — a fix-the-file error, not a
    missing library. The launcher shows this so the user repairs the script."""


def _epkey(name: str):
    k = subtitles.episode_key(name or "")
    return f"S{k[0]:02d}E{k[1]:02d}" if k else None


def _epkeys(name: str) -> list:
    """Every episode a file covers — a two-part finale shipped as one
    "S03E23E24" file has both of its episodes catalogued, so both are present."""
    return [f"S{s:02d}E{e:02d}" for s, e in subtitles.episode_keys(name or "")]


def needed_from_script(script_path: str) -> dict:
    """{show_lower: set(of 'S04E13')} the script's shots reference.

    A shot's `source` (the show title) and `season_episode` together name one
    episode; `source` alone with no episode is a whole-title need (a movie).
    """
    try:
        beats = jobs.read_beats(script_path)
    except Exception as exc:
        raise ScriptUnreadable(
            f"script JSON could not be read (fix the file, then re-check): {exc}"
        ) from exc
    out: dict = {}
    for b in beats:
        for shot in (b.get("shots") or []):
            show = str(shot.get("source") or "").strip()
            if not show or (shot.get("type") or "").strip() == "real_world":
                continue                       # stock / press photo — not our library
            ep = _epkey(str(shot.get("season_episode") or ""))
            out.setdefault(show.lower(), set())
            if ep:
                out[show.lower()].add(ep)
    return out


def _catalogued(movies_root: str) -> dict:
    """{ (show_folder_lower, 'S04E13'): complete_bool } for every catalog on disk."""
    found: dict = {}
    for f in glob.glob(os.path.join(movies_root, "**", "*.catalog.json"),
                       recursive=True):
        eps = _epkeys(os.path.basename(f))
        if not eps:
            continue
        # the show is the top-level folder under movies_root
        rel = os.path.relpath(f, movies_root)
        show = rel.split(os.sep)[0].lower()
        try:
            data = json.load(open(f, encoding="utf-8"))
            shots = data.get("shots") or []
            ok = bool(data.get("complete")) or (
                shots and sum(1 for s in shots if s.get("description"))
                >= 0.9 * len(shots))
        except Exception:
            ok = False
        for ep in eps:                  # a combined file satisfies each of them
            found[(show, ep)] = ok
    return found


def _match_show(want: str, have_shows: set) -> str:
    """Loose-match a script's show name to a movies folder name."""
    w = re.sub(r"[^a-z0-9]+", "", want.lower())
    for s in have_shows:
        if re.sub(r"[^a-z0-9]+", "", s) == w or w in re.sub(r"[^a-z0-9]+", "", s) \
                or re.sub(r"[^a-z0-9]+", "", s) in w:
            return s
    return ""


def check(script_path: str, movies_root: str) -> dict:
    """The launcher gate. Returns:
    {ready: bool, shows: [{show, needed, present, missing, incomplete}], ...}
    `missing` = no catalog at all; `incomplete` = catalog exists but < 90% done.
    """
    need = needed_from_script(script_path)
    have = _catalogued(movies_root)
    have_shows = {sh for sh, _ep in have.keys()}
    report = []
    all_ready = True
    for show, eps in sorted(need.items()):
        folder = _match_show(show, have_shows)
        present, missing, incomplete = [], [], []
        for ep in sorted(eps):
            ok = have.get((folder, ep)) if folder else None
            if ok is True:
                present.append(ep)
            elif ok is False:
                incomplete.append(ep)          # catalog there but not finished
            else:
                missing.append(ep)             # no catalog at all
        if missing or incomplete:
            all_ready = False
        report.append({"show": show, "matched_folder": folder,
                       "needed": sorted(eps), "present": present,
                       "missing": missing, "incomplete": incomplete})
    return {"ready": all_ready, "script": script_path, "shows": report}


def format_report(res: dict) -> str:
    lines = []
    if res["ready"]:
        lines.append("LIBRARY READY — all episodes this script needs are catalogued. Good to go.")
    else:
        lines.append("LIBRARY NOT READY — build these first:")
    for s in res["shows"]:
        tag = "OK" if not (s["missing"] or s["incomplete"]) else "BUILD"
        lines.append(f"  [{tag}] {s['show']}: needs {len(s['needed'])}"
                     + (f" | present {len(s['present'])}" if s['present'] else "")
                     + (f" | MISSING {s['missing']}" if s['missing'] else "")
                     + (f" | INCOMPLETE {s['incomplete']}" if s['incomplete'] else ""))
    return "\n".join(lines)
