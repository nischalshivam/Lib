"""A human-labelled benchmark, and the honest metrics it makes possible.

This is the one thing every accuracy claim in this project has been missing,
and the reason the same argument kept repeating. The tool could say
"placeable", "moved", "Tier B", "23% exact", "0 cards" — and none of those
say whether the footage on screen is the right footage. Only a person
watching the video can say that, and until their verdict is written down and
counted, every "it's better now" and every "it's worse now" is a guess.

So this module is deliberately small and does exactly two things:

  1. Turn a finished build's manifest into a **labelling sheet** — one row
     per scene, with the narration and what the tool placed, and an empty
     verdict column. The person watches the video once and writes, for each
     scene, one of: exact / ok / wrong / none.

  2. Read that filled sheet back and compute the **only numbers that mean
     anything**: of the scenes the tool auto-filled, how many are right
     (precision); of all scenes, how many it filled at all (coverage); and
     the same split by how the shot was placed, so a wrong "Tier B" can no
     longer hide inside a healthy-looking total.

The verdict vocabulary is kept to four words on purpose — a tired person
labelling forty scenes needs no more:

    exact  — the right moment of the right scene
    ok     — right scene/character, slightly off moment; usable
    wrong  — wrong footage
    none   — a card / blank / nothing was placed

Nothing here changes a build. It measures one. That separation is the point:
the solver may never again be tuned against a number the solver produced.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

# The four words a person writes, and everything they might reasonably type
# for each, folded to one. Kept forgiving because the sheet is filled by hand
# in a text editor at the end of a long day.
VERDICTS = ("exact", "ok", "wrong", "none")
_ALIASES = {
    "exact": "exact", "e": "exact", "perfect": "exact", "right": "exact",
    "ok": "ok", "okay": "ok", "acceptable": "ok", "fine": "ok", "close": "ok",
    "wrong": "wrong", "w": "wrong", "bad": "wrong", "no": "wrong",
    "none": "none", "card": "none", "blank": "none", "missing": "none",
    "": "",
}


def normalise(verdict) -> str:
    return _ALIASES.get(str(verdict or "").strip().lower(), "")


# Counted as a real placement the tool is responsible for. `none` is not the
# tool being wrong — it is the tool honestly declining, which is a different
# thing and must not be counted against precision.
PLACED = ("exact", "ok", "wrong")


@dataclass
class Row:
    request_id: str
    scene: int
    narration: str
    placed: str                 # human summary of what the tool put here
    method: str                 # anchor | vlm | interpolated | paced | filler | none
    tier: str                   # A | B | C
    verdict: str = ""           # filled by the person
    note: str = ""

    @property
    def judged(self) -> bool:
        return self.verdict in PLACED or self.verdict == "none"


def request_id(scene: int, shot: int = 0) -> str:
    """A stable ID for a scene (or a shot within it), independent of range text.

    The denominator problem, fixed: `beat_018` names the same thing on every
    run, so two builds can be compared row for row.
    """
    base = f"beat_{int(scene):03d}"
    return f"{base}_shot_{int(shot):02d}" if shot else base


def _primary(assets: list) -> dict:
    """The asset that mostly decides what a scene looks like: its first clip,
    or its first still, or the card. That one carries the scene's verdict."""
    for kind in ("video", "image"):
        for a in assets or []:
            if a.get("kind") == kind and a.get("placed_by") != "needs_visual":
                return a
    return (assets or [{}])[0] if assets else {}


def _clock(seconds) -> str:
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return "?"
    return f"{s // 60}:{s % 60:02d}"


def rows_from_manifest(manifest: dict) -> list:
    """One Row per scene, summarising what the tool placed there."""
    out = []
    for sc in manifest.get("scenes", []):
        no = sc.get("scene")
        if no is None:
            continue
        a = _primary(sc.get("assets", []))
        method = str(a.get("placed_by") or "none")
        if method == "needs_visual":
            placed = "NEEDS VISUAL card"
        else:
            placed = (f"{a.get('source') or '?'} @ {_clock(a.get('source_start'))} "
                      f"({method})")
        out.append(Row(
            request_id=request_id(no),
            scene=int(no),
            narration=str(sc.get("narration") or "")[:200],
            placed=placed,
            method=method,
            tier=str(a.get("tier") or "C"),
        ))
    return out


