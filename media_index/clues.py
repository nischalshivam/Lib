"""Remembered dialogue, checked against the real subtitles before it counts.

The third script. `PROMPT_CLUE_SCRIPT.md` asks a chat model for one thing and
forbids it another:

    Recall dialogue. Never invent a timestamp.

That asymmetry is the whole idea, and it is measured, not stylistic. On a
real script, four of five *remembered timestamps* were wrong by seven to
fifteen minutes, and every shot depending on them landed in a different part
of the episode with nothing to say so. A remembered *line* cannot fail that
way: this module looks it up in the local subtitle file, and either gets a
millisecond back or gets nothing back. Wrong dialogue is discarded in a
second. Wrong timestamps survive into the video.

So nothing a clue says is believed here. Every field is a **hypothesis**,
and this module's job is to run each one past the subtitles and keep only
what came back:

  - a line found            -> `exact_dialogue` on a shot that had none,
                               which the aligner turns into a real anchor
  - two lines bracketing it -> a window bounded at both ends by timestamps
                               out of the subtitle file, for a scene where
                               nobody says anything at all
  - an episode              -> used **only** if one of that clue's lines was
                               actually found inside it
  - people on screen        -> passed to the face check, never to placement

Why enrich the visual script rather than place shots directly: every module
downstream — `align`, `verify`, `timings`, `tiers` — already knows how to
handle a shot that quotes a line. Adding a fifth placement path would mean
five things to keep honest instead of one. A clue's contribution is to make
the visual script say something true that it did not say before.

The measurement that motivates all of it, from the two scripts this was
written against — the same essay, the same narration, one visual script
written from the clean narration and one written from a clue script:

    from the clean script:  9 distinct quoted lines across 926 s  (1 / 103 s)
    from the clue script:  19 distinct quoted lines across 154 s  (1 / 8 s)

Twelve times the anchor density. That is not a better model; it is the same
model given the one thing it can be accurate about.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from . import search, subtitles
from .jobs import _documents, straighten

# How far either side of a bracketing line the scene may reach. `before` and
# `after` are described to the model as "shortly" before and after, and a
# scene that runs longer than this is one the clue has not really bounded.
BRACKET_PAD_S = 45.0
# A single bracketing line found on its own bounds nothing; it only says
# "near here". Wider than BRACKET_PAD_S because there is no other end.
SINGLE_PAD_S = 90.0
# Below this, a subtitle hit is not the line the clue meant.
MIN_SCORE = 62.0
# Below this share of shared words, a clue is not about this beat.
MIN_NARRATION_OVERLAP = 0.34
# Words that two unrelated sentences share for free.
STOP = frozenset("""a an and are as at be been but by for from had has have he
her him his i if in into is it its me my no not of on or our out she so than
that the their them then there they this to too up us was we were what when
which who will with would you your""".split())


@dataclass
class Clue:
    """One remembered moment, before anything has been checked."""
    clue_id: str = ""
    narration_covered: str = ""
    what_happens: str = ""
    episode: str = ""
    episode_confidence: str = ""
    silent: bool = False
    in_scene: list = field(default_factory=list)
    before: str = ""
    after: str = ""
    dialogue_confidence: str = ""
    on_screen: list = field(default_factory=list)
    mentioned: list = field(default_factory=list)
    location: str = ""
    visible: str = ""
    objects: list = field(default_factory=list)
    notes: str = ""

    @property
    def lines(self) -> list:
        """Every line this clue offers, most useful first.

        `in_scene` first because a line spoken *during* the moment places it
        outright; the brackets only bound it.
        """
        out = [str(x).strip() for x in self.in_scene if str(x).strip()]
        for edge in (self.before, self.after):
            if str(edge).strip():
                out.append(str(edge).strip())
        return out


class ClueError(Exception):
    """The file could not be read as a clue script."""


def _clue_from(raw: dict) -> Clue:
    def text(key):
        return str(raw.get(key) or "").strip()

    def names(key):
        got = raw.get(key) or []
        if isinstance(got, str):
            got = [got]
        return [str(n).strip() for n in got if str(n).strip()]

    in_scene = raw.get("dialogue_in_scene") or []
    if isinstance(in_scene, str):
        in_scene = [in_scene]
    return Clue(
        clue_id=text("clue_id"),
        narration_covered=text("narration_covered"),
        what_happens=text("what_happens"),
        episode=text("episode"),
        episode_confidence=text("episode_confidence"),
        silent=bool(raw.get("silent")),
        in_scene=[str(x).strip() for x in in_scene if str(x).strip()],
        before=text("dialogue_before"),
        after=text("dialogue_after"),
        dialogue_confidence=text("dialogue_confidence"),
        on_screen=names("characters_on_screen"),
        mentioned=names("characters_mentioned"),
        location=text("location"),
        visible=text("visible"),
        objects=names("objects"),
        notes=text("notes"),
    )


def read(path: str) -> list:
    """The clues in a `schema: clue-1` file.

    Straightened first, unconditionally, rather than only as a repair. A
    clue script is copied out of a chat window by hand — that is the only
    way it can be produced — and one of the two real ones this was written
    against had 2,373 typographic quotes and was not JSON at all. Repairing
    that on the second attempt would be the same answer with an extra step.
    """
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            raw = f.read()
    except OSError as exc:
        raise ClueError(str(exc)) from exc

    docs = None
    for attempt in (raw, straighten(raw)):
        try:
            docs = _documents(attempt)[0]
            break
        except json.JSONDecodeError:
            continue
    if docs is None:
        raise ClueError("ye file JSON nahi hai — clue script chahiye, "
                        "schema \"clue-1\" wali")

    found = []
    for doc in docs:
        if isinstance(doc, dict) and isinstance(doc.get("clues"), list):
            found = doc["clues"]
            break
        if isinstance(doc, list) and doc and isinstance(doc[0], dict) \
                and "clue_id" in doc[0]:
            found = doc
            break
    if not found:
        raise ClueError("is file me koi \"clues\" list nahi mili")
    return [_clue_from(c) for c in found if isinstance(c, dict)]


# ---------------------------------------------------------------------------
# which beat is a clue talking about
# ---------------------------------------------------------------------------

def _words(text: str) -> set:
    return {w for w in re.findall(r"[a-z']+", (text or "").lower())
            if len(w) > 2 and w not in STOP}


def _overlap(a: set, b: set) -> float:
    """Share of the smaller sentence's real words that both sentences use.

    Not Jaccard. A clue covering three sentences of narration is matched
    against one beat holding one of them, and Jaccard punishes that
    correctness for being uneven — which is precisely the shape the prompt
    asks for ("one entry per SCENE, not per sentence").
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def match(beats: list, clues: list) -> dict:
    """{beat number: Clue} — each beat gets at most the one clue that fits it.

    Joined on the narration text, because that is the one thing the two
    files provably share: the clue script's `narration_covered` is copied
    out of the same clean script the visual script's `narration` is. No
    ordering is assumed between them; a clue script written for a 24-beat
    script and a visual script cut into 59 beats must still line up.
    """
    scored = []
    for clue in clues:
        want = _words(clue.narration_covered) or _words(clue.what_happens)
        for beat in beats or []:
            no = beat.get("beat")
            if no is None:
                continue
            got = _overlap(want, _words(beat.get("narration") or ""))
            if got >= MIN_NARRATION_OVERLAP:
                scored.append((got, no, clue))

    out: dict = {}
    for got, no, clue in sorted(scored, key=lambda s: -s[0]):
        out.setdefault(no, clue)
    return out


