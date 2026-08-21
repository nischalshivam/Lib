"""10 format style packs — same pipeline, radically different looks.
F1/F2/F6/F9 were proven on real footage first; the rest are compositions of
the same proven primitives (grades, drift, xfade types, text engines)."""
from __future__ import annotations

import os

# Fonts are BUNDLED in the repo so on-screen text works on ANY OS (the tool is
# often run on Windows where Linux font paths don't exist -> "cannot open
# resource"). Resolution order per role: env override -> bundled font ->
# common system fonts (win/mac/linux) -> any bundled font that exists.
_BUNDLED = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "assets", "fonts")


def _resolve_font(env_var, bundled_name, system_candidates):
    for cand in ([os.environ.get(env_var)] if os.environ.get(env_var) else []) \
            + [os.path.join(_BUNDLED, bundled_name)] + system_candidates:
        if cand and os.path.isfile(cand):
            return cand
    # last resort: whatever bundled font we can find (text must never crash)
    for f in ("DejaVuSans-Bold.ttf", "DejaVuSerif-Bold.ttf",
              "DejaVuSansMono-Bold.ttf"):
        p = os.path.join(_BUNDLED, f)
        if os.path.isfile(p):
            return p
    return os.path.join(_BUNDLED, bundled_name)   # may not exist; caller warns


SANS = _resolve_font("PS_FONT_SANS", "DejaVuSans-Bold.ttf", [
    "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/segoeuib.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"])
SERIF = _resolve_font("PS_FONT_SERIF", "DejaVuSerif-Bold.ttf", [
    "C:/Windows/Fonts/timesbd.ttf", "C:/Windows/Fonts/georgiab.ttf",
    "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"])
MONO = _resolve_font("PS_FONT_MONO", "DejaVuSansMono-Bold.ttf", [
    "C:/Windows/Fonts/consolab.ttf", "C:/Windows/Fonts/couri.ttf",
    "/System/Library/Fonts/Supplemental/Courier New Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"])

# soft = within a scene, scene = at scene boundaries
FORMATS = {
    "F1_Cinematic": dict(
        desc="Netflix-style: slow push-ins, elegant serif fades, dissolve",
        font=SERIF, size=56, upper=True, spaced=True,
        anim="fade", border=0, shake=0.0, drift=0.35, grain=0,
        vignette=True, letterbox=False, glitch=False, sepia=False,
        pushin=True, spotlight=False, pan=None,
        soft=("dissolve", 0.6), scene=("fadeblack", 0.6)),
    "F2_Kinetic": dict(
        desc="High-retention: bold bounce text, hard cuts, shake, grain",
        font=SANS, size=82, upper=True, spaced=False,
        anim="bounce", border=3, shake=0.0, drift=0.6, grain=7,
        vignette=True, letterbox=False, glitch=False, sepia=False,
        pushin=False, spotlight=False, pan=None,
        soft=("fade", 0.12), scene=("fade", 0.15)),
    "F3_Archival": dict(
        desc="Dossier: typewriter mono text, sepia, shutter-flash cuts",
        font=MONO, size=58, upper=True, spaced=False,
        anim="type", border=1, shake=0.0, drift=0.45, grain=10,
        vignette=True, letterbox=False, glitch=False, sepia=True,
        pushin=True, spotlight=False, pan=None,
        soft=("fadewhite", 0.22), scene=("fadeblack", 0.5)),
    "F4_Depth": dict(
        desc="2.5D parallax feel: strong push + blur-separation (mask optional)",
        font=SANS, size=72, upper=True, spaced=False,
        anim="pop", border=2, shake=0.0, drift=0.5, grain=4,
        vignette=True, letterbox=False, glitch=False, sepia=False,
        pushin=True, spotlight=False, pan=None, strong_push=True,
        soft=("smoothleft", 0.5), scene=("fadeblack", 0.55)),
    "F5_Grid": dict(
        desc="Analytical: lower-third text, slide/wipe moves",
        font=SANS, size=52, upper=False, spaced=False,
        anim="fade", border=2, shake=0.0, drift=0.3, grain=0,
        vignette=False, letterbox=False, glitch=False, sepia=False,
        pushin=True, spotlight=False, pan=None, lower_third=True,
        soft=("slideleft", 0.45), scene=("wipeleft", 0.5)),
    "F6_Letterbox": dict(
        desc="Theatrical 21:9 bars, saturated, minimal subtitle text",
        font=SANS, size=44, upper=False, spaced=False,
        anim="fade", border=0, shake=0.0, drift=0.4, grain=0,
        vignette=False, letterbox=True, glitch=False, sepia=False,
        pushin=True, spotlight=False, pan=None,
        soft=("fade", 0.4), scene=("fadeblack", 0.5)),
    "F7_Glitch": dict(
        desc="Tech/industrial: chromatic pulses, mono decode text, pixel cuts",
        font=MONO, size=64, upper=True, spaced=False,
        anim="type", border=2, shake=0.0, drift=0.5, grain=6,
        vignette=True, letterbox=False, glitch=True, sepia=False,
        pushin=False, spotlight=False, pan=None,
        soft=("pixelize", 0.25), scene=("fadeblack", 0.4)),
    "F8_Horizontal": dict(
        desc="Timeline: continuous left-to-right pans, anchored text",
        font=SANS, size=64, upper=True, spaced=False,
        anim="pop", border=2, shake=0.0, drift=0.2, grain=3,
        vignette=True, letterbox=False, glitch=False, sepia=False,
        pushin=False, spotlight=False, pan="lr",
        soft=("smoothright", 0.55), scene=("smoothright", 0.7)),
    "F9_FocusPuller": dict(
        desc="Mystery: blur-snap reveals, centered serif",
        font=SERIF, size=64, upper=True, spaced=False,
        anim="pop", border=1, shake=0.0, drift=0.4, grain=0,
        vignette=True, letterbox=False, glitch=False, sepia=False,
        pushin=False, spotlight=False, pan=None,
        soft=("hblur", 0.5), scene=("fadeblack", 0.5)),
    "F10_Spotlight": dict(
        desc="Minimalist drama: darkened frame + center glow, thin serif",
        font=SERIF, size=60, upper=True, spaced=True,
        anim="fade", border=0, shake=0.0, drift=0.3, grain=4,
        vignette=True, letterbox=False, glitch=False, sepia=False,
        pushin=True, spotlight=True, pan=None,
        soft=("fadewhite", 0.4), scene=("fadeblack", 0.6)),
}

