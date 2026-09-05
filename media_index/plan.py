"""Match a narration/visual script against the catalogue — Stage 2.

The catalogue (`catalog.py`) says what every shot of a film IS. This module
answers the other half: for each thing the script wants on screen, which
catalogued shot should play. It is the retrieval step a friend's brief calls
"for any point in the narration, locate and pull the exact right footage".

## The ladder, precision first

Each shot-request is answered by the strongest signal that fires, and the
answer carries *why* so a person can trust or overrule it:

  1. **dialogue anchor** — the request quotes a line, and a catalogued shot's
     own subtitle text contains it. This is the "money moment": the exact
     second a line was spoken, located to the shot. Strongest, because it is
     matched fact, not resemblance.
  2. **description + character** — no quotable line, so the request's visual
     sentence is matched against the shots' descriptions/tags, filtered to the
     named person. Strong on ordinary connective footage, where any good shot
     of the right character in the right moment is a right answer.
  3. **none → NEEDS VISUAL** — nothing cleared the bar. An honest gap the user
     fills, never a confident wrong guess. (This is the fail-closed rule that
     the whole project turns on: a card beats wrong footage.)

Nothing here decides a timestamp on its own or invents a shot; it only ranks
shots the catalogue already contains. Character *verification* against
reference photos (`cast.py`) is the layer that sits on top of a chosen shot.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import catalog

# A quoted line shorter than this is too common to anchor on — "I know",
# "Stop", "What?" match somewhere in almost every reel and would place a shot
# by coincidence. Longer lines are near-unique to their moment.
MIN_ANCHOR_CHARS = 12


@dataclass
class Request:
    """One thing the script wants on screen."""
    beat: int = 0
    visual: str = ""
    characters: list = field(default_factory=list)
    dialogue: str = ""            # a line the script says is spoken here
    source: str = ""              # which title/episode the shot belongs to
    scene_range: str = ""         # e.g. "40:00-45:00" — confines within source
    kind: str = "clip"            # clip (moving) | still (frozen frame)
    duration: float = 0.0         # duration_target_sec the script asked for

    @property
    def character(self) -> str:
        return self.characters[0] if self.characters else ""


@dataclass
class Match:
    """The chosen shot for a request, and the honest reason."""
    shot: object = None                 # a catalog.Shot, or None
    method: str = "none"                # dialogue | description | none
    why: str = ""

    @property
    def placed(self) -> bool:
        return self.shot is not None


def _norm(s: str) -> str:
    return re.sub(r"\s{2,}", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()


def dialogue_anchor(library: dict, line: str, limit: int = 5) -> list:
    """Catalogued shots whose own subtitle text contains this line.

    The whole line need not match — a script quotes a fragment, the subtitle
    holds the surrounding sentence — so containment either way counts. Returned
    earliest-first, because a repeated line's first delivery is usually the one
    a script means.
    """
    q = _norm(line)
    if len(q) < MIN_ANCHOR_CHARS:
        return []
    # The script's line must appear INSIDE the shot's subtitle text (q in d).
    # The reverse (d in q) was a bug: a shot whose whole dialogue is a short
    # fragment — "nothing.", "you" — is a substring of almost any longer line,
    # so it matched the wrong moment by coincidence.
    hits = [s for s in library.values()
            if _norm(s.dialogue) and q in _norm(s.dialogue)
            and not catalog.is_explicit(s)]      # never anchor onto adult footage
    return sorted(hits, key=lambda s: s.start)[:limit]


def _norm_ep(s: str) -> str:
    """A comparable episode key: 'S04E01', 'Season 4 Episode 1', 'Breaking Bad
    S04E01' all reduce to 's4e1'. The SHOW is dropped here on purpose — use
    `_show_ep` when two shows' same-numbered episodes must be told apart."""
    from . import subtitles
    key = subtitles.episode_key(s or "")
    return f"s{key[0]}e{key[1]}" if key else ""


def _show_ep(s: str) -> tuple:
    """(show, episode) tokens from a source. 'Breaking Bad S04E13' ->
    ('breaking bad', 's4e13'); a bare 'S04E13' -> ('', 's4e13')."""
    from . import subtitles
    return subtitles.show_prefix(s or ""), _norm_ep(s)


def _norm_eps(s: str) -> set:
    """EVERY comparable episode key a source covers. One file can hold a
    two-part finale ('S03E23E24'), and a shot in it belongs to both episodes —
    asking for either one has to find it."""
    from . import subtitles
    return {f"s{a}e{b}" for a, b in subtitles.episode_keys(s or "")}


