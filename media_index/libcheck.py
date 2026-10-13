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


def _lp(p: str) -> str:
    """Long-path-safe path for Windows. A catalog whose absolute path is >260
    chars (deep Google-Drive folders, long release names) is listed fine by glob
    but plain open() raises FileNotFoundError — so without this the gate silently
    treats a fully-catalogued episode as INCOMPLETE and blocks a buildable video."""
    if os.name != "nt" or not p:
        return p
    ap = os.path.abspath(p)
    if len(ap) >= 255 and not ap.startswith("\\\\?\\"):
        return "\\\\?\\" + ap
    return ap


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
    # Shot types that are generic B-roll / press imagery, NOT a title in our
    # library: a script routinely tags these ("Mystery board", "Archival
    # television"), and the movie gate must not demand a catalogue for them or a
    # perfectly buildable video is blocked. Selection places them by look.
    NON_LIBRARY = {"real_world", "stock", "b-roll", "broll", "archival", "press"}
    out: dict = {}
    for b in beats:
        for shot in (b.get("shots") or []):
            show = str(shot.get("source") or "").strip()
            if not show or (shot.get("type") or "").strip().lower() in NON_LIBRARY:
                continue                       # stock / press photo — not our library
            ep = _epkey(str(shot.get("season_episode") or ""))
            out.setdefault(show.lower(), set())
            if ep:
                out[show.lower()].add(ep)
    return out


def _catalogued(movies_root: str) -> dict:
    """{ (show_folder_lower, 'S04E13'): complete_bool } for every catalog on disk.
    `movies_root` may be several roots joined with ';' (SSD + Google Drive)."""
    roots = [r.strip() for r in str(movies_root or "").split(";") if r.strip()]
    if len(roots) > 1:
        merged: dict = {}
        for r in roots:
            merged.update(_catalogued(r))
        return merged
    found: dict = {}
    for f in glob.glob(os.path.join(movies_root, "**", "*.catalog.json"),
                       recursive=True):
        eps = _epkeys(os.path.basename(f))
        if not eps:
            continue
        # the show is the top-level folder under movies_root
        rel = os.path.relpath(f, movies_root)
        show = rel.split(os.sep)[0].lower()
        ok, _title = _file_status(f)
        for ep in eps:                  # a combined file satisfies each of them
            found[(show, ep)] = ok
    return found


# --------------------------------------------------------------------------- #
#  per-file status cache: (size, mtime) -> (complete?, title). A cold scan of   #
#  Google-Drive catalogues parses every multi-MB JSON (minutes); after the     #
#  first time only a stat() is needed. Rewritten only when something changed.  #
# --------------------------------------------------------------------------- #
_STATUS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            ".catalog_status_cache.json")
_status: dict = {}
_status_loaded = False
_status_dirty = False


def _file_status(f: str) -> tuple:
    global _status, _status_loaded, _status_dirty
    if not _status_loaded:
        try:
            _status = json.load(open(_STATUS_PATH, encoding="utf-8"))
        except (OSError, ValueError):
            _status = {}
        _status_loaded = True
    try:
        st = os.stat(_lp(f))
        sig = [st.st_size, int(st.st_mtime)]
    except OSError:
        return False, ""
    key = os.path.normcase(os.path.abspath(f))
    hit = _status.get(key)
    if hit and hit[:2] == sig:
        return bool(hit[2]), hit[3]
    try:
        data = json.load(open(_lp(f), encoding="utf-8"))
        shots = data.get("shots") or []
        ok = bool(data.get("complete")) or bool(
            shots and sum(1 for s in shots if s.get("description")) >= 0.9 * len(shots))
        title = str((shots[0].get("source") if shots else "") or "")
    except Exception:
        ok, title = False, ""
    _status[key] = sig + [ok, title]
    _status_dirty = True
    return ok, title


def _flush_status() -> None:
    global _status_dirty
    if not _status_dirty:
        return
    try:
        tmp = _STATUS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_status, fh)
        os.replace(tmp, _STATUS_PATH)
        _status_dirty = False
    except OSError:
        pass


_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


