"""Style resolution: niche preset x event type x repetition memory ->
a concrete render plan per event (fonts, colors, zone, animation, timing)."""
from __future__ import annotations

import json
import os

from .util import BASE_DIR, FONT_DIR, norm_words
from .zones import pick_zone

PACKS = {
    "bold_geometric":  {"primary": "Poppins-ExtraBold.ttf",  "secondary": "Poppins-Bold.ttf"},
    "modern_clean":    {"primary": "Inter-ExtraBold.ttf",    "secondary": "Poppins-Bold.ttf"},
    "editorial_serif": {"primary": "PlayfairDisplay-Bold.ttf", "secondary": "Inter-ExtraBold.ttf"},
    "classic_cinema":  {"primary": "LibreBaskerville-Bold.ttf", "secondary": "Oswald-SemiBold.ttf"},
    "condensed_impact": {"primary": "Oswald-SemiBold.ttf",   "secondary": "Inter-ExtraBold.ttf"},
    "playful_premium": {"primary": "ArchivoBlack.ttf",       "secondary": "Poppins-Bold.ttf"},
    "typewriter":      {"primary": "CourierPrime-Bold.ttf",  "secondary": "Oswald-SemiBold.ttf"},
}
_FALLBACK_FONT = "DejaVuSans-Bold.ttf"

# EVENT_TYPE -> behavior: size tier (fraction of frame height per main line),
# timing mode, hold bias, allowed families override, quote flag
BEHAVIOR = {
    "HOOK":               dict(tier=1.25, timing="phrase_build", hold=1.15),
    "REVELATION":         dict(tier=1.20, timing="post_impact",  hold=1.20),
    "EMOTIONAL_PEAK":     dict(tier=1.10, timing="word_hit",     hold=1.25,
                               families=["static_hold", "fade_rise"]),
    "CONTRAST":           dict(tier=1.10, timing="phrase_build", hold=1.00),
    "QUESTION":           dict(tier=1.10, timing="word_hit",     hold=1.15),
    "IMPORTANT_FACT":     dict(tier=1.00, timing="word_hit",     hold=1.00),
    "NUMBER_OR_DATE":     dict(tier=1.15, timing="pre_reveal",   hold=1.05),
    "CHARACTER_INSIGHT":  dict(tier=1.05, timing="phrase_build", hold=1.05),
    "QUOTE":              dict(tier=1.00, timing="word_hit",     hold=1.30,
                               quote=True),
    "CHAPTER_TRANSITION": dict(tier=1.30, timing="pre_reveal",   hold=1.15),
    "SETUP":              dict(tier=0.90, timing="word_hit",     hold=0.90),
    "NORMAL_EXPLANATION": dict(tier=0.85, timing="word_hit",     hold=0.90),
}
_INT_MUL = {"HIGH": 1.12, "MEDIUM": 1.0, "LOW": 0.88}
_SCALE_MUL = {"small": 0.85, "balanced": 1.0, "large": 1.18, "auto": 1.0}

_CONNECTORS = {"and", "the", "a", "an", "to", "of", "who", "that", "is",
               "was", "it", "in", "on", "his", "her", "their", "they're",
               "he", "she", "they", "but", "with", "from", "for"}


def load_niche(niche: str, overrides: dict):
    presets = json.load(open(os.path.join(BASE_DIR, "presets", "niches.json")))
    p = dict(presets.get(niche, presets["MOVIE_ESSAY"]))
    if overrides.get("pack") and overrides["pack"] != "auto":
        p["pack"] = overrides["pack"]
    if overrides.get("accent"):
        p["accent"] = list(overrides["accent"])
    if overrides.get("energy"):
        p["energy"] = float(overrides["energy"])
    return p


def font_path(name: str) -> str:
    p = os.path.join(FONT_DIR, name)
    return p if os.path.exists(p) else os.path.join(FONT_DIR, _FALLBACK_FONT)


