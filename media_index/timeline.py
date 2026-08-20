"""Decide how long every shot stays on screen, and when.

Up to here the tool answers "which frame". This answers "for how long", and
they are not the same question — a video made of correct footage cut to a
metronome still looks like a machine made it.

Three rules came from watching a real build and from what a real editor
notices first:

**Nothing is a fixed length.** Every clip was cut to exactly 4.0 seconds,
because `clip_seconds` defaulted to 4.0 and the cutter took `min(end, start +
4.0)`. Forty identical durations in a row is a rhythm no human produces, and
it is the single most recognisable signature of an automated edit. Durations
here are varied, per shot, and never repeat back to back.

**The number of visuals comes from the narration, not from the assets.** A
ten-word line is about four seconds of speech. Putting a clip and three
stills under it gives each of them a second, which reads as footage shoved in
to fill a hole — and it reads that way precisely because it is. So a beat's
budget decides how many cuts it can hold, and a short beat holds one.

**A clip and a still are different instruments.** A clip carries motion and
gets tiring past six seconds. A still can hold ten or twelve while the
narration does the work, and holding one is a choice an editor makes on
purpose. They alternate, and the still side takes slightly more of the
screen time — which is where the roughly 55/45 split comes from.

Beat boundaries come from the narration's own lengths, scaled to the real
runtime of the voiceover. That is honest but approximate: it assumes the read
is even. Aligning each beat to the actual audio — the transcriber is already
here — is the obvious next step, and this module takes explicit boundaries
whenever something can supply them.
"""
from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field

# The ceilings are the brief. A clip past six seconds outstays its welcome;
# a still can hold far longer because nothing on it is moving.
MAX_CLIP_S = 6.0
MIN_CLIP_S = 1.8
MAX_STILL_S = 12.0
MIN_STILL_S = 3.0
# Nothing may flash past faster than this, whatever the arithmetic says. This
# is the number that stops four visuals being crammed under one short line.
MIN_ON_SCREEN_S = 2.2
# How long a visual holds on average before variation is applied. Chosen
# against the competitor measurement: 4 to 63 cuts per minute, averaging
# 31.5, which is a visual every 1.9 seconds at the fast end and every 15 at
# the slow one. Sitting near the middle leaves room to move either way.
BASE_SEGMENT_S = 4.6
# Pace is an editorial choice, not a technical one, so it is a dial rather
# than a constant. Measured against competitor essays running 4 to 63 cuts a
# minute — averaging 31.5 — these land at roughly 10, 13, 20 and 26. The
# default matches the brief for this channel: clips of 3 to 5 seconds and
# stills around 5, which arithmetic alone puts near 13.
PACES = {"calm": 6.0, "normal": 4.6, "quick": 3.0, "rapid": 2.4}
# How far a duration may stray from its share of the beat. Enough to be
# audibly irregular, not so much that one visual eats a beat.
JITTER = 0.28
WORDS_PER_MINUTE = 150.0
# Two neighbouring visuals within this are the same length to the eye.
SAME_LENGTH_S = 0.25