def scoped(library: dict, source: str) -> dict:
    """Only the shots from the named episode/title. A single-scene essay must
    draw from ONE episode, and searching the whole series scatters its shots.

    Show-aware: 'Better Call Saul S04E13' keeps only BCS's S04E13, never
    Breaking Bad's — both reduce to the same episode NUMBER, and the title is
    the only thing separating two shows of one universe. A request that names
    no show (a bare 'S04E13') still matches on the number alone, so existing
    single-show scripts behave exactly as before.
    """
    if not source:
        return library
    want_show, want_ep = _show_ep(source)
    if want_ep:                               # an episode marker: match by it
        from . import subtitles
        out = {}
        for k, s in library.items():
            if want_ep not in _norm_eps(s.source):
                continue
            sh = subtitles.show_prefix(s.source)
            if want_show and sh and sh != want_show:
                continue                      # same number, different show
            out[k] = s
        return out
    # A movie title. The clue script and the catalogue rarely spell it byte for
    # byte the same — the script writes "The Lord of the Rings: The Fellowship
    # of the Ring" and the file is "The Lord of the Rings The Fellowship of the
    # Ring (2001)". A raw substring test fails on the colon and the year, then
    # falls back to the WHOLE library, so a Fellowship shot could be filled from
    # Two Towers. Compare on a normalised key (no punctuation, no year) instead,
    # which still tells the three films apart by their unique subtitles.
    # The requested title's WORDS must appear, in order, inside the catalogue's
    # (longer, year-tagged) title. Word tokens, not a raw substring: matching on
    # characters let "...part ii" fall inside "...part iii", pulling a wrong
    # sequel's shots. One direction only — the reverse would let "The Godfather"
    # swallow "The Godfather Part II".
    want = _title_tokens(source)
    out = {k: s for k, s in library.items()
           if want and _tokens_contain(_title_tokens(s.source), want)}
    return out or library


_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


def _title_tokens(s: str) -> list:
    """A movie title as comparable words: lowercase, year removed, split on any
    run of non-alphanumerics. 'The Lord of the Rings: The Fellowship of the Ring'
    and '...The Fellowship of the Ring (2001)' reduce to the same word list."""
    s = _YEAR_RE.sub(" ", (s or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", s).split()


def _tokens_contain(haystack: list, needle: list) -> bool:
    """Is `needle` a contiguous run of words inside `haystack`?"""
    n = len(needle)
    if not n or n > len(haystack):
        return False
    return any(haystack[i:i + n] == needle for i in range(len(haystack) - n + 1))


def _range_seconds(text: str) -> tuple:
    """('40:00-45:00' | '2400-2700') -> (2400.0, 2700.0), or () if unreadable."""
    m = re.search(r"(\d{1,2}:\d{2}(?::\d{2})?|\d+)\s*[-–]\s*"
                  r"(\d{1,2}:\d{2}(?::\d{2})?|\d+)", text or "")
    if not m:
        return ()

    def to_s(v):
        if ":" in v:
            parts = [int(p) for p in v.split(":")]
            return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))
        return float(v)
    lo, hi = to_s(m.group(1)), to_s(m.group(2))
    return (lo, hi) if hi > lo else ()


def windowed(pool: dict, scene_range: str, pad: float = 30.0) -> dict:
    """Only shots overlapping the scene's time window (with a little padding).
    This is what pins a single scene inside an episode — the box-cutter scene
    is one five-minute stretch of a forty-seven-minute file."""
    span = _range_seconds(scene_range)
    if not span:
        return pool
    lo, hi = span[0] - pad, span[1] + pad
    inside = {k: s for k, s in pool.items() if s.end > lo and s.start < hi}
    return inside or pool                     # never strand a whole beat


def match(request: Request, library: dict, scope: str = "") -> Match:
    """The best catalogued shot for one request, precision first.

    `scope` (or the request's own `source`) confines the search to one
    episode/title before ranking — the single biggest accuracy lever on a
    series, because it stops a box-cutter line from matching the word
    "box cutter" three episodes away.
    """
    ep_pool = scoped(library, scope or request.source)
    if not ep_pool:                           # scope named nothing we have
        ep_pool = library

    # Dialogue is the most precise locator there is — the exact subtitle
    # timestamp. It searches the whole EPISODE, never the guessed scene window:
    # a genspark `scene_range` is the model's estimate ("range_confidence:
    # medium"), and letting a wrong guess window out the real line is what put
    # "You kill me, you have nothing" at 32:48 instead of the 10:14 it is
    # actually spoken. The guess must never override the fact.
    if request.dialogue:
        anchored = dialogue_anchor(ep_pool, request.dialogue)
        if anchored:
            top = anchored[0]
            return Match(shot=top, method="dialogue",
                         why=f'line at {top.start:.0f}s: "{request.dialogue[:48]}"')

    # Only description — which has no precise locator — leans on the scene
    # window to narrow an episode down to the right minutes.
    pool = (windowed(ep_pool, request.scene_range)
            if request.scene_range else ep_pool)
    hits = catalog.search(pool, f"{request.visual} {request.dialogue}",
                          characters=request.characters)
    if hits:
        where = hits[0].source
        return Match(shot=hits[0], method="description",
                     why=(f"visual+character match"
                          + (f" ({request.character})" if request.character else "")
                          + (f" in {where}" if scope or request.source else "")))

    return Match(method="none", why="koi match nahi — NEEDS VISUAL card")


