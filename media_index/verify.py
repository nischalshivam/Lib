"""Check every placement against the picture, and fix the ones that are wrong.

Alignment places shots by *inference*: find a quoted line, then lay the rest
of the run along the scene in script order. On the real Breaking Bad script
that meant 13 of 287 assets rested on evidence and 274 were inherited from
them — and when a single anchor was wrong, all 274 were wrong with it. Three
different builds were ruined that way, each by a different bad anchor, and
each was patched with a new guard against that one shape of mistake.

Guards do not generalise. The next script brings a new shape.

What generalises is *looking*. Every shot in a scene breakdown carries a
written description of what should be on screen — "Gus, in the red hazmat
suit, ties the apron with quiet care" — and `visual.py` can score that
description against every sampled frame of the episode. So a placement no
longer has to be believed. It can be checked, and moved when it is wrong.

## Why the whole run is solved at once

The obvious version — take each shot, search near where alignment put it,
keep the best frame — fails in a specific and ugly way. Shots are decided
independently, so nothing stops six of them landing on the same striking
frame, or shot 40 landing before shot 12. A scene reassembled out of order
is worse than one that is merely offset, because an offset is one mistake and
a scramble is forty.

So the run is solved as one problem: choose a frame for every shot, in
increasing time order, maximising the total visual agreement. That is a
shortest-path over a grid of (shot x frame), and it has three properties that
matter more than the optimisation itself:

  * **order is structural.** Monotonicity is a constraint, not a hope.
  * **a shot with no visual signal is not stranded.** It cannot wander, so it
    settles between its neighbours — the same thing interpolation did, but
    now positioned by shots that were actually verified rather than by one
    anchor at the far end of the run.
  * **anchors are pinned, not trusted.** A line matched in the subtitles is a
    real millisecond and stays fixed. A line matched weakly is allowed to be
    outvoted by forty shots that all agree the scene is somewhere else.

Alignment is still used, but as a *window* rather than a vote. A quoted line
says which stretch of the episode a run belongs to; the pictures say which
frame inside it. Letting either do the other's job broke a build each way —
a soft prior overruled pictures that had genuinely matched, and removing it
altogether let ninety-one shots spread across a whole episode.

## Being sure it was found at all

Two shots in three carry a description the model cannot place. That is
normal — "he thinks about what he has done" is not a picture — and those
shots are meant to sit between the ones that were placed, not to choose for
themselves.

Deciding which is which needs care, because the best of fourteen hundred
scores is high for *any* caption, including one about nothing in this film.
So the bar is measured on the episode itself: score a set of captions about
unrelated things, see how high they get, and require a shot to beat that.
Chance still leaks through a shot at a time, so the run is asked the same
question — find more than a quarter of yourself, or find one thing outright.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np

from . import align, cast, embed, visual

# How much the alignment prior is worth, in the same units as visual lift.
# Deliberately small. A shot 90 seconds from where alignment expected it pays
# this much — enough to settle a tie between two indistinguishable frames,
# nowhere near enough to overrule a description that genuinely matched.
PRIOR_WEIGHT = 0.5
PRIOR_TAU_S = 90.0
# An anchored shot may be nudged this far to land on a sampled frame, and no
# further: the subtitle timing is the ground truth it is standing on.
ANCHOR_TOLERANCE_S = 2.5
# The least a run may wander either side of where alignment put it, whatever
# its own length. A four-shot run claiming eighteen seconds would otherwise be
# confined to eighteen seconds, which is fewer sampled frames than it has
# shots — a window so tight it is a pin by another name.
MIN_REACH_S = 120.0
# The least source time two shots of the same run may be placed apart.
#
# Below this they are not two shots, they are one picture used twice: the
# footage is sampled every two seconds, so nothing in this tool can even tell
# two moments a second apart from each other. It is the smallest number that
# means anything, which is what makes it safe as a structural constraint
# rather than a taste one.
MIN_APART_S = 2.0
# Only shots that HAVE a match anywhere get to choose a frame. The rest are
# interpolated between them.
#
# This is the constraint that was missing, and its absence is what put a box
# cutter six minutes from the sentence describing it. The solver maximised
# the total match and nothing else, so a shot matching nothing was free to
# sit anywhere the ordering allowed — and with fifty-five such shots the best
# path is simply to spread them over everything available. Ninety-one shots
# of a hundred-second sequence ended up across twenty minutes of episode.
#
# A penalty on the run's total span was the first attempt and it was wrong:
# it cannot tell a run that spread out because it MATCHED things far apart
# from one that spread out because it matched nothing. The three runs placed
# on pictures alone legitimately cover twenty minutes of their episodes, and
# a span penalty strong enough to fix the first crushes those.
#
# Choosing which shots may choose is the distinction that actually exists.
NEG = -1e9

# Captions about nothing in particular, used to ask each episode how high a
# description that does NOT belong to it scores anyway.
#
# A fixed threshold cannot answer that, because the best of N scores rises
# with N. Measured on the fake model: a caption matching nothing reached a
# lift of 1.7 against 400 frames, 2.0 against 1,200 and 2.1 against 3,000 —
# all of them above the 1.2 that means "found". A 47-minute episode sampled
# twice a second is 1,400 frames, so on the real builds every shot in the
# script cleared the bar by luck alone, and "only matched shots may choose"
# stopped meaning anything.
#
# These are unrelated to any particular film on purpose, and there are enough
# of them that the two highest can be thrown away — one of them landing on a
# real kitchen or a real staircase should not raise the bar for everyone.
CONTROLS = (
    "a snow-covered mountain under a clear sky",
    "a bowl of soup on a wooden table",
    "a rocket lifting off from a launch pad",
    "a busy fish market at dawn",
    "a violin resting on a velvet cushion",
    "a herd of elephants crossing a river",
    "a spiral staircase in an empty library",
    "a surfboard planted in the sand",
    "a chessboard mid-game beside a lamp",
    "a tractor ploughing a muddy field",
    "a glass of orange juice on a windowsill",
    "a lighthouse in heavy fog",
    "a knitted scarf hanging on a hook",
    "a satellite dish on a flat roof",
    "a plate of pancakes with syrup",
    "a bicycle leaning against a brick wall",
)
CONTROL_DISCARD = 2          # how many of the highest controls to ignore
_FLOORS: dict = {}


def describe(shot: dict) -> str:
    """The sentence a frame is scored against.

    The visual line is the caption the model actually understands. Setting
    and characters are appended because they cost nothing — SigLIP reads 64
    tokens and these descriptions rarely reach 30 — and because a proper noun
    from a well-known show is sometimes recognised outright.

    `must_not_have` is deliberately ignored. It exists to keep reaction cams
    and fan art out of a web search, and every frame scored here already came
    out of the film.
    """
    parts = [str(shot.get("visual") or "").strip()]
    setting = str(shot.get("setting") or "").strip()
    if setting:
        parts.append(setting)
    chars = shot.get("characters") or []
    if isinstance(chars, (list, tuple)) and chars:
        parts.append(", ".join(str(c) for c in chars))
    return ". ".join(p for p in parts if p)


@dataclass
class Verdict:
    beat: int
    shot: int
    action: str = "unchecked"    # kept | moved | pinned | drifted | unchecked
    before_ms: int = 0
    after_ms: int = 0
    lift: float = 0.0
    # The best this description scored ANYWHERE in the episode, ignoring
    # order and ignoring where alignment expected it. Without this, a low
    # `lift` has two completely different meanings that look identical:
    # the model could not find the picture at all, or it found it and the
    # ordering constraint gave that frame to a neighbour. Those need
    # opposite fixes — better captions versus a looser solver — so a build
    # that cannot tell them apart cannot be acted on.
    best: float = 0.0
    # Whether, at that best frame, this description beats every OTHER
    # description in the run — that is, whether the frame is unambiguously
    # this shot's rather than one several shots half-explain.
    #
    # It does NOT remove chance, and it was written here as though it did.
    # Measured: twelve captions matching nothing at all, against twelve
    # hundred frames, came back eleven-of-twelve "distinct" — because a
    # shot's best frame is by construction the one where its own luck peaked,
    # and the others sit at their average there. Only `bar` answers chance.
    distinct: bool = False
    # What counted as found in this episode: `visual.LIFT_OK`, or the
    # episode's own noise floor when that is higher. Carried on the verdict
    # so the report is scored by the same bar the solver used.
    bar: float = visual.LIFT_OK
    note: str = ""

    @property
    def moved_seconds(self) -> float:
        return abs(self.after_ms - self.before_ms) / 1000.0

    @property
    def lost_to_ordering(self) -> bool:
        """It could have been found, and the ordering took it away.

        Both halves are needed. `best` alone counts a caption that merely
        won a lottery over 1,400 frames; `distinct` alone counts a caption
        that beat its rivals at a frame none of them actually matched.
        Together they mean: there was a real frame for this shot, it was
        unambiguously this shot's, and something else got it.
        """
        return (self.distinct and self.best >= self.bar
                and self.lift < self.bar)


@dataclass
class Report:
    verdicts: list = field(default_factory=list)
    checked: int = 0
    moved: int = 0
    unmatched: int = 0
    findable: int = 0            # had a match somewhere in the episode
    lost_to_ordering: int = 0    # ...and did not keep it
    runs_without_index: list = field(default_factory=list)
    # Runs where nothing beat the episode's noise floor, so the pictures said
    # nothing and alignment was left to stand. Worth a number of its own: if
    # this is most of the script, the bar is wrong or the descriptions are,
    # and either way the stage is doing no work and should say so rather than
    # look like it agreed with everything.
    runs_left_alone: int = 0
    runs_seen: int = 0
    floors: list = field(default_factory=list)
    reason: str = ""             # why nothing was checked, if nothing was

    def summary(self) -> str:
        from . import term
        d = term.sym("dot")
        if self.reason:
            return f"  pictures not checked — {self.reason}"
        big = sum(1 for v in self.verdicts if v.action == "moved"
                  and v.moved_seconds >= 5.0)
        lines = [f"  {self.checked} shot(s) checked against the picture {d} "
                 f"{self.moved} moved ({big} by 5s or more) {d} "
                 f"{self.unmatched} with no matching frame"]
        if self.unmatched:
            # The one number that says WHICH fix is needed.
            lines.append(
                f"  of those {self.unmatched}, {self.lost_to_ordering} did "
                "have a match elsewhere in the episode and lost it to the "
                "ordering; " f"{self.unmatched - self.lost_to_ordering} "
                "matched nothing anywhere — those need better descriptions, "
                "not a looser solver")
        if self.runs_left_alone:
            floor = (f", where an unrelated caption already scores "
                     f"{max(self.floors):.1f}" if self.floors else "")
            lines.append(
                f"  {self.runs_left_alone} of {self.runs_seen} scene(s) found "
                "nothing above chance and were left where alignment put them"
                + floor)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# the solver
# ---------------------------------------------------------------------------

def lift_matrix(index: visual.VisualIndex, texts: list, backend) -> np.ndarray:
    """(shots x frames), each row in this episode's own lift units.

    Rows are normalised independently because raw similarity varies with the
    length and wording of a description far more than with whether it is
    right. Without this, one verbose shot would outbid every terse one and
    the solver would bend the whole run around it.
    """
    vecs = backend.encode_texts(texts)
    if not len(vecs) or not len(index):
        return np.zeros((len(texts), len(index)), dtype=np.float32)
    sims = np.asarray(vecs, dtype=np.float32) @ index.vecs.T
    med = np.median(sims, axis=1, keepdims=True)
    p95 = np.percentile(sims, 95, axis=1, keepdims=True)
    spread = np.maximum(p95 - med, 1e-6)
    out = (sims - med) / spread
    # A description that could not be encoded scores nothing, rather than
    # scoring noise that the solver would happily follow.
    dead = ~np.any(vecs, axis=1)
    out[dead, :] = 0.0
    return out.astype(np.float32)


def interpolate(times: list, axis: list, placed: dict,
                apart: float = 0.0) -> list:
    """Fill the shots nobody could place, between the ones somebody could.

    `placed` maps a shot's index to its chosen time. Everything else lands in
    proportion to the script's own axis — the same idea alignment has always
    used, except the fixed points are now shots whose picture was actually
    found rather than one quoted line at the far end of the run.

    Outside the placed range there is nothing to interpolate between, so the
    shots there keep the spacing ALIGNMENT gave them, hung off the nearest
    placed shot. Alignment is a good guess about pacing and a bad one about
    absolute position, so its shape is worth keeping even when its position
    is not. Two earlier versions did worse: extrapolating a rate flung shots
    to the end of the episode, and holding them all ON the nearest placed
    time is one picture shown five times.

    `apart` is the least two shots may be placed apart. Nothing here can
    invent room that is not there, but the solver has already guaranteed it:
    two chosen shots are kept far enough apart to hold everything between
    them.
    """
    if not placed:
        return list(times)
    keys = sorted(placed)
    first, last = keys[0], keys[-1]
    out = list(times)
    for i in range(len(times)):
        if i in placed:
            out[i] = placed[i]
            continue
        before = [k for k in keys if k < i]
        after = [k for k in keys if k > i]
        if not before:
            out[i] = placed[first] - max(0.0, times[first] - times[i])
        elif not after:
            out[i] = placed[last] + max(0.0, times[i] - times[last])
        else:
            a, b = before[-1], after[0]
            span = axis[b] - axis[a]
            frac = (axis[i] - axis[a]) / span if span > 0.01 else 0.5
            out[i] = placed[a] + frac * (placed[b] - placed[a])
    if apart <= 0:
        return out
    # The proportional pass follows the script's shape, which is uneven: two
    # short shots side by side can still land on the same second even when
    # the stretch as a whole has room. Walk it forward and hold everything
    # `apart`, without moving a shot that was actually found and without
    # crowding out the ones still to come.
    for j in range(len(keys) - 1):
        a, b = keys[j], keys[j + 1]
        for i in range(a + 1, b):
            lo = out[i - 1] + apart
            hi = placed[b] - (b - i) * apart
            if hi < lo:                       # no room: share it out evenly
                out[i] = placed[a] + (i - a) * (placed[b] - placed[a]) / (b - a)
            else:
                out[i] = min(max(out[i], lo), hi)
    for i in range(first - 1, -1, -1):        # before the first one found
        out[i] = max(0.0, min(out[i], out[i + 1] - apart))
    for i in range(last + 1, len(out)):       # after the last one found
        out[i] = max(out[i], out[i - 1] + apart)
    return out


def prior_matrix(times: np.ndarray, wanted_s: np.ndarray) -> np.ndarray:
    """A soft pull towards where alignment expected each shot."""
    d = (times[None, :] - wanted_s[:, None]) / PRIOR_TAU_S
    return (-PRIOR_WEIGHT * np.minimum(d * d, 9.0)).astype(np.float32)


def solve(score: np.ndarray, bounds: list, gaps: list | None = None,
          tail: int = 0) -> list:
    """Pick one frame per shot, in increasing time, best total score.

    `bounds[i]` is None, or an inclusive (lo, hi) range of frame indices that
    shot i is pinned inside. Returns one frame index per shot.

    `gaps[i]` is how many frames shot i must sit after shot i-1, and it is
    what stops a run collapsing. `gaps[0]` is how many frames must come
    before the first shot and `tail` how many must follow the last: the
    shots interpolated at either end need room too, and without it the first
    chosen shot can land on second zero of the episode with two shots still
    to fit before it.

    Only some shots choose; the rest are
    interpolated between them, so two chosen shots with sixty interpolated
    shots between them have to be far enough apart to HOLD sixty shots. When
    they were merely required to be in order, six near-chance matches landed
    within forty seconds of each other and the sixty shots between them were
    spread across those forty seconds — thirty-one of the first sixty-six
    pictures in a real build came out of one six-second stretch of episode,
    which on screen is the same shot over and over.

    O(shots x frames): the prefix maximum of the previous row is accumulated
    rather than re-searched, so a 147-shot script against a 1,400-frame
    episode is a fifth of a second, not four minutes.
    """
    n, N = score.shape
    if n == 0 or N == 0 or n > N:
        return []
    steps = [0] + [1] * (n - 1)
    if gaps:
        steps[0] = max(0, int(gaps[0]))
        for i in range(1, min(n, len(gaps))):
            steps[i] = max(1, int(gaps[i]))
    tail = max(0, int(tail))
    if sum(steps) + tail >= N:
        return []                   # the run cannot fit here at all

    def masked(i):
        row = score[i].astype(np.float64).copy()
        b = bounds[i] if i < len(bounds) else None
        if b is not None:
            lo, hi = b
            if lo > hi or lo >= N or hi < 0:
                return row              # an impossible pin is no pin at all
            keep = np.zeros(N, dtype=bool)
            keep[max(0, lo):min(N, hi + 1)] = True
            row[~keep] = NEG
        return row

    back = np.zeros((n, N), dtype=np.int32)
    prev = masked(0)
    room = steps[0]
    if room:
        prev[:room] = NEG
    for i in range(1, n):
        g = steps[i]
        run_max = np.maximum.accumulate(prev)
        fresh = np.empty(N, dtype=bool)
        fresh[0] = True
        fresh[1:] = run_max[1:] > run_max[:-1]
        arg = np.where(fresh, np.arange(N), 0)
        arg = np.maximum.accumulate(arg)

        shifted = np.full(N, NEG)
        shifted[g:] = run_max[:N - g]
        back[i, g:] = arg[:N - g]
        room += g
        cur = masked(i) + shifted
        cur[:room] = NEG            # no room for the shots that come first
        prev = cur

    if tail:
        prev[N - tail:] = NEG
    end = int(np.argmax(prev))
    if prev[end] <= NEG / 2:
        return []                       # no legal assignment exists
    path = [0] * n
    path[n - 1] = end
    for i in range(n - 1, 0, -1):
        path[i - 1] = int(back[i, path[i]])
    return path


# ---------------------------------------------------------------------------
# applying it to a build
# ---------------------------------------------------------------------------

def _distinct_at_best(score: np.ndarray) -> np.ndarray:
    """Per shot: at its own best frame, does it beat every other caption?

    Rows are already z-like — each is measured against its own episode-wide
    spread — so a column compares captions fairly. A shot that wins its own
    best frame is one the model can genuinely tell apart from the rest of
    the script; a shot that loses it was never distinguishable, however high
    its raw score happened to be.

    A run of one or two shots has nothing to compare against, so nothing is
    claimed for it.
    """
    n = score.shape[0] if score.size else 0
    if n < 3:
        return np.zeros(n, dtype=bool)
    peak = score.argmax(axis=1)
    mine = score[np.arange(n), peak]
    best_any = score[:, peak].max(axis=0)
    return mine >= best_any - 1e-6


def noise_floor(index: visual.VisualIndex, backend, inside=None) -> float:
    """How high a caption that does not belong here scores anyway.

    The answer depends on the episode and on how many of its frames are being
    searched, so it is measured rather than assumed: score a fixed set of
    captions about unrelated things, take each one's best frame, and report
    near the top of what those reach. A shot that cannot beat that has not
    been found — it has merely been searched for a long time.

    The two highest controls are discarded first. On a domestic drama one of
    them will occasionally describe a real frame, and a bar set by a genuine
    match is a bar no honest shot can clear.

    The controls are scored against the episode once and the rows kept, not
    the answer: every run wants a different stretch, so a cache of finished
    floors would miss on all but the first and re-encode sixteen captions per
    run. The rows are 16 x N floats — ninety kilobytes for a long episode.
    """
    if not len(index):
        return 0.0
    key = (getattr(index, "path", ""), getattr(index, "model", ""),
           len(index), getattr(backend, "name", ""))
    rows = _FLOORS.get(key)
    if rows is None:
        rows = lift_matrix(index, list(CONTROLS), backend)
        _FLOORS[key] = rows
    if not rows.size:
        return 0.0
    if inside:
        lo, hi = inside
        rows = rows[:, max(0, lo):hi + 1]
    if not rows.size:
        return 0.0
    best = np.sort(rows.max(axis=1))
    keep = best[:-CONTROL_DISCARD] if len(best) > CONTROL_DISCARD else best
    return float(keep[-1]) if len(keep) else 0.0


def _bounds_for_window(times: np.ndarray, window) -> tuple:
    """Frame indices covering a stretch of the episode, or None for all of it."""
    if not window:
        return None
    lo = int(np.searchsorted(times, window[0], side="left"))
    hi = int(np.searchsorted(times, window[1], side="right")) - 1
    if hi < lo:
        return None                 # the window fell outside the footage
    return lo, hi


def _bounds_for_anchor(times: np.ndarray, at_s: float) -> tuple:
    lo = int(np.searchsorted(times, at_s - ANCHOR_TOLERANCE_S, side="left"))
    hi = int(np.searchsorted(times, at_s + ANCHOR_TOLERANCE_S, side="right")) - 1
    if hi < lo:                          # no sampled frame that close
        nearest = int(np.argmin(np.abs(times - at_s)))
        return nearest, nearest
    return lo, hi


def verify_run(index: visual.VisualIndex, run, placements: list, backend,
               log=lambda *a: None) -> list:
    """Re-place one run using the pictures. Mutates `placements` in place.

    Hook shots take no part. A hook quotes a line out of sequence — that is
    what makes it a hook — so including one would either break the ordering
    constraint outright or drag every shot around it to keep the order legal.
    It is already sitting on a real, matched line, and it stays there.
    """
    ordered = [i for i, e in enumerate(run.entries) if not e.is_hook]
    out = [Verdict(beat=p.beat, shot=p.shot, action="pinned",
                   before_ms=p.start_ms, after_ms=p.start_ms,
                   note="a hook quote — left on the line it matched")
           for p in placements]
    if not ordered:
        return out

    texts = [describe(run.entries[i].data) for i in ordered]
    if not any(texts):
        for i in ordered:
            out[i] = Verdict(beat=placements[i].beat, shot=placements[i].shot,
                             note="no description to check against")
        return out

    score = lift_matrix(index, texts, backend)
    wanted = np.array([placements[i].start_ms / 1000.0 for i in ordered],
                      dtype=np.float32)
    # An anchor answers WHICH STRETCH of the episode. The pictures answer
    # WHICH FRAME inside it. Neither is allowed to do the other's job, and
    # two builds went wrong by letting one of them try.
    #
    # A soft prior let the anchor decide frames: 19 of 91 shots kept a match
    # they had found, because the pull towards one extrapolated point beat
    # the picture that actually matched. Removing it entirely was worse. With
    # 91 shots that must fall in increasing time order and a weak per-shot
    # signal, the best path is simply to spread them evenly over everything
    # available — so a run belonging to a six-minute scene at 30 minutes was
    # laid across the whole 47-minute episode, starting at 56 seconds.
    #
    # So the anchor gives a hard window and no vote inside it. A run may
    # wander by at most its own planned length from where alignment put it,
    # which cannot reach another sequence and cannot pin a single frame.
    grounded = sum(1 for i in ordered if placements[i].method == "anchor")
    total = score
    window = None
    if grounded:
        lo = min(wanted) if len(wanted) else 0.0
        hi = max(wanted) if len(wanted) else 0.0
        reach = max(hi - lo, MIN_REACH_S)
        window = (lo - reach, hi + reach)
        log(f"      {run.label}: held inside {window[0]:.0f}s-{window[1]:.0f}s "
            "by its quoted line; the pictures choose the frames within it")

    inside = _bounds_for_window(index.times, window) if window else None
    # Two different things, and conflating them cost a whole build: `bounds`
    # is where the solver may look, `held` is whether the shot is standing on
    # a quoted line. Once the window started filling `bounds` for every shot,
    # "has bounds" stopped meaning "is an anchor" — and every shot in the run
    # was treated as pinned and never moved at all.
    bounds, held = [], []
    for i in ordered:
        p = placements[i]
        anchor = p.method == "anchor" and p.confidence in ("high", "medium")
        held.append(anchor)
        # An anchor's pin is tighter than the window and wins.
        bounds.append(_bounds_for_anchor(index.times, p.start_ms / 1000.0)
                      if anchor else inside)

    # Only shots with a match somewhere may choose a frame. A shot that
    # matched nothing has no opinion, and letting it vote is what spread a
    # hundred-second sequence over twenty minutes.
    #
    # "A match" is measured against this episode's own noise floor, over the
    # same frames the shot is allowed to occupy. A fixed number cannot do it:
    # the best of fourteen hundred scores is high for any caption at all.
    floor = noise_floor(index, backend, inside)
    bar = max(visual.LIFT_OK, floor)
    strong = max(visual.LIFT_STRONG, bar + (visual.LIFT_STRONG - visual.LIFT_OK))
    if floor > visual.LIFT_OK:
        log(f"      {run.label}: a caption about nothing scores {floor:.1f} "
            f"here, so {bar:.1f} is what counts as found")
    searched = score
    if inside:
        lo_i, hi_i = inside
        searched = score[:, max(0, lo_i):hi_i + 1]
    reachable = (searched.max(axis=1) if searched.size
                 else np.zeros(len(ordered)))
    own_best = _distinct_at_best(score)
    choosers = [k for k in range(len(ordered))
                if held[k] or reachable[k] >= bar]

    # A last question, asked of the run rather than of any one shot: is this
    # more than luck would have given it anyway?
    #
    # No per-shot bar can be clean. The floor sits near the top of what a
    # caption about nothing reaches, so roughly one shot in eight still clears
    # it by chance — two of twelve, in the test that measures this. Two lucky
    # frames are enough to drag the other ten between them, which is the
    # original complaint in miniature.
    #
    # So a run must find more than a quarter of itself, or find one thing
    # convincingly. The second half matters as much as the first: a single
    # shot far above the floor in a run of twenty is not luck, and refusing it
    # would throw away the one real thing the model saw.
    #
    # Only the picture picks are counted. A quoted line is separate evidence
    # and keeps its pin either way — but it does not vouch for the lucky
    # frames around it, and a run held by two anchors can still be pulled two
    # minutes out of shape by one of them.
    #
    # And a shot that IS convincing vouches only for itself. Letting one
    # vouch for the whole run was the difference between a usable build and
    # an unusable one: 91 shots, 6 above a floor of 2.5, one of them at 3.6 —
    # so all six were kept, and the five that were chance decided where 85
    # shots went.
    picks = [k for k in choosers if not held[k]]
    if picks and len(picks) <= len(ordered) / 4.0:
        sure = [k for k in picks if reachable[k] >= strong]
        if not sure:
            log(f"      {run.label}: only {len(picks)} of {len(ordered)} "
                "shot(s) beat what an unrelated caption scores here — that is "
                "chance, not a match")
        elif len(sure) < len(picks):
            log(f"      {run.label}: {len(sure)} shot(s) found outright; the "
                f"other {len(picks) - len(sure)} are level with what an "
                "unrelated caption scores here and do not get a vote")
        keep = set(sure)
        choosers = [k for k in choosers if held[k] or k in keep]

    # How far apart two choosers must be: far enough to hold the shots that
    # will be interpolated between them, at MIN_APART_S each. Without this a
    # run does not spread, it stacks.
    dt = float(np.median(np.diff(index.times))) if len(index.times) > 1 else 2.0
    per_shot = max(1, int(round(MIN_APART_S / max(dt, 0.1))))
    gaps, tail = [], 0
    if choosers:
        # The shots at either end need room as much as the ones in between.
        gaps = [choosers[0] * per_shot] + [
            max(1, (choosers[j] - choosers[j - 1]) * per_shot)
            for j in range(1, len(choosers))]
        tail = (len(ordered) - 1 - choosers[-1]) * per_shot

    path = solve(total[choosers], [bounds[k] for k in choosers],
                 gaps, tail) if choosers else []
    if not path and any(held[k] for k in choosers):
        # The pins themselves are out of order, which no assignment can
        # satisfy. That is worth knowing: it means two quoted lines disagree
        # about which way this run runs. Solve it on the pictures alone.
        log(f"      {run.label}: the quoted lines contradict each other on "
            "order — deciding on the pictures alone")
        path = solve(total[choosers], [inside] * len(choosers), gaps, tail)
        if path:
            held = [False] * len(ordered)
    if not path:
        # Either nothing matched, or what matched is packed too tightly to
        # hold this many shots without stacking them on one another.
        # Alignment's spread is the better answer to both.
        log(f"      {run.label}: nothing in these {len(ordered)} shot(s) could "
            "be found in the picture far enough apart to hold them — left as "
            "aligned")
        for k, i in enumerate(ordered):
            out[i] = Verdict(beat=placements[i].beat, shot=placements[i].shot,
                             bar=bar, best=float(reachable[k]),
                             distinct=bool(own_best[k]),
                             note="no frame in this episode matched any of them")
        return out

    chosen = {k: float(index.times[path[j]]) for j, k in enumerate(choosers)}
    lifts = {k: float(score[k, path[j]]) for j, k in enumerate(choosers)}
    axis = align.axis(run)
    settled = interpolate([placements[i].start_ms / 1000.0 for i in ordered],
                          [axis[i] for i in ordered], chosen, MIN_APART_S)
    # An episode has an end. Interpolation walks outward from the shots that
    # were found, and with a run held by one line at its last shot it can
    # walk right off the back of the film: two shots of a real build were
    # placed at 2918s and 3488s of a 2848-second episode. ffmpeg cut nothing,
    # the segments failed to render, and eleven seconds vanished from the
    # finished video with the picture drifting ahead of the voice from there.
    last_frame = float(index.times[-1]) if len(index.times) else 0.0
    if last_frame > 0:
        settled = [min(max(0.0, t), last_frame) for t in settled]
    if len(choosers) < len(ordered):
        log(f"      {run.label}: {len(choosers)} shot(s) found in the picture, "
            f"{len(ordered) - len(choosers)} placed between them")

    solid = any(v >= strong for v in lifts.values())
    for k, i in enumerate(ordered):
        p = placements[i]
        # `path` is indexed by chooser, not by shot: a shot nobody could find
        # has no entry in it at all, and reading one was how an earlier
        # version of this quietly mixed up which shot went where.
        lift = lifts.get(k, 0.0)
        best = float(reachable[k])
        before = p.start_ms
        after = int(settled[k] * 1000)
        duration = max(500, p.end_ms - p.start_ms)

        if held[k]:
            v = Verdict(beat=p.beat, shot=p.shot, action="pinned",
                        before_ms=before, after_ms=before, lift=lift,
                        best=best, distinct=bool(own_best[k]), bar=bar)
            v.note = ("held on its quoted line; the picture "
                      + ("agrees" if lift >= bar else "says little"))
            out[i] = v
            continue

        p.start_ms = after
        p.end_ms = after + duration
        if lift >= bar:
            p.method = "verified"
        elif grounded or solid:
            # Something in this run IS fixed — a quoted line, or a picture
            # that matched outright — so the shots between are positioned by
            # it, which is what interpolation has always meant.
            p.method = "interpolated"
        else:
            # Nothing in this run is fixed by anything. Cutting here would
            # be inventing a position, so it stays unplaced and is reported.
            p.method = "none"
        p.confidence = ("high" if lift >= strong
                        else "medium" if lift >= bar else "low")
        v = Verdict(beat=p.beat, shot=p.shot, before_ms=before,
                    after_ms=after, lift=lift, best=best,
                    distinct=bool(own_best[k]), bar=bar)
        if lift >= bar:
            v.action = "moved" if abs(after - before) >= 1000 else "kept"
            p.note = f"the picture matches this description (lift {lift:.1f})"
            if v.action == "moved":
                p.note += f", {v.moved_seconds:.0f}s from where the script implied"
        else:
            v.action = "drifted"
            p.note = ("no frame in this episode matches this description — "
                      "placed in order between the shots that did")
        out[i] = v
    return out


# A run covers one stretch of one episode, not the whole of it. How long a
# stretch is guessed from how much screen time the run asks for: a scene
# breakdown of eight shots is not spread over half an hour.
WINDOW_SPAN = 2.5               # of the run's own screen time
WINDOW_MIN_S = 90.0
WINDOW_MAX_S = 600.0
# How far past an ordinary window the best one has to stand before it is
# believed. Below this the episode has no opinion and the whole of it is
# fairer than a confident wrong quarter of it.
WINDOW_EDGE = 1.9
# Never the titles, never the recap, never the credits. Those are the most
# visually distinctive frames in any episode — hard cuts, captions, a
# montage — so a search for "the part that stands out" walks straight into
# them. A real build put filler at 4s, 8s and 37s of a forty-seven minute
# episode for scenes about a killing thirty minutes in.
WINDOW_KEEP_OUT = (0.06, 0.97)
# The chosen window is widened by this much on each side. The window is a
# hint for filler, not a boundary: picking the highest-scoring START can
# clip the tail of the very run it just found, and losing the last two shots
# of a scene to an off-by-one is a worse failure than being slightly loose.
WINDOW_PAD = 0.35


def locate_run(index: visual.VisualIndex, captions: list, backend,
               wanted_seconds: float = 0.0,
               faces: np.ndarray | None = None) -> tuple:
    """Which stretch of this episode the whole run happens in.

    Asking each shot on its own is what a search does, and on a wordless
    scene it mostly fails: one description of one dim interior against
    fourteen hundred frames is a coin toss, and the tool then spread the run
    across thirty-eight minutes of an episode that contained it in four.

    Asking all of them together is a different question, and a much easier
    one. Twenty descriptions from the same scene all score a little higher
    in the part of the episode where that scene actually is, and twenty
    little agreements are worth more than one confident guess. So this
    slides a window over the episode and keeps the one the run as a whole
    likes best.

    Returns (lo, hi, strength), or (0, 0, 0) when the episode has no opinion
    — in which case the whole of it stays available, because a confident
    wrong quarter is worse than an honest whole.
    """
    if not len(index) or not captions:
        return (0.0, 0.0, 0.0)
    vecs = backend.encode_texts(captions)
    sims = np.stack([index.similarities(v) for v in vecs])   # shots x frames
    if not np.any(sims):
        return (0.0, 0.0, 0.0)

    # Who is on screen, added to what the descriptions say. This is the
    # cheapest large win available: twenty captions from one scene agree
    # weakly about where it is, and "the two people the scene is about are
    # both in these four minutes" agrees strongly.
    if faces is not None and len(faces) == sims.shape[1]:
        sims = sims + faces[None, :] * cast.CAST_WEIGHT * float(np.std(sims))

    times = np.asarray(index.times, dtype=np.float64)
    length = float(times[-1] - times[0]) or 1.0
    floor = times[0] + WINDOW_KEEP_OUT[0] * length
    ceiling = times[0] + WINDOW_KEEP_OUT[1] * length
    span = min(WINDOW_MAX_S,
               max(WINDOW_MIN_S, WINDOW_SPAN * float(wanted_seconds or 0.0)))
    if span >= length:
        return (0.0, 0.0, 0.0)          # the window is the episode

    step = max(1, int(len(times) / 240))         # ~240 windows, whatever the length
    scores, starts = [], []
    lo = floor
    while lo + span <= ceiling:
        inside = (times >= lo) & (times <= lo + span)
        if inside.any():
            # The best quarter of the shots, not all of them. Two shots in
            # three carry a description no model can place — "he thinks about
            # what he has done" is not a picture — and averaging those in
            # means the handful that CAN be placed never move the number.
            # The median over sixty such shots is a measure of the noise.
            best = np.sort(sims[:, inside].max(axis=1))[::-1]
            keep = max(2, int(round(len(best) * 0.25)))
            scores.append(float(best[:keep].mean()))
            starts.append(lo)
        lo += (times[step] - times[0]) if step < len(times) else span
    if len(scores) < 4:
        return (0.0, 0.0, 0.0)

    scores = np.asarray(scores)
    best = int(np.argmax(scores))
    middle = float(np.median(scores))
    spread = float(np.percentile(scores, 90)) - middle
    if spread <= 1e-6:
        return (0.0, 0.0, 0.0)          # every part of the episode alike
    strength = (float(scores[best]) - middle) / spread
    if strength < WINDOW_EDGE:
        return (0.0, 0.0, 0.0)
    pad = span * WINDOW_PAD
    return (max(floor, float(starts[best]) - pad),
            min(ceiling, float(starts[best] + span) + pad),
            strength)


def locate_runs(db_path: str, beats: list, people: dict | None = None,
                log=lambda *a: None) -> dict:
    """{(beat, shot): (lo, hi)} — where each run happens in its episode.

    Keyed by shot rather than by beat because a beat routinely draws from
    several episodes — 24 of 34 on a real script — and one window per beat
    means one episode's window silently overwrites all the others in it.
    """
    if embed.loaded() is None:
        ok, _why = embed.available()
        if not ok:
            return {}
    from .library import connect

    con = connect(db_path)
    try:
        backend = embed.load(log=log)
    except embed.EmbedError:
        con.close()
        return {}

    found: dict = {}
    try:
        for run in align.runs(beats):
            path = align.episode_file(db_path, run)
            if not path:
                continue
            index = visual.load(con, db_path, path)
            if index is None or not len(index):
                continue
            captions, wanted = [], 0.0
            for entry in run.entries:
                caption = describe(entry.data or {})
                if caption:
                    captions.append(caption)
                try:
                    wanted += float(entry.data.get("duration_target_sec") or 4.0)
                except (TypeError, ValueError, AttributeError):
                    wanted += 4.0
            if len(captions) < 2:
                continue                 # two agreements are the minimum
            lo, hi, strength = locate_run(index, captions, backend, wanted,
                                          faces=_faces_for(index, run, people))
            if hi <= lo:
                log(f"      {run.label}: the picture has no opinion about "
                    "where this run happens")
                continue
            for entry in run.entries:
                found[(entry.beat, entry.shot)] = (lo, hi)
            log(f"      {run.label}: happens around "
                f"{lo/60:.0f}-{hi/60:.0f} min of "
                f"{os.path.basename(path)} (x{strength:.1f})")
    finally:
        con.close()
    return found


def _faces_for(index, run, people: dict | None):
    """A per-frame bonus for everybody this run names, or None."""
    if not people or index is None or not len(index):
        return None
    wanted, seen = [], set()
    for entry in run.entries:
        for person in cast.named_in(entry.data or {}, people):
            if person.key not in seen:
                wanted.append(person)
                seen.add(person.key)
    if not wanted:
        return None
    return cast.frames_with(index, wanted)


def place_by_picture(db_path: str, beats: list, placements: list,
                     episodes: dict | None = None,
                     windows: dict | None = None,
                     people: dict | None = None,
                     log=lambda *a: None) -> int:
    """Find a home for every shot that dialogue could not place.

    Until now a shot with no quoted line fell straight through to filler —
    the right episode, at a moment chosen by walking through it. That is
    honest when nothing better is available, and it was all that WAS
    available while nine of sixty-two episodes had their frames read.

    It is no longer all that is available. A complete picture index can be
    asked the actual question — *where in this episode does this description
    happen?* — and it answers it for a shot with no dialogue exactly as well
    as for one with dialogue, because it never needed the dialogue.

    This matters most on precisely the videos worth making. A scene everyone
    remembers is usually a quiet one: the box-cutter scene has almost no
    speech in it at all, so a script about it has almost nothing to quote,
    and a build that anchors only on speech placed three shots out of a
    hundred and eighteen and filled the rest by walking. Every complaint
    about random footage came from that.

    Only placements that clear the episode's own measured noise floor are
    taken. Below it the picture is not saying anything, and filler — which
    is at least spread evenly — remains the better answer.

    Returns how many shots were placed this way.
    """
    if not placements:
        return 0
    if embed.loaded() is None:
        ok, why = embed.available()
        if not ok:
            log(f"      placing by picture is off — {why}")
            return 0

    from .library import connect

    by_beat = {b.get("beat", i): b for i, b in enumerate(beats, 1)}
    homeless = [p for p in placements if not p.ok or not p.path]
    if not homeless:
        return 0

    con = connect(db_path)
    try:
        backend = embed.load(log=log)
    except embed.EmbedError as exc:
        con.close()
        log(f"      placing by picture is off — {exc}")
        return 0

    # An episode is loaded once and asked many times: the frames are the
    # slow part and every shot of a run wants the same ones.
    seen: dict = {}
    floors: dict = {}
    placed, tried = 0, 0
    try:
        for p in homeless:
            beat = by_beat.get(p.beat) or {}
            shots = beat.get("shots") or []
            shot = shots[p.shot - 1] if 0 < p.shot <= len(shots) else {}
            caption = describe(shot)
            if not caption:
                continue
            path = p.path or (episodes or {}).get(p.beat, "")
            if not path:
                continue
            if path not in seen:
                seen[path] = visual.load(con, db_path, path)
            index = seen[path]
            if index is None or not len(index):
                continue
            if path not in floors:
                floors[path] = max(visual.LIFT_OK,
                                   noise_floor(index, backend))
            tried += 1
            vec = backend.encode_texts([caption])[0]
            # Inside the stretch the run was located to, when there is one.
            # A description searched across a whole episode competes with
            # fourteen hundred frames of everything else in it; searched
            # across the four minutes the scene actually occupies, it is
            # competing with the scene.
            span = (windows or {}).get((p.beat, p.shot))
            # ...and only among the frames the people this shot names are
            # actually in, where the script says who they are. A sentence
            # about Gus and Walter landing on Skyler and Walt Jr. is the
            # complaint this answers, and no description can answer it: the
            # kitchen looks like the kitchen either way.
            here = cast.named_in(shot, people) if people else []
            bonus = (cast.frames_with(index, here) * cast.CAST_WEIGHT
                     if here else None)
            match = (visual.best_in(index, vec, lo=span[0], hi=span[1],
                                    bonus=bonus)
                     if span and span[1] > span[0]
                     else visual.best_in(index, vec, bonus=bonus))
            if match.lift < floors[path]:
                continue
            hold = max(1, p.end_ms - p.start_ms)
            p.path = path
            p.start_ms = int(match.time * 1000)
            p.end_ms = p.start_ms + hold
            p.method = "picture"
            p.confidence = match.confidence
            p.note = f"found in the picture (lift {match.lift:.1f})"
            placed += 1
    finally:
        con.close()
    if tried:
        log(f"      placed by picture: {placed} of {tried} shot(s) that had "
            "no quoted line")
    return placed


# ---------------------------------------------------------------------------
# pacing — a run is a sequence even when nothing in it can be matched
# ---------------------------------------------------------------------------

# Fewer than this and the run is a cutaway, not a walk through a scene:
# there is no order worth preserving and filler is as good an answer.
PACE_MIN_SHOTS = 4


def pace_runs(db_path: str, beats: list, placements: list,
              windows: dict | None = None, log=lambda *a: None) -> int:
    """Lay a run with no quoted line in ORDER across the stretch it belongs to.

    This is the answer to the loudest complaint about the whole tool — "the
    clips are random" — and the reason it was true is not that the matching
    was bad. It is that a run nothing could match never had an ORDER applied
    to it at all.

    Alignment already builds the right shape for such a run: the script says
    how long each shot is, so the shots are seconds apart in a known
    sequence. But with no anchor there was nowhere to put that shape, so it
    was parked at the middle of the episode and left as method "none" —
    correct, and unusable. Every shot then fell through to filler, and filler
    walks the episode by the golden ratio: eighty-five consecutive shots of
    one four-minute scene came back in eighty-five unrelated orders. Shot 3
    from the end of the scene, shot 4 from the start. That is what "random"
    looked like, and no amount of better matching would have fixed it,
    because nothing was being matched.

    Once `locate_run` has said WHERE the scene is, the shape has somewhere to
    go. Slide it there, keep the script's own spacing, and the scene plays
    through in the order it happens — which is what an editor with the
    footage and the script would do without thinking about it.

    The spacing is compressed to fit and never stretched to fill. A window is
    wider than the run on purpose; spreading the run to the edges of it would
    invent gaps the script never asked for.

    Returns how many shots were laid out this way.
    """
    if not placements or not windows:
        return 0
    by_key = {(p.beat, p.shot): p for p in placements}
    moved = 0
    for run in align.runs(beats):
        mine = [by_key.get((e.beat, e.shot)) for e in run.entries]
        if len(mine) < PACE_MIN_SHOTS or any(p is None for p in mine):
            continue
        loose = [p for p in mine if not p.ok]
        if len(loose) < PACE_MIN_SHOTS:
            continue                # dialogue placed this run; leave it alone
        span = windows.get((run.entries[0].beat, run.entries[0].shot))
        if not span or span[1] <= span[0]:
            continue                # the picture has no opinion — filler, then
        path = next((p.path for p in mine if p.path), "")
        if not path:
            continue

        lo, hi = float(span[0]), float(span[1])
        ax = align.axis(run)
        reach = max(1e-6, ax[-1] - ax[0])
        squeeze = min(1.0, (hi - lo) / reach)
        start = lo + max(0.0, ((hi - lo) - reach * squeeze) / 2.0)

        # One shot in the run WAS found — by a quoted line, or by its picture
        # standing clear of the episode's noise. That is a real measurement
        # and it outranks the middle of a window: hang the sequence off it
        # rather than off a guess, as long as the run still fits the window.
        # ...but only a shot that was found INSIDE the window. One that
        # landed outside it disagrees with the window, and when the window
        # came from a person who typed it, the window is the one that was
        # not inferred from anything.
        firm = [(i, p) for i, p in enumerate(mine)
                if p.ok and p.path == path and lo <= p.start_ms / 1000.0 <= hi]
        if firm:
            i, p = firm[len(firm) // 2]
            start = (p.start_ms / 1000.0) - (ax[i] - ax[0]) * squeeze
            start = min(max(start, lo), max(lo, hi - reach * squeeze))

        for i, p in enumerate(mine):
            if p.ok:
                continue
            hold = max(1, p.end_ms - p.start_ms)
            p.path = path
            p.start_ms = int(max(0.0, start + (ax[i] - ax[0]) * squeeze) * 1000)
            p.end_ms = p.start_ms + hold
            p.method = "paced"
            p.confidence = "low"
            p.note = "laid in script order across the scene this run was found in"
            moved += 1
        log(f"      {run.label}: {len(loose)} shot(s) laid in order across "
            f"{start/60:.1f}-{(start + reach * squeeze)/60:.1f} min"
            + (" (hung off the one shot that was found)" if firm else ""))
    if moved:
        log(f"      paced: {moved} shot(s) placed in script order rather than "
            "scattered as filler")
    return moved


def apply(db_path: str, beats: list, placements: list,
          log=lambda *a: None) -> Report:
    """Check and correct a whole script's placements. Never raises.

    A missing model, a missing picture index, a single unreadable episode:
    all of them leave the build exactly as alignment produced it and say so.
    Verification improves a result; it must never be the reason there isn't
    one.
    """
    report = Report()
    # A backend already in hand beats any guess about what is installed —
    # a caller holding one model open across a queue of videos, or a test
    # standing in for it, has answered the question by having it.
    if embed.loaded() is None:
        ok, why = embed.available()
        if not ok:
            report.reason = why
            return report

    from .library import connect
    by_key = {(p.beat, p.shot): p for p in placements}
    con = connect(db_path)
    try:
        backend = embed.load(log=log)
    except embed.EmbedError as exc:
        con.close()
        report.reason = str(exc).splitlines()[0]
        return report

    try:
        cache: dict = {}
        for run in align.runs(beats):
            mine = [by_key.get((e.beat, e.shot)) for e in run.entries]
            mine = [p for p in mine if p is not None]
            if len(mine) != len(run.entries) or not mine:
                continue
            path = next((p.path for p in mine if p.path), "")
            if not path:
                continue
            if path not in cache:
                cache[path] = visual.load(con, db_path, path)
            index = cache[path]
            if index is None:
                name = os.path.basename(path)
                if name not in report.runs_without_index:
                    report.runs_without_index.append(name)
                continue
            verdicts = verify_run(index, run, mine, backend, log=log)
            report.verdicts += verdicts
            report.runs_seen += 1
            if verdicts and all(v.action == "unchecked" for v in verdicts):
                report.runs_left_alone += 1
            floor = max((v.bar for v in verdicts), default=0.0)
            if floor > visual.LIFT_OK:
                report.floors.append(floor)
            for v in verdicts:
                if v.action in ("kept", "moved", "pinned", "drifted"):
                    report.checked += 1
                if v.action == "moved":
                    report.moved += 1
                if v.action == "drifted":
                    report.unmatched += 1
                    if v.lost_to_ordering:
                        report.lost_to_ordering += 1
                # "Findable" has to mean the same thing here as it does in
                # `lost_to_ordering`, or the two numbers in the report
                # contradict each other: a real match somewhere, above this
                # episode's own bar, that this shot could call its own.
                if v.distinct and v.best >= v.bar:
                    report.findable += 1
            _log_run(run, verdicts, log)
        if report.runs_without_index and not report.checked:
            report.reason = ("no picture index yet for "
                             + ", ".join(report.runs_without_index[:3])
                             + " — run 'Look at the footage' first")
    finally:
        con.close()
    return report


def _log_run(run, verdicts: list, log) -> None:
    matched = sum(1 for v in verdicts if v.lift >= v.bar)
    findable = sum(1 for v in verdicts if v.distinct and v.best >= v.bar)
    moved = [v for v in verdicts if v.action == "moved"]
    log(f"      {run.label}: {matched}/{len(verdicts)} shot(s) found in the "
        f"picture, {len(moved)} moved"
        + (f" ({findable} had a match somewhere, so "
           f"{findable - matched} lost theirs to the ordering)"
           if findable > matched else ""))
    for v in sorted(moved, key=lambda x: -x.moved_seconds)[:3]:
        log(f"        shot {v.shot}: {v.before_ms/1000:.0f}s -> "
            f"{v.after_ms/1000:.0f}s (lift {v.lift:.1f})")