@dataclass
class Item:
    """One visual on the timeline."""
    file: str
    kind: str                     # video | image
    start: float = 0.0            # seconds into the finished video
    duration: float = 0.0
    source: str = ""              # the episode it came from
    source_start: float = 0.0     # where in that episode
    placed_by: str = ""
    confidence: str = ""

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass
class Scene:
    index: int
    narration: str = ""
    start: float = 0.0
    end: float = 0.0
    items: list = field(default_factory=list)
    note: str = ""

    @property
    def budget(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def covered(self) -> float:
        return sum(i.duration for i in self.items)

    @property
    def gap(self) -> float:
        return max(0.0, self.budget - self.covered)


@dataclass
class Timeline:
    video: str = ""
    audio: str = ""
    pace: str = "normal"
    total_seconds: float = 0.0
    scenes: list = field(default_factory=list)

    @property
    def items(self) -> list:
        return [i for s in self.scenes for i in s.items]

    @property
    def still_share(self) -> float:
        total = sum(i.duration for i in self.items)
        if not total:
            return 0.0
        return sum(i.duration for i in self.items if i.kind == "image") / total

    @property
    def cuts_per_minute(self) -> float:
        if self.total_seconds <= 0:
            return 0.0
        return len(self.items) / (self.total_seconds / 60.0)

    def uncovered(self) -> list:
        return [s for s in self.scenes if s.gap > 0.5]

    def summary(self) -> str:
        from . import term
        d = term.sym("dot")
        gaps = self.uncovered()
        lengths = sorted({round(i.duration, 1) for i in self.items})
        return (f"  {len(self.items)} visual(s) over "
                f"{self.total_seconds / 60:.1f} min {d} "
                f"{self.cuts_per_minute:.0f} cuts/min {d} "
                f"{self.still_share:.0%} of the screen on stills {d} "
                f"{len(lengths)} distinct lengths"
                + (f" {d} {len(gaps)} scene(s) short of footage" if gaps else ""))


# ---------------------------------------------------------------------------
# how long each beat lasts
# ---------------------------------------------------------------------------

def spoken_seconds(beat: dict) -> float:
    """What this beat's narration is worth, before any scaling.

    The script's own `narration_seconds` when it gave one — it was written by
    something that read the sentence — and a word count otherwise. A beat
    with neither still gets a floor rather than zero, because a beat with no
    time gets no picture.
    """
    for key in ("narration_seconds", "seconds", "duration_sec"):
        try:
            value = float(beat.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    words = len((beat.get("narration") or "").split())
    return max(1.0, words / WORDS_PER_MINUTE * 60.0)


def boundaries(beats: list, total_seconds: float = 0.0) -> list:
    """[(start, end)] per beat, scaled to the real voiceover if there is one.

    Scaling matters more than it looks. The script estimates at 150 words a
    minute; a real read is rarely that, and an eight-minute script over a
    nine-minute recording drifts a whole minute by the end — every visual in
    the last third under the wrong sentence.
    """
    lengths = [spoken_seconds(b) for b in beats]
    planned = sum(lengths) or 1.0
    scale = (total_seconds / planned) if total_seconds > 0 else 1.0
    out, t = [], 0.0
    for length in lengths:
        out.append((t, t + length * scale))
        t += length * scale
    return out


# ---------------------------------------------------------------------------
# how many visuals, and how long each
# ---------------------------------------------------------------------------

def segment_count(budget: float, available: int,
                  base: float = BASE_SEGMENT_S) -> int:
    """How many visuals this much narration can carry.

    Driven by the budget, never by how many assets happen to exist. Four
    visuals under a four-second line is a second each, and a second each is
    what "footage shoved in to fill a hole" looks like — because that is
    exactly what it is.
    """
    if budget <= 0 or available <= 0:
        return 0
    by_time = max(1, int(budget // MIN_ON_SCREEN_S))
    natural = max(1, round(budget / max(0.5, base)))
    return max(1, min(natural, by_time, available))


def _limits(kind: str) -> tuple:
    if kind == "video":
        return MIN_CLIP_S, MAX_CLIP_S
    return MIN_STILL_S, MAX_STILL_S


def share_out(budget: float, kinds: list, rng: random.Random) -> list:
    """Split a budget across visuals: varied, inside each kind's limits.

    Water-filling rather than a single pass. Clamping a clip to six seconds
    has to give its overflow to something else, or the beat comes up short
    and the narration runs on over a frozen frame.
    """
    n = len(kinds)
    if n == 0:
        return []
    lows = [_limits(k)[0] for k in kinds]
    highs = [_limits(k)[1] for k in kinds]

    share = budget / n
    want = [max(0.1, share * (1.0 + rng.uniform(-JITTER, JITTER)))
            for _ in kinds]
    total = sum(want) or 1.0
    want = [w * budget / total for w in want]

    fixed = [False] * n
    for _ in range(n + 2):
        residual = budget - sum(want[i] for i in range(n) if fixed[i])
        free = [i for i in range(n) if not fixed[i]]
        if not free:
            break
        loose = sum(want[i] for i in free) or 1.0
        for i in free:
            want[i] = want[i] * residual / loose
        broke = False
        for i in free:
            if want[i] < lows[i]:
                want[i], fixed[i], broke = lows[i], True, True
            elif want[i] > highs[i]:
                want[i], fixed[i], broke = highs[i], True, True
        if not broke:
            break
    return [round(w, 2) for w in want]


def vary(durations: list, kinds: list, rng: random.Random) -> list:
    """Make sure no two neighbours hold for the same length.

    Forty clips of 4.0 seconds is the signature of an automated edit, and two
    in a row is where a viewer first feels it.

    The separation is taken from one of the pair and given to the other, so
    the beat still adds up to exactly the time the narration occupies.
    Lengthening one shot and leaving the total alone was the first attempt,
    and it quietly overran every beat it touched — the fix for a machine-like
    rhythm cannot be a drift in the sync.
    """
    out = list(durations)
    for i in range(1, len(out)):
        if abs(out[i] - out[i - 1]) >= SAME_LENGTH_S:
            continue
        step = SAME_LENGTH_S + rng.uniform(0.05, 0.35)
        lo_a, hi_a = _limits(kinds[i - 1])
        lo_b, hi_b = _limits(kinds[i])
        for give, take in ((step, -step), (-step, step)):
            a, b = out[i - 1] + give, out[i] + take
            if lo_a <= a <= hi_a and lo_b <= b <= hi_b:
                out[i - 1], out[i] = round(a, 2), round(b, 2)
                break
        # Neither direction fits: both are pinned at a limit, which means the
        # beat is already as varied as its own ceilings allow.
    return out


def choose(assets: list, n: int) -> list:
    """Pick and order `n` assets, alternating motion and stillness.

    A run of clips is a trailer and a run of stills is a slideshow. Cutting
    between them is what an editor does, and because a still is allowed to
    hold far longer than a clip, alternating is also what produces the
    roughly 55/45 split of screen time on its own.
    """
    clips = [a for a in assets if a.get("kind") == "video"]
    stills = [a for a in assets if a.get("kind") != "video"]
    out = []
    # Lead with whichever kind this beat has more of; a beat of six stills
    # and one clip should not open on its only clip and then stop moving.
    take_clip = len(clips) >= len(stills)
    while len(out) < n and (clips or stills):
        pool = clips if (take_clip and clips) else stills
        if not pool:
            pool = clips or stills
        out.append(pool.pop(0))
        take_clip = not take_clip
    return out


def lay_out(scene_index: int, narration: str, start: float, end: float,
            assets: list, seed: int = 0, base: float = BASE_SEGMENT_S) -> Scene:
    """One beat's stretch of timeline."""
    scene = Scene(index=scene_index, narration=narration, start=start, end=end)
    budget = scene.budget
    n = segment_count(budget, len(assets), base=base)
    if not n:
        scene.note = ("nothing to show here" if not assets
                      else "no time budgeted for this beat")
        return scene

    # Seeded per scene, so a rebuild produces the same timeline. A review
    # step is worthless if the thing reviewed changes when it is rendered.
    # Seeded from a string: a tuple was rejected outright by Python 3.9 and
    # later, which turned every beat into a TypeError rather than a rhythm.
    rng = random.Random(f"{seed}:{scene_index}")
    picked = choose(assets, n)
    kinds = [a.get("kind", "image") for a in picked]
    lengths = vary(share_out(budget, kinds, rng), kinds, rng)

    t = start
    for asset, kind, length in zip(picked, kinds, lengths):
        scene.items.append(Item(
            file=asset.get("file", ""), kind=kind, start=round(t, 2),
            duration=length, source=asset.get("source", ""),
            source_start=float(asset.get("source_start") or 0.0),
            placed_by=asset.get("placed_by", ""),
            confidence=asset.get("confidence", "")))
        t += length
    if scene.gap > 0.5:
        scene.note = (f"{scene.gap:.1f}s of this beat has no footage — "
                      f"{len(assets)} asset(s) available")
    return scene


# ---------------------------------------------------------------------------
# the whole video
# ---------------------------------------------------------------------------

def plan(beats: list, manifest: dict, total_seconds: float = 0.0,
         audio: str = "", seed: int = 0, pace: str = "normal",
         spans: list | None = None) -> Timeline:
    """Turn a built folder's manifest into a timed sequence.

    `spans` are real beat boundaries, measured off the recording by
    `narration.py`. Without them the estimate is used, which assumes an even
    read — good enough to work with, wrong wherever the narrator paused.
    """
    base = PACES.get(pace, BASE_SEGMENT_S)
    by_scene = {s.get("scene"): s for s in (manifest.get("scenes") or [])}
    if spans and len(spans) == len(beats):
        spans = list(spans)
    else:
        spans = boundaries(beats, total_seconds)
    tl = Timeline(video=manifest.get("video", ""), audio=audio, pace=pace,
                  total_seconds=(total_seconds
                                 or (spans[-1][1] if spans else 0.0)))
    for i, (start, end) in enumerate(spans, 1):
        entry = by_scene.get(i) or {}
        assets = list(entry.get("assets") or [])
        for a in assets:
            a.setdefault("source", entry.get("source", ""))
            a.setdefault("confidence", entry.get("confidence", ""))
        tl.scenes.append(lay_out(i, entry.get("narration", ""), start, end,
                                 assets, seed=seed, base=base))
    return tl


def to_dict(tl: Timeline) -> dict:
    return {
        "video": tl.video,
        "audio": tl.audio,
        "pace": tl.pace,
        "generated_at": int(time.time()),
        "total_seconds": round(tl.total_seconds, 2),
        "still_share": round(tl.still_share, 3),
        "cuts_per_minute": round(tl.cuts_per_minute, 1),
        "scenes": [{
            "scene": s.index,
            "narration": s.narration,
            "start": round(s.start, 2),
            "end": round(s.end, 2),
            "note": s.note,
            "items": [{
                "file": i.file, "kind": i.kind,
                "start": round(i.start, 2), "duration": round(i.duration, 2),
                "source": i.source, "source_start": round(i.source_start, 2),
                "placed_by": i.placed_by, "confidence": i.confidence,
            } for i in s.items],
        } for s in tl.scenes],
    }


def write(tl: Timeline, out_dir: str, name: str = "timeline.json") -> str:
    path = os.path.join(out_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(to_dict(tl), f, indent=2)
    return path


def load_manifest(out_dir: str) -> dict:
    path = os.path.join(out_dir, "manifest.json")
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)