def candidates(request: Request, library: dict, scope: str = "",
               limit: int = 8) -> list:
    """A RANKED list of Matches for one request, best first.

    `match` returns only the top pick; a visual verifier needs runners-up, so
    that when the first candidate turns out not to show what the script asked
    for, there is a second and a third to check before giving up. Dialogue
    anchors come first (a precise locator), then description hits.
    """
    ep_pool = scoped(library, scope or request.source) or library
    out, seen = [], set()

    def add(shot, method, why):
        if shot.id not in seen:
            seen.add(shot.id)
            out.append(Match(shot=shot, method=method, why=why))

    if request.dialogue:
        for s in dialogue_anchor(ep_pool, request.dialogue, limit=3):
            add(s, "dialogue", f'line at {s.start:.0f}s: "{request.dialogue[:40]}"')

    pool = (windowed(ep_pool, request.scene_range)
            if request.scene_range else ep_pool)
    for s in catalog.search(pool, f"{request.visual} {request.dialogue}",
                            characters=request.characters, limit=limit):
        add(s, "description", f"visual+character match ({request.character})"
            if request.character else "visual match")
    return out


def requests_from_beats(beats: list) -> list:
    """Turn a visual (genspark) script's shots into shot-requests.

    A genspark run marks its `scene_range` on the FIRST shot only; the rest of
    the run belongs to the same scene but carries no range, so on its own each
    of those shots would scope to the whole episode and drift. The range is
    carried forward across shots of the same source until a new range appears
    (a new scene) or the source changes (a new episode) — so every shot of a
    scene is pinned to that scene's window, not just its opening frame.
    """
    out = []
    cur_source, cur_range = "", ""
    for b in beats:
        bn = b.get("beat") or 0
        for shot in (b.get("shots") or []):
            # Show + episode together, so a cross-show script's 'Better Call
            # Saul' + 'S04E13' scopes to BCS, never Breaking Bad's identically
            # numbered episode. Either alone still works (single-show scripts,
            # or a bare episode marker).
            show = str(shot.get("source") or "").strip()
            ep = str(shot.get("season_episode") or "").strip()
            src = f"{show} {ep}".strip() if show and ep else (ep or show)
            rng = str(shot.get("scene_range") or "").strip()
            if src != cur_source:             # new episode: forget the window
                cur_source, cur_range = src, ""
            if rng:                           # new scene: adopt its window
                cur_range = rng
            out.append(Request(
                beat=bn,
                visual=str(shot.get("visual") or ""),
                characters=catalog.list_entries(
                    shot.get("characters") or shot.get("people")),
                dialogue=str(shot.get("exact_dialogue")
                             or shot.get("dialogue") or "").strip(),
                source=src,
                scene_range=rng or cur_range,
                kind=str(shot.get("kind") or "clip").strip().lower(),
                duration=float(shot.get("duration_target_sec") or 0) or 0.0))
    return out


@dataclass
class PlanStats:
    total: int = 0
    by_method: dict = field(default_factory=dict)

    @property
    def placed(self) -> int:
        return sum(v for k, v in self.by_method.items() if k != "none")

    @property
    def coverage(self) -> float:
        return self.placed / self.total if self.total else 0.0

    def summary(self) -> str:
        parts = ", ".join(f"{k}: {v}" for k, v in sorted(self.by_method.items()))
        return (f"{self.placed}/{self.total} shots placed "
                f"({self.coverage * 100:.0f}%) — {parts}")


def known_names(library: dict) -> list:
    """Every character name the catalogue knows, longest first so 'Walter
    White' is tried before 'Walt' when scanning a sentence."""
    names = {c for shot in library.values() for c in shot.characters}
    return sorted(names, key=lambda n: -len(n))


def requests_from_text(text: str, names: list | None = None) -> list:
    """Turn a plain narration script into shot-requests, one per sentence.

    A clean narration is prose about meaning, but a good essay's narration is
    also highly visual — "He steps into a red hazmat suit", "he picks up a box
    cutter" — so each sentence is a fair query for the footage that should sit
    under it. Any catalogue character named in the sentence becomes its
    character filter, which is what turns "he kills Victor" into a search that
    actually prefers Victor's shots.
    """
    names = names or []
    sentences = re.split(r"(?<=[.!?])\s+", (text or "").replace("\n", " "))
    out = []
    for i, s in enumerate(sentences, 1):
        s = s.strip()
        if len(s) < 12:                       # skip stubs and headers
            continue
        low = s.lower()
        found = [n for n in names if n.lower() in low
                 or n.split()[0].lower() in low.split()]
        out.append(Request(beat=i, visual=s, characters=found[:3]))
    return out


def plan(source, library: dict, scope: str = "") -> tuple:
    """(list of (Request, Match), PlanStats) for a whole script.

    `source` may be parsed genspark beats (a list of beat dicts) or a plain
    narration string — the retrieval is the same either way. `scope` confines
    the WHOLE script to one episode/title, which is what a single-scene essay
    (e.g. the box-cutter scene, all of it in S04E01) needs.
    """
    if isinstance(source, str):
        reqs = requests_from_text(source, known_names(library))
    else:
        reqs = requests_from_beats(source)
    pairs, stats = [], PlanStats()
    for req in reqs:
        m = match(req, library, scope=scope)
        pairs.append((req, m))
        stats.total += 1
        stats.by_method[m.method] = stats.by_method.get(m.method, 0) + 1
    return pairs, stats