# ---------------------------------------------------------------------------
# checking a remembered line against the actual subtitle file
# ---------------------------------------------------------------------------

@dataclass
class Found:
    line: str
    hit: search.Hit

    @property
    def episode_key(self) -> tuple | None:
        if self.hit.season is not None and self.hit.episode is not None:
            return (self.hit.season, self.hit.episode)
        return None


def sentences(line: str) -> list:
    """A remembered line as the separate things a subtitle file holds.

    A model recalls a moment and writes it out whole:

        "Been watching him for weeks. I know every step of his cook."

    The subtitle file has those as two cues, seconds apart, and searching
    for both at once finds neither cleanly — the fragment retry then goes
    looking and can land in a different episode entirely. Split, each half
    is an ordinary search that either works or does not.

    Short fragments are dropped rather than searched. "Couldn't." appears
    hundreds of times in a season and a hit on it means nothing.
    """
    parts = [p.strip(" -–—") for p in re.split(r"(?<=[.!?])\s+", line or "")]
    return [p for p in parts if len(p.split()) >= 4]


def _look_up(db: str, line: str, show: str = "", episode: str = "") -> Found | None:
    """The line in the real subtitles, or nothing. Never an approximation.

    Searched inside the clue's episode when it named one, and across the
    whole library when it did not — but a hit found elsewhere still counts,
    and is what corrects a wrongly remembered episode instead of inheriting
    it.

    The whole line is tried first, because it is the most specific thing
    available and a hit on it is the strongest. Only when that fails is it
    broken into sentences — the shape the subtitle file actually stores.
    """
    # Checked rather than caught: sqlite *creates* a database it was asked
    # to open, so a mistyped library path would leave an empty .db beside
    # the tool and every lookup would come back honestly empty forever.
    if not os.path.isfile(db):
        return None
    key = subtitles.episode_key(episode or "") if episode else None
    where = [{"season": key[0], "episode": key[1]}] if key else []
    where.append({})

    def ask(text):
        for scope in where:
            try:
                hits = search.find(db, text, show=show or None, limit=1,
                                   min_score=MIN_SCORE, **scope)
            except Exception:                   # a missing db is not fatal
                return None
            if hits:
                return Found(line=text, hit=hits[0])
        return None

    got = ask(line)
    if got:
        return got
    parts = sentences(line)
    if len(parts) < 2:
        return None
    for part in parts:
        got = ask(part)
        if got:
            return got
    return None


