"""Export a built project as the ResearchCut Automate handoff.

ResearchCut 3.0's Automate stage (the pro-effects engine) reads a
`researchcut-automation-beats-v1` JSON: a `beats` array, one entry per visual on
the approved timeline, carrying that clip's absolute start/end and the narration
around it. ResearchCut attaches the finishing (kinetic callouts, annotations,
transitions) on top — it never changes the clips or audio. This module turns the
`timeline.json` that `makevideo` writes into exactly that file.

Contract (from ResearchCut): each beat needs `id`, `clipId`+`clipIndex`, `start`,
`end`, `narration`. Optional `emphasisPhrase`/`intent`/`focus` are LEFT OUT here —
ResearchCut extracts emphasis itself and, crucially, will not invent an annotation
without real focus coordinates, so omitting `focus` is the honest default until
media_index can supply face/subject positions.
"""
from __future__ import annotations

import json
import os
import re


SCHEMA = "researchcut-automation-beats-v1"
DEFAULT_FPS = 30


def _clip_id(file: str, idx: int) -> str:
    """A stable per-clip id that survives timeline reordering better than a bare
    index — ResearchCut prefers `clipId`. Built from the cut file's name."""
    stem = re.sub(r"[^a-z0-9]+", "", os.path.splitext(os.path.basename(file or ""))[0].lower())
    return f"c_{idx:04d}_{stem}" if stem else f"c_{idx:04d}"


def from_timeline(timeline: dict, name: str = "") -> dict:
    """Build the handoff dict from a loaded timeline.json."""
    scenes = timeline.get("scenes") or []
    beats = []
    idx = 0
    for sc in scenes:
        base = float(sc.get("start") or 0.0)
        narration = str(sc.get("narration") or "")
        for it in (sc.get("items") or []):
            start = base + float(it.get("start") or 0.0)
            end = start + float(it.get("duration") or 0.0)
            beats.append({
                "id": f"beat_{idx + 1:04d}",
                "clipId": _clip_id(it.get("file", ""), idx),
                "clipIndex": idx,
                "start": round(start, 3),
                "end": round(end, 3),
                "narration": narration,
                # optional fields intentionally omitted (see module docstring):
                # ResearchCut auto-extracts emphasis and won't annotate without
                # real focus coordinates.
            })
            idx += 1
    return {
        "schema": SCHEMA,
        "name": name or timeline.get("video") or "media_index project",
        "project": {
            "id": re.sub(r"[^a-z0-9]+", "_", (name or "project").lower())[:40] or "project",
            "fps": DEFAULT_FPS,
            "duration": round(float(timeline.get("total_seconds") or 0.0), 3),
        },
        "beats": beats,
    }


def export(build_dir: str, out: str = "") -> str:
    """Read `<build_dir>/timeline.json`, write the handoff JSON, return its path."""
    tl_path = os.path.join(build_dir, "timeline.json")
    with open(tl_path, "r", encoding="utf-8") as f:
        timeline = json.load(f)
    data = from_timeline(timeline, name=os.path.basename(build_dir.rstrip("/\\")))
    out = out or os.path.join(build_dir, "researchcut_beats.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return out