_HEADER = ["request_id", "scene", "verdict", "narration", "tool_placed",
           "method", "tier", "note"]


def write_template(rows: list) -> str:
    """The labelling sheet as CSV text. `verdict` is the only empty column."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_HEADER)
    for r in rows:
        w.writerow([r.request_id, r.scene, r.verdict, r.narration, r.placed,
                    r.method, r.tier, r.note])
    return buf.getvalue()


def read_labels(text: str) -> list:
    """Parse a filled sheet back into Rows, tolerant of hand editing."""
    out = []
    reader = csv.DictReader(io.StringIO(text))
    for raw in reader:
        try:
            scene = int(raw.get("scene") or 0)
        except (TypeError, ValueError):
            continue
        # The verdict is meant to go in the `verdict` column, but people
        # naturally type into the LAST column (note) or the first empty one
        # they see — a real user did exactly that. So a verdict is taken from
        # `verdict` if present, otherwise from `note`, otherwise from ANY
        # cell that holds one of the four words. Losing 30 hand-typed
        # verdicts to a column mix-up would be the worst possible outcome.
        verdict = normalise(raw.get("verdict"))
        if not verdict:
            verdict = normalise(raw.get("note"))
        if not verdict:
            for v in raw.values():
                got = normalise(v)
                if got:
                    verdict = got
                    break
        out.append(Row(
            request_id=(raw.get("request_id") or request_id(scene)).strip(),
            scene=scene,
            narration=(raw.get("narration") or "").strip(),
            placed=(raw.get("tool_placed") or "").strip(),
            method=(raw.get("method") or "none").strip(),
            tier=(raw.get("tier") or "C").strip(),
            verdict=verdict,
            note=(raw.get("note") or "").strip(),
        ))
    return out


@dataclass
class Score:
    total: int = 0
    labelled: int = 0
    exact: int = 0
    ok: int = 0
    wrong: int = 0
    none: int = 0
    by_method: dict = field(default_factory=dict)   # method -> (right, placed)

    @property
    def placed(self) -> int:
        return self.exact + self.ok + self.wrong

    @property
    def precision(self) -> float:
        """Of the footage the tool AUTO-PLACED, how much is usable. The one
        number that answers "can I trust what it puts down"."""
        return (self.exact + self.ok) / self.placed if self.placed else 0.0

    @property
    def exact_precision(self) -> float:
        return self.exact / self.placed if self.placed else 0.0

    @property
    def coverage(self) -> float:
        """Of all scenes, how many the tool filled at all (right or wrong)."""
        return self.placed / self.total if self.total else 0.0

    def summary(self) -> str:
        if not self.labelled:
            return ("  gold: sheet me koi verdict nahi bhara. har row ki "
                    "'verdict' me exact/ok/wrong/none likho.")
        lines = [
            f"  gold: {self.labelled}/{self.total} scene labelled",
            f"    usable precision : {self.precision*100:4.0f}%  "
            f"(exact+ok) / auto-placed  [{self.exact + self.ok}/{self.placed}]",
            f"    exact precision  : {self.exact_precision*100:4.0f}%  "
            f"exact only               [{self.exact}/{self.placed}]",
            f"    coverage         : {self.coverage*100:4.0f}%  "
            f"scenes tool filled       [{self.placed}/{self.total}]",
            f"    exact {self.exact} {chr(183)} ok {self.ok} {chr(183)} "
            f"wrong {self.wrong} {chr(183)} card/none {self.none}",
        ]
        if self.by_method:
            lines.append("    kis tarah lage the, aur wo kitne sahi nikle:")
            for m, (right, placed) in sorted(self.by_method.items()):
                if placed:
                    lines.append(f"      {m:14s} {right}/{placed} usable "
                                 f"({100*right/placed:.0f}%)")
        return "\n".join(lines)


def score(rows: list) -> Score:
    """The honest read of a filled sheet. Unlabelled rows are ignored."""
    s = Score(total=len(rows))
    for r in rows:
        v = r.verdict
        if v not in PLACED and v != "none":
            continue
        s.labelled += 1
        setattr(s, v, getattr(s, v) + 1)
        right, placed = s.by_method.get(r.method, (0, 0))
        if v in PLACED:
            placed += 1
            if v in ("exact", "ok"):
                right += 1
        s.by_method[r.method] = (right, placed)
    return s