@dataclass
class Enrichment:
    """What the clues actually managed to prove, and what they did not."""
    quotes_added: int = 0
    brackets_added: int = 0
    episodes_filled: int = 0
    people_filled: int = 0
    lines_checked: int = 0
    lines_found: int = 0
    episodes_corrected: list = field(default_factory=list)
    windows: dict = field(default_factory=dict)   # {(beat, shot): (lo, hi)}
    unmatched: list = field(default_factory=list)  # clues no beat wanted

    @property
    def hit_rate(self) -> float:
        return self.lines_found / self.lines_checked if self.lines_checked else 0.0

    def summary(self) -> str:
        if not self.lines_checked:
            return "  clue script: koi line check karne layak nahi mili"
        return (f"  clue script: {self.lines_found}/{self.lines_checked} line "
                f"subtitle me mili ({self.hit_rate * 100:.0f}%) "
                f"{chr(183)} {self.quotes_added} shot ko asli quote mila "
                f"{chr(183)} {self.brackets_added} run ko dono taraf se bandha")


def _bracket(a: Found | None, b: Found | None) -> tuple | None:
    """A window from whichever bracketing lines were actually found.

    Both ends is the case worth having: two timestamps out of the subtitle
    file, and the silent scene between them is bounded by measurement rather
    than by anybody's opinion. One end is worth much less and is padded to
    say so.
    """
    if a and b and a.episode_key == b.episode_key:
        lo = min(a.hit.start_ms, b.hit.start_ms) / 1000.0 - BRACKET_PAD_S
        hi = max(a.hit.end_ms, b.hit.end_ms) / 1000.0 + BRACKET_PAD_S
        return (max(0.0, lo), hi)
    only = a or b
    if only:
        at = only.hit.start_ms / 1000.0
        return (max(0.0, at - SINGLE_PAD_S), at + SINGLE_PAD_S)
    return None