def _title_tokens(s: str) -> list:
    """A movie title as comparable words: lowercase, year removed. Mirrors
    plan._title_tokens so the gate agrees with what selection will actually
    find."""
    s = _YEAR_RE.sub(" ", (s or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", s).split()


def _tokens_contain(haystack: list, needle: list) -> bool:
    n = len(needle)
    if not n or n > len(haystack):
        return False
    return any(haystack[i:i + n] == needle for i in range(len(haystack) - n + 1))


def _movie_catalogs(movies_root: str) -> list:
    """[(title_tokens, complete_bool)] for every catalogue that is NOT an
    episode — i.e. a film. A movie library is invisible to the episode gate
    (no SxxExx in its name), so without this a script that draws on a film
    passes the check even when that film was never catalogued."""
    roots = [r.strip() for r in str(movies_root or "").split(";") if r.strip()]
    if len(roots) > 1:
        return [m for r in roots for m in _movie_catalogs(r)]
    out = []
    for f in glob.glob(os.path.join(movies_root, "**", "*.catalog.json"),
                       recursive=True):
        if _epkeys(os.path.basename(f)):
            continue                        # an episode catalogue, handled above
        ok, title = _file_status(f)
        if not title:
            title = os.path.basename(f)[:-len(".catalog.json")]
        out.append((_title_tokens(title), ok))
    return out


# A shot's `source` is sometimes not a real title in the library but an EDITORIAL
# / BOOK-LORE label the script writer coined for an analytical beat
# ("book/world-lore context", "a clash of kings", "world lore", "fire & blood /
# world lore", "lotr book canon + got comparison", "analytical synthesis"...).
# There is no such folder or film to catalogue — the beat is meant to be filled
# from the real footage already in scope (GOT/HOTD by character + look). When
# MI_FILL_GAPS is on, these are treated as soft (non-blocking) so the video builds
# with relatable clips instead of hard-blocking on a title that can never exist.
_EDITORIAL_RE = re.compile(
    r"book|canon|lore|editorial|analytical|synthesis|comparison|inference|"
    r"context|framework|setup|hypothetical|distinction|appendix|tolkien|"
    r"clash of kings|storm of swords|dance with dragons|song of ice|feast for|"
    r"world of ice|fire\s*&\s*blood|fire and blood|awoiaf|official illustrated|"
    r"illustrated history|world-lore|world lore|governance|stress test",
    re.I)


def _is_editorial(show: str) -> bool:
    return bool(_EDITORIAL_RE.search(show or ""))


_COMBINED_RE = re.compile(r"\+|&|/|,|\bvs\.?\b|\bversus\b|\band\b", re.I)


def _is_combined(show: str) -> bool:
    """A source label that names SEVERAL shows ('young sheldon + georgie &
    mandy'). Only these may resolve an episode pin under another show."""
    return bool(_COMBINED_RE.search(show or ""))


def _match_show(want: str, have_shows: set) -> str:
    """Match a script's show name to a library folder name.

    Exact (normalised: no punctuation, no bracketed year, no leading 'the')
    wins. Otherwise one may only be a PREFIX of the other with a short tail
    ('the office' ~ 'the office (us)', 'sopranos' ~ 'the sopranos'). A plain
    substring test is NOT allowed: 'the middle' sits inside 'malcolm in the
    middle', which let The Middle's episode pins 'resolve' to Malcolm's."""
    key = subtitles.show_key
    w = key(want)
    if not w:
        return ""
    for s in have_shows:
        if key(s) == w:
            return s
    for s in sorted(have_shows):
        k = key(s)
        short, long_ = (w, k) if len(w) <= len(k) else (k, w)
        if short and long_.startswith(short) and len(long_) - len(short) <= 4:
            return s
    return ""


def check(script_path: str, movies_root: str, soft_editorial: bool = None) -> dict:
    """The launcher gate. Returns:
    {ready: bool, shows: [{show, needed, present, missing, incomplete}], ...}
    `missing` = no catalog at all; `incomplete` = catalog exists but < 90% done.

    soft_editorial: when true (default from env MI_FILL_GAPS), a `source` that names
    no real folder/film AND reads as an editorial/book-lore label is NOT blocking —
    that beat gets filled from the footage already in scope. Real, indexable
    episodes/films that are genuinely missing still block.
    """
    if soft_editorial is None:
        soft_editorial = os.environ.get("MI_FILL_GAPS", "").strip() not in ("", "0")
    need = needed_from_script(script_path)
    have = _catalogued(movies_root)
    have_shows = {sh for sh, _ep in have.keys()}
    have_movies = _movie_catalogs(movies_root)
    # every real episode present anywhere (any show folder) — lets an editorial
    # beat that pins a real episode (e.g. S03E01) resolve to the show that has it.
    have_eps = {ep for (_sh, ep), ok in have.items() if ok}
    report = []
    all_ready = True
    for show, eps in sorted(need.items()):
        folder = _match_show(show, have_shows)

        # No real show folder for this source. It may still be satisfiable:
        #  * a combined / editorial label whose PINNED EPISODES actually live under
        #    another real show (e.g. "young sheldon + georgie & mandy" S03E06 is a
        #    Young Sheldon episode) — resolve each pin against EVERY indexed episode;
        #  * a real FILM (matched by title tokens);
        #  * a pure pseudo-show (interviews / editorial) with no dedicated footage —
        #    soft-fill from whatever real shows are in scope (when soft_editorial).
        if not folder:
            want = _title_tokens(show)
            is_film = any(ok for toks, ok in have_movies
                          if want and _tokens_contain(toks, want))
            if eps and not _is_combined(show):
                # A plain single-show name ("malcolm in the middle", "the middle")
                # whose folder is not in ANY library root. Its episode pins must
                # NOT be "found" under some other show just because every show has
                # an S01E01 — that false positive let whole videos build with no
                # footage of their own show. Missing = blocks, unless it is an
                # editorial label that is meant to be soft-filled.
                if soft_editorial and _is_editorial(show):
                    report.append({"show": show, "matched_folder": None,
                                   "needed": sorted(eps), "present": [show],
                                   "missing": [], "incomplete": [], "fill": True,
                                   "fill_missing": sorted(eps)})
                    continue
                all_ready = False
                report.append({"show": show, "matched_folder": None,
                               "needed": sorted(eps), "present": [],
                               "missing": sorted(eps), "incomplete": [],
                               "not_indexed": True})
                continue
            if eps:
                present = sorted(e for e in eps if e in have_eps)
                missing = sorted(e for e in eps if e not in have_eps)
                if not missing:                       # every pin found under some show
                    report.append({"show": show, "matched_folder": None,
                                   "needed": sorted(eps), "present": present,
                                   "missing": [], "incomplete": []})
                    continue
                if soft_editorial:                    # some pins have no footage: fill
                    report.append({"show": show, "matched_folder": None,
                                   "needed": sorted(eps), "present": present or [show],
                                   "missing": [], "incomplete": [], "fill": True,
                                   "fill_missing": missing})
                    continue
                all_ready = False
                report.append({"show": show, "matched_folder": None,
                               "needed": sorted(eps), "present": present,
                               "missing": missing, "incomplete": []})
                continue
            # no episode pins: a film, or a pure pseudo-show
            if is_film:
                report.append({"show": show, "matched_folder": None, "needed": [show],
                               "present": [show], "missing": [], "incomplete": []})
                continue
            if soft_editorial:
                report.append({"show": show, "matched_folder": None, "needed": [show],
                               "present": [show], "missing": [], "incomplete": [],
                               "fill": True, "fill_missing": []})
                continue
            all_ready = False
            report.append({"show": show, "matched_folder": None, "needed": [show],
                           "present": [], "missing": [show], "incomplete": []})
            continue

        # A combined / loose label ("young sheldon + the big bang theory") matches
        # ONE folder by substring, but its episodes may belong to the OTHER named
        # show — so for a loose match (source name != the folder it matched) we let
        # an episode resolve against ANY indexed show. A CLEAN single-show source
        # (name == folder) never cross-resolves, so a real missing episode of that
        # show still blocks even if another show happens to share the number.
        _clean = lambda s: re.sub(r"[^a-z0-9]+", "", (s or "").lower())
        loose = bool(folder) and _clean(show) != _clean(folder)
        present, missing, incomplete = [], [], []
        for ep in sorted(eps):
            ok = have.get((folder, ep)) if folder else None
            if ok is True:
                present.append(ep)
            elif ok is None and loose and ep in have_eps:
                present.append(ep)             # exists under another named show
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
        if s.get("fill"):
            fm = s.get("fill_missing") or []
            lines.append(f"  [FILL] {s['show']}: editorial/book-lore beat — "
                         "filled from footage in scope"
                         + (f" (no clip for {fm})" if fm else ""))
            continue
        tag = "OK" if not (s["missing"] or s["incomplete"]) else "BUILD"
        lines.append(f"  [{tag}] {s['show']}: needs {len(s['needed'])}"
                     + (f" | present {len(s['present'])}" if s['present'] else "")
                     + (f" | MISSING {s['missing']}" if s['missing'] else "")
                     + (f" | INCOMPLETE {s['incomplete']}" if s['incomplete'] else ""))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  scan cache — importing 20 topics would otherwise rescan ~3000 catalogues    #
#  (all drives) 20 times. Short TTL, so freshly indexed shows appear quickly.  #
# --------------------------------------------------------------------------- #
import time as _time

_CACHE: dict = {}
_TTL_S = 120


def clear_cache() -> None:
    _CACHE.clear()


def _cached(fn):
    def wrapper(movies_root):
        k = (fn.__name__, str(movies_root or ""))
        hit = _CACHE.get(k)
        if hit and _time.time() - hit[0] < _TTL_S:
            return hit[1]
        val = fn(movies_root)
        _flush_status()
        _CACHE[k] = (_time.time(), val)
        return val
    wrapper.__wrapped__ = fn
    wrapper.__name__ = fn.__name__
    return wrapper


_catalogued = _cached(_catalogued)
_movie_catalogs = _cached(_movie_catalogs)
