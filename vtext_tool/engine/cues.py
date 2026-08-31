"""Resolve NARRATION_CUEs to timestamps using the aligned script words."""
from __future__ import annotations

import difflib

from .util import norm_words


def resolve_cues(events: list, aligned: list, log=print):
    """Attach ev['t_start'] / ev['t_cue_end'] (seconds) to each event.

    aligned: list of {w, s, e, matched} per script word (in script order).
    Events whose cue can't be located, or whose words were never spoken in
    the audio, get ev['skip_reason'] instead. Search is in script order
    (a cue is looked up after the previous event's position).
    """
    script = [a["w"] for a in aligned]
    pos = 0
    resolved = 0
    for ev in events:
        cue = norm_words(ev["NARRATION_CUE"])
        idx = _find(script, cue, pos)
        if idx is None:
            idx = _find(script, cue, 0)          # out-of-order fallback
        if idx is None:
            idx = _fuzzy_find(script, cue, pos)  # last resort
        if idx is None:
            ev["skip_reason"] = "cue not found in script"
            continue
        span = aligned[idx: idx + len(cue)]
        timed = [w for w in span if w["s"] is not None]
        if len(timed) < max(1, len(cue) // 2):
            ev["skip_reason"] = "cue outside audio (not spoken in this video)"
            continue
        ev["t_start"] = timed[0]["s"]
        ev["t_cue_end"] = timed[-1]["e"]
        ev["script_idx"] = idx
        ev["cue_span"] = span
        pos = idx + len(cue)
        resolved += 1
    log(f"[cues] resolved {resolved}/{len(events)} events to timestamps")

    # drop any accidental time-overlap between consecutive resolved events
    prev = None
    for ev in events:
        if "t_start" not in ev:
            continue
        if prev is not None and ev["t_start"] < prev:
            ev["t_start"] = prev + 0.15
        prev = ev["t_start"]
    return events


def _find(script, cue, start):
    n, m = len(script), len(cue)
    for i in range(start, n - m + 1):
        if script[i:i + m] == cue:
            return i
    return None


def _fuzzy_find(script, cue, start, min_ratio=0.72):
    """Sliding-window best fuzzy match (handles 1-2 word differences)."""
    n, m = len(script), len(cue)
    best, best_r = None, min_ratio
    lo = max(0, start - 40)
    for i in range(lo, n - m + 1):
        r = difflib.SequenceMatcher(a=cue, b=script[i:i + m]).ratio()
        if r > best_r:
            best, best_r = i, r
    return best