ROTATION = list(FORMATS)


def resolve_format(choice: str, job_index: int, rng) -> str:
    if choice and choice.lower() not in ("auto", "auto-rotate", "random", ""):
        for k in FORMATS:
            if choice.lower() in k.lower():
                return k
    if choice and choice.lower() == "random":
        return rng.choice(ROTATION)
    return ROTATION[job_index % len(ROTATION)]


# --------- sentiment grades (mood x niche) ---------------------------------
NICHE_BASE = {
    "True Crime & Espionage":
        "eq=contrast=1.11:saturation=0.96:brightness=-0.02,"
        "colorbalance=bs=0.08:bm=0.03:bh=-0.02",
    "Heavy Machinery & Engineering":
        "eq=contrast=1.10:saturation=1.05,colorbalance=bs=0.05:bh=-0.02:rh=0.02",
    "Internet Lore":
        "eq=contrast=1.08:saturation=1.15:brightness=-0.01,"
        "colorbalance=bs=0.05:rh=0.03",
    "Movie Essay":
        "eq=contrast=1.09:saturation=1.10:brightness=-0.012,"
        "colorbalance=bs=0.06:bm=0.02:bh=-0.03:rh=0.04",
    "Documentary":
        "eq=contrast=1.06:saturation=1.04,colorbalance=rh=0.02:bh=0.01",
    # entertainment niches (movies / cartoon / anime / old films)
    "Cartoon":
        "eq=contrast=1.06:saturation=1.22:brightness=0.008,"
        "colorbalance=rh=0.02:gh=0.01",
    "Anime":
        "eq=contrast=1.11:saturation=1.18:brightness=-0.006,"
        "colorbalance=bh=0.04:rh=0.02",
    "Old Movie":
        "eq=contrast=1.14:saturation=0.86:brightness=-0.02,"
        "colorbalance=rh=0.06:rm=0.03:bs=-0.03",
}

# themed border colour for the 'border' framing (cartoon/anime recap look)
NICHE_THEME = {
    "Cartoon": "0xF2B705", "Anime": "0xE23B6D", "Old Movie": "0xC9A24B",
    "Internet Lore": "0x3BA7E2",
}
DEFAULT_THEME = "0xE8C26A"

# per-niche framing pool (weights). 'full'=full-bleed, 'blurfill'=blur bg,
# 'card'=cinematic floating card, 'border'=themed frame, 'letterbox'=21:9 bars.
# Cartoon/anime lean into the framed recap look; cinema niches stay clean.
NICHE_FRAMING = {
    "Cartoon":   {"border": 60, "blurfill": 25, "full": 15},
    "Anime":     {"border": 55, "blurfill": 25, "full": 20},
    "Old Movie": {"full": 55, "letterbox": 22, "card": 23},
    "Movie Essay": {"full": 70, "card": 18, "letterbox": 12},
    "Internet Lore": {"full": 62, "card": 20, "border": 18},
}
DEFAULT_FRAMING = {"full": 78, "card": 14, "letterbox": 8}


def theme_color(niche: str) -> str:
    return NICHE_THEME.get(niche, DEFAULT_THEME)

MOOD_TWEAK = {
    "danger": ",eq=saturation=0.92:brightness=-0.018,colorbalance=bs=0.05",
    "success": ",eq=saturation=1.08:brightness=0.008,colorbalance=rh=0.05:gh=0.02",
    "neutral": "",
}


def grade_for(niche: str, mood: str, sepia: bool) -> str:
    g = NICHE_BASE.get(niche, NICHE_BASE["Movie Essay"]) + MOOD_TWEAK.get(mood, "")
    if sepia:
        g += ",colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131"
    return g
