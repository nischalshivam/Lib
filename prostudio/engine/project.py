"""Editable project = a saved plan (job + shots + text events) the browser
review page shows and mutates before the final export.

Round-trips through plain JSON so nothing but stdlib is needed, and the user's
edits (replace a clip, trim, delete, reorder) survive a tool restart.
"""
from __future__ import annotations

import dataclasses
import json
import os

from .planner import Shot
from .script_nlp import Chunk


def shot_to_dict(sh: Shot) -> dict:
    return {"path": sh.path, "kind": sh.kind, "t0": sh.t0, "t1": sh.t1,
            "scene_i": sh.scene_i, "mood": sh.mood, "zoom_in": sh.zoom_in,
            "punch_in": sh.punch_in, "drift_seed": sh.drift_seed,
            "src_in": getattr(sh, "src_in", 0.0),
            "framing": getattr(sh, "framing", ""),
            "move": getattr(sh, "move", ""), "transition": sh.transition}


def shot_from_dict(d: dict) -> Shot:
    return Shot(path=d["path"], kind=d["kind"], t0=d["t0"], t1=d["t1"],
               scene_i=d["scene_i"], mood=d.get("mood", "neutral"),
               zoom_in=d.get("zoom_in", True), punch_in=d.get("punch_in", False),
               drift_seed=d.get("drift_seed", 0), src_in=d.get("src_in", 0.0),
               framing=d.get("framing", ""), move=d.get("move", ""),
               transition=d.get("transition"))


def event_to_dict(ev) -> dict:
    t0, t1, si, ch, zone = ev
    return {"t0": t0, "t1": t1, "si": si, "zone": zone,
            "chunk": {"text": ch.text, "word_index": ch.word_index,
                      "n_words": ch.n_words, "crucial": ch.crucial,
                      "colors": ch.colors}}


def event_from_dict(d: dict):
    c = d["chunk"]
    ch = Chunk(text=c["text"], word_index=c["word_index"],
               n_words=c["n_words"], crucial=c.get("crucial", False),
               colors=c.get("colors", []))
    return (d["t0"], d["t1"], d["si"], ch, d["zone"])


def narration_by_shot(shots, words, scenes, windows) -> list:
    """The narration text spoken UNDER each shot's [t0,t1] — so the review page
    shows 'this clip plays while the voice says …' (the whole point of the
    check). Uses whisper word times when available, else the scene narration
    split proportionally across the scene's shots."""
    out = []
    if words:
        for sh in shots:
            spoken = [w for (w, s, e) in words if sh.t0 - 0.2 <= s < sh.t1]
            out.append(" ".join(spoken).strip())
        return out
    # no whisper: divide each scene's narration across its shots by time share
    for si, scene in enumerate(scenes):
        sc_shots = [sh for sh in shots if sh.scene_i == si]
        toks = scene.narration.split()
        if not sc_shots:
            continue
        total = sum(sh.secs for sh in sc_shots) or 1.0
        idx = 0
        for k, sh in enumerate(sc_shots):
            share = sh.secs / total
            take = len(toks) - idx if k == len(sc_shots) - 1 else \
                round(len(toks) * share)
            out.append(" ".join(toks[idx:idx + take]).strip())
            idx += take
    return out


def save_project(path, job, shots, events, scenes, windows, words,
                 audio_duration=0.0, proxy=""):
    data = {
        "job": dataclasses.asdict(job),
        "shots": [shot_to_dict(s) for s in shots],
        "events": [event_to_dict(e) for e in events],
        "narration_by_shot": narration_by_shot(shots, words, scenes, windows),
        "scene_narration": [s.narration for s in scenes],
        "audio_duration": audio_duration,
        "proxy": proxy,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return data


def load_project(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    shots = [shot_from_dict(d) for d in data["shots"]]
    events = [event_from_dict(d) for d in data["events"]]
    return data, shots, events
