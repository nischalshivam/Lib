"""How much a placement is worth, and what the tool is allowed to do with it.

Every version of this tool until now had one word for "we put something
there": *placed*. A shot found by a quoted line the subtitles confirm and a
shot picked by walking through an episode with a golden-ratio counter were
both `placed`, both went into the timeline, and both looked identical in the
finished video until somebody watched it.

That single word is the reason a week went into "why are the clips random".
They were not all random. About a tenth of them were exact. Nothing said
which tenth.

## The three tiers

**A — verified.** The moment is known, not inferred. A quoted line matched
in the local subtitles, or a time somebody typed. Safe to place without
anybody looking.

**B — plausible.** Probably the right area, definitely not certain: laid
between two verified points in the same run, or found by picture above the
episode's own noise floor. Worth showing a person; not worth trusting.

**C — unresolved.** The right episode and nothing more, or not even that.
Pacing, filler, a repeated shot used to avoid a hole.

## The three modes

**Strict** places Tier A and nothing else. Everything below becomes a
visible NEEDS VISUAL card holding the exact duration. This is the mode
worth having: the part the tool fills can be trusted completely, and the
part it does not fill is bounded, obvious editor work. A visible gap is a
task. A confident wrong clip makes somebody re-check the whole video.

**Balanced** places A and B, marking B. C becomes a card.

**Draft** places everything, exactly as this tool behaved before tiers
existed. Useful for a rough cut; never to be described as accuracy.

## The rule that must not bend

No score, no combination of weak signals, and no desire to avoid a hole may
move a placement up a tier. `paced` and `filler` exist to cover holes, so
they can never be the thing that proves there is no hole.
"""
from __future__ import annotations

STRICT, BALANCED, DRAFT = "strict", "balanced", "draft"
MODES = (STRICT, BALANCED, DRAFT)

# What each placement method is worth, at most.
#
# "At most" is the important half. A method can be demoted by something
# else going wrong — an unknown character, a source that does not exist —
# but nothing can promote it beyond the row below.
CEILING = {
    "stated": "A",        # a time a person typed; never inferred
    "anchor": "A",        # a quoted line found in the local subtitles
    "chosen": "A",        # a person picked this in the editor
    "verified": "B",      # the picture agreed with the description
    "picture": "B",       # the picture found it, above the noise floor
    "vlm": "B",           # a vision model picked this frame from a window
    "interpolated": "B",  # laid between two placed shots of the same run
    "paced": "C",         # laid in order across a guessed stretch
    "filler": "C",        # the right episode, no particular moment
    "none": "C",
}

WHY = {
    "A": "quoted line ya di hui timing — exact",
    "B": "sahi hisse me hai, par exact moment pakka nahi",
    "C": "sirf sahi episode — moment ka koi saboot nahi",
}


def tier_of(method: str, stated: bool = False) -> str:
    """The most this placement can be worth.

    `stated` lifts an inferred placement only as far as B, never to A: a
    typed range says which four minutes, not which second. The shot inside
    it is still a guess — a much better guess than filler, and still a
    guess.
    """
    top = CEILING.get(str(method or "none"), "C")
    if stated and top == "C":
        return "B"
    return top


def places(mode: str, tier: str) -> bool:
    """Does this mode put this tier on screen?"""
    if mode == DRAFT:
        return True
    if mode == BALANCED:
        return tier in ("A", "B")
    return tier == "A"


def normalise(mode) -> str:
    got = str(mode or "").strip().lower()
    return got if got in MODES else BALANCED


def summary(counts: dict) -> str:
    """One line for a log: how much of this video is worth trusting."""
    total = sum(counts.values()) or 1
    return (f"Tier A {counts.get('A', 0)} · Tier B {counts.get('B', 0)} · "
            f"Tier C {counts.get('C', 0)}  "
            f"({counts.get('A', 0) * 100 // total}% exact)")