def build_plans(events: list, niche_cfg: dict, cuts: list, video_h: int,
                zone_data: dict, opts: dict, duration: float = 1e9,
                log=print):
    """Produce render plans. events must already carry t_start/t_cue_end.
    zone_data: {ev_num: (zone_scores, zone_lum)}."""
    pack = PACKS.get(niche_cfg["pack"], PACKS["bold_geometric"])
    energy = float(niche_cfg.get("energy", 2.5))
    scale_mul = _SCALE_MUL.get(opts.get("text_scale", "auto"), 1.0)
    base_size = 0.075 * video_h  # main line height fraction

    resolved = [e for e in events if "t_start" in e
                and e["EVENT_TYPE"] != "BREATHING_MOMENT"]
    breathing = [(e["t_start"], e.get("t_cue_end", e["t_start"]) + 4.0)
                 for e in events
                 if "t_start" in e and e["EVENT_TYPE"] == "BREATHING_MOMENT"]

    plans, memory = [], {"zones": [], "families": []}
    seq_zone, seq_family = {}, {}

    for i, ev in enumerate(resolved):
        beh = BEHAVIOR[ev["EVENT_TYPE"]]
        nxt_start = (resolved[i + 1]["t_start"]
                     if i + 1 < len(resolved) else None)

        # --- timing -----------------------------------------------------
        t0 = ev["t_start"]
        cue_end = ev.get("t_cue_end", t0 + 1.5)
        if t0 > duration - 0.8:
            ev["skip_reason"] = "starts at/after end of video"
            continue
        if beh["timing"] == "pre_reveal":
            t0 = max(0.0, t0 - 0.30)
        elif beh["timing"] == "post_impact":
            t0 = min(cue_end + 0.10, t0 + 1.2)
        nwords = sum(len(l.split()) for l in ev["lines"])
        hold = (1.0 + 0.34 * nwords) * beh["hold"] * _INT_MUL[ev["INTENSITY"]]
        t1 = max(cue_end + 0.6, t0 + hold)
        t1 = min(t1, t0 + 5.2, duration - 0.05)
        if nxt_start is not None:
            t1 = min(t1, nxt_start - 0.12)
        # shot-cut protection: end at the first cut after the cue finishes
        if opts.get("cut_protect", True):
            for c in cuts:
                if cue_end + 0.05 < c < t1:
                    t1 = c - 0.06
                    break
        # never render inside an explicit breathing window
        skip = False
        for (b0, b1) in breathing:
            if t0 < b1 and t1 > b0:
                skip = True
        if skip:
            ev["skip_reason"] = "inside a BREATHING_MOMENT window"
            continue
        if t1 - t0 < 0.7:
            ev["skip_reason"] = "no room before next cut/event/video end"
            continue

        # --- zone -------------------------------------------------------
        zone_scores, zone_lum = zone_data.get(
            ev["num"], ({z: 0.5 for z in [(r, c) for r in range(3)
                                          for c in range(3)]}, {}))
        sg = ev.get("SEQUENCE_GROUP")
        if sg and sg in seq_zone:
            zone = seq_zone[sg]
        else:
            zone = pick_zone(zone_scores, memory["zones"])
            if sg:
                seq_zone[sg] = zone
        memory["zones"].append(zone)

        # --- animation family ------------------------------------------
        fams = beh.get("families") or niche_cfg["families"]
        if opts.get("families"):
            fams = [f for f in fams if f in opts["families"]] or fams
        if sg and sg in seq_family:
            family = seq_family[sg]
        else:
            family = _pick_family(fams, memory["families"])
            if sg:
                seq_family[sg] = family
        memory["families"].append(family)

        # --- lines: colors + sizes + anchors ---------------------------
        emph = set(norm_words(ev.get("EMPHASIS_WORDS", "")))
        lines = []
        caps = niche_cfg.get("caps", "title")
        n_lines = len(ev["lines"])
        for li, ltext in enumerate(ev["lines"]):
            words = ltext.split()
            lw = []
            line_has_emph = any(_iswhole(w, emph) for w in words)
            all_conn = all(w.lower().strip(".,!?") in _CONNECTORS
                           for w in words)
            for w in words:
                if _iswhole(w, emph):
                    col = tuple(niche_cfg["accent"])
                elif all_conn and n_lines >= 3 and not line_has_emph:
                    col = tuple(niche_cfg["dim"])
                else:
                    col = tuple(niche_cfg["base"])
                lw.append({"t": _caps(w, caps), "color": col})
            size = base_size * beh["tier"] * scale_mul
            if line_has_emph:
                size *= 1.22
            elif all_conn and n_lines >= 3:
                size *= 0.86
            lines.append({"words": lw, "size": int(size),
                          "anchor": _line_anchor(ev, li, n_lines, t0,
                                                 cue_end, beh["timing"])})

        plans.append({
            "num": ev["num"], "event_type": ev["EVENT_TYPE"],
            "t0": round(t0, 3), "t1": round(t1, 3),
            "zone": zone, "zone_lum": zone_lum.get(zone, 0.3),
            "family": family, "energy": energy,
            "font": font_path(pack["secondary"] if beh.get("quote")
                              else pack["primary"]),
            "lines": lines, "intensity": ev["INTENSITY"],
            "seq_group": sg,
        })
    log(f"[styles] {len(plans)} events planned "
        f"({len(events) - len(plans)} skipped)")
    return plans


def _iswhole(word, emph_set):
    from .util import norm_word
    return norm_word(word) in emph_set if emph_set else False


def _caps(w: str, mode: str) -> str:
    if mode == "upper":
        return w.upper()
    return w  # instruction files arrive in Title Case already


def _line_anchor(ev, li, n_lines, t0, cue_end, timing):
    """When does line li appear? phrase_build spreads lines across the cue;
    other modes reveal lines with a quick fixed stagger."""
    if timing == "phrase_build" and cue_end > t0 and n_lines > 1:
        span = min(cue_end - t0, 2.6)
        return round(t0 + span * li / n_lines, 3)
    return round(t0 + 0.14 * li, 3)


def _pick_family(candidates, recent):
    for f in candidates:
        if f not in recent[-2:]:
            return f
    return candidates[0]