def _spread(found: list, beats: list) -> dict:
    """{beat number: [lines to place there]} — k lines across n beats, evenly.

    The bug this function is the answer to, in one line of a real log:

        Breaking Bad S04E01: 31 shot(s), 1 anchor(s)

    Thirty-one shots got a real quoted line and exactly one of them survived
    alignment. The clue script was right; the placement was not. One clue in
    that script covered ten beats of narration — the whole box-cutter
    sequence is one scene — and its three remembered lines were written into
    the shots of *every* one of those ten beats. The same line then claimed
    to be at ten different points of the run, the aligner correctly saw a
    sequence implying 398x the pace of the script around it, and it threw
    almost all of them away. Correct behaviour, from the aligner, in
    response to nonsense it had been handed.

    A clue is one scene, and its lines are spoken in the order given, once
    each. So they are spread through the beats that scene covers — the first
    line near the start, the last near the end — which is also exactly the
    shape the aligner wants: anchors apart, silence interpolated between.

    Spread over SLOTS, not over beats, and that is the second half of the
    fix. Spreading over beats picked beat 1, 5 and 10 and dropped any line
    whose beat happened to have no empty shot — the visual script usually
    quotes something already. The next build found 84 of 85 lines and placed
    thirteen. A slot is a shot with nothing in it, so every line that was
    found now lands somewhere.
    """
    if not found or not beats:
        return {}
    out: dict = {}
    last = len(beats) - 1
    for i, item in enumerate(found):
        at = 0 if len(found) == 1 else round(i * last / (len(found) - 1))
        out.setdefault(beats[at], []).append(item)
    return out


def _trustworthy(got: "Found") -> bool:
    """Is this hit strong enough to overrule the episode a clue named?

    A high bar on purpose. Moving a beat to a different episode on the
    strength of a fuzzy match is the most damaging single thing this module
    can do, and "Son of a bitch." matches somewhere in almost every hour of
    television ever made. A confident, unique hit may overrule; anything
    less leaves the clue's own answer alone and simply contributes nothing.
    """
    return got.hit.confidence == "high" and not got.hit.alternatives


def enrich(db: str, beats: list, clues: list, log=lambda *a: None) -> Enrichment:
    """Fill in what the clues can prove, in place, and report what they could not.

    Only ever fills fields that are **empty**. A visual script that already
    quotes a line for a shot has an editor's or a model's answer there
    already, and overwriting it with a remembered one would trade something
    checked for something recalled. The clue script exists for the 87% of
    shots that had nothing.

    Works clue by clue rather than beat by beat, because a clue is a scene
    and a scene routinely spans many beats — ten, in the script this was
    fixed against. Its lines are looked up once and placed once.
    """
    out = Enrichment()
    if not beats or not clues:
        return out

    paired = match(beats, clues)
    wanted = {id(c) for c in paired.values()}
    out.unmatched = [c.clue_id or c.what_happens[:40]
                     for c in clues if id(c) not in wanted]
    if not paired:
        log("  clue script: koi bhi clue kisi beat se match nahi hua — "
            "narration_covered wahi text hona chahiye jo visual script me hai")
        return out

    by_no = {b.get("beat"): b for b in beats if b.get("beat") is not None}
    order = [b.get("beat") for b in beats if b.get("beat") is not None]
    mine: dict = {}
    for no in order:
        clue = paired.get(no)
        if clue is not None and (by_no[no].get("shots") or []):
            mine.setdefault(id(clue), []).append(no)

    cache: dict = {}
    quotes: dict = {}          # {(beat, shot): Found}  placed once each
    edges: dict = {}           # {beat: (Found, "before"|"after")}
    windows: dict = {}         # {beat: (lo, hi)}
    facts: dict = {}           # {beat: (episode, clue)}

    for clue in clues:
        where = mine.get(id(clue)) or []
        if not where:
            continue
        first = by_no[where[0]]["shots"][0]
        show = (first.get("source") or "").strip()

        def check(line):
            if not line:
                return None
            key = (line.lower(), show, clue.episode)
            if key not in cache:
                out.lines_checked += 1
                got = _look_up(db, line, show=show, episode=clue.episode)
                if got:
                    out.lines_found += 1
                cache[key] = got
            return cache[key]

        inside = [f for f in (check(l) for l in clue.in_scene) if f]
        before, after = check(clue.before), check(clue.after)
        window = _bracket(before, after)

        # The episode the clue named counts only where one of its own lines
        # turned up inside an episode, and only when that hit is strong
        # enough to be worth more than the clue's own answer.
        proven = next((f for f in inside + [before, after]
                       if f and _trustworthy(f)), None)
        proven_se = ""
        if proven and proven.episode_key:
            season, ep = proven.episode_key
            proven_se = f"S{season:02d}E{ep:02d}"
            if clue.episode and subtitles.episode_key(clue.episode) \
                    != proven.episode_key:
                # Once per clue, not once per beat it happens to cover.
                out.episodes_corrected.append(
                    f"{clue.clue_id or 'clue'}: kaha {clue.episode}, "
                    f"line mili {proven_se} me")

        # Every shot of this scene that has nothing quoted on it yet, in
        # script order. Lines are spread over THESE, not over the beats: a
        # beat whose shots the visual script already filled has no room, and
        # spreading over beats silently threw those lines away.
        slots = [(no, i) for no in where
                 for i, shot in enumerate(by_no[no].get("shots") or [], 1)
                 if not (shot.get("exact_dialogue") or "").strip()]
        for slot, got in _spread(inside, slots).items():
            quotes[slot] = got[0]
        # A line spoken *before* the scene belongs at its start and a line
        # spoken after it at its end — putting either on all ten beats is
        # the same duplication that broke the quotes.
        if before:
            edges[where[0]] = (before, "before")
        if after:
            edges.setdefault(where[-1], (after, "after"))
        for no in where:
            facts[no] = (proven_se, clue)
            if window:
                windows[no] = window
        if window and before and after:
            out.brackets_added += 1

    for no, beat in by_no.items():
        proven_se, clue = facts.get(no, ("", None))
        if clue is None:
            continue
        edge = edges.get(no)
        for i, shot in enumerate(beat.get("shots") or [], 1):
            if not (shot.get("season_episode") or "").strip() and proven_se:
                shot["season_episode"] = proven_se
                shot["se_confidence"] = "high"
                out.episodes_filled += 1
            got = quotes.get((no, i))
            if got and not (shot.get("exact_dialogue") or "").strip():
                shot["exact_dialogue"] = got.line
                shot["dialogue_confidence"] = got.hit.confidence
                out.quotes_added += 1
            elif edge and not (shot.get("nearest_dialogue") or "").strip():
                shot["nearest_dialogue"], shot["nearest_dialogue_position"] = (
                    edge[0].line, edge[1])
            if not (shot.get("characters") or []) and clue.on_screen:
                shot["characters"] = list(clue.on_screen)
                out.people_filled += 1
            if not (shot.get("visual") or "").strip() and clue.visible:
                shot["visual"] = clue.visible
            if no in windows:
                out.windows[(no, i)] = windows[no]

    log(out.summary())
    if out.episodes_corrected:
        log(f"    {len(out.episodes_corrected)} clue ka episode galat tha, "
            "subtitle ne sahi bata diya:")
        for line in out.episodes_corrected[:5]:
            log(f"      {line}")
    if out.unmatched:
        log(f"    {len(out.unmatched)} clue kisi beat se nahi juda "
            "(narration_covered milta nahi)")
    return out
