"""Text layout + animation — EXACT metrics edition.

Words are measured with the real TTF via Pillow (no estimation), so words in a
line never collide and text never leaves the frame. Position varies per event
(the caller rotates zones) so it reads like an editor's motion-graphics, not
subtitles. Per-word colors preserved (keyword colorization).
"""
from __future__ import annotations

import json
import os

from PIL import ImageFont

LANG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "presets", "languages.json")
_LANGS = None
_FONTS = {}


def lang_cfg(code: str) -> dict:
    global _LANGS
    if _LANGS is None:
        with open(LANG_FILE, encoding="utf-8") as f:
            _LANGS = json.load(f)
    return _LANGS.get(code, _LANGS["en"])


def _font(path: str, size: int):
    key = (path, size)
    if key not in _FONTS:
        _FONTS[key] = ImageFont.truetype(path, size)
    return _FONTS[key]


def _ff_font(path: str) -> str:
    """Font path for ffmpeg drawtext. Single quotes already protect the drive
    colon and spaces (verified), but Windows backslashes are escape chars in a
    filtergraph — convert them to forward slashes."""
    return path.replace("\\", "/")


_NONLATIN_BLOCKS = [
    (0x0900, 0x097F, "Devanagari (Hindi/Marathi)"),
    (0x0980, 0x09FF, "Bengali"),
    (0x0A00, 0x0A7F, "Gurmukhi (Punjabi)"),
    (0x0B80, 0x0BFF, "Tamil"),
    (0x0600, 0x06FF, "Arabic (also right-to-left)"),
    (0x0590, 0x05FF, "Hebrew (also right-to-left)"),
    (0x4E00, 0x9FFF, "Chinese/Japanese (CJK)"),
    (0x3040, 0x30FF, "Japanese kana"),
    (0xAC00, 0xD7AF, "Korean"),
    (0x0E00, 0x0E7F, "Thai"),
]


def script_needs_font(text: str):
    """Return the script name if `text` uses a writing system the bundled
    Latin fonts don't cover (so the tool can warn and ask for a font). Latin-
    script European languages (incl. accents/diacritics) return None = fine."""
    for ch in text:
        o = ord(ch)
        for lo, hi, name in _NONLATIN_BLOCKS:
            if lo <= o <= hi:
                return name
    return None


def esc(t: str) -> str:
    # The text is emitted INSIDE single quotes in the drawtext filter, so the
    # only real hazards are the apostrophe (closes the quote) and the backslash
    # (escape-sequence ambiguity that some ffmpeg builds — notably Windows —
    # mis-handle, breaking the whole graph). Everything else (: , % . -) is safe
    # literally inside the quotes; we must NOT backslash-escape it, or Windows
    # ffmpeg rejects the filterchain with "Invalid argument".
    for ch in ("'", "’", "‘", "`", "´", '"', "“", "”"):
        t = t.replace(ch, "")
    return t.replace("\\", "").replace("—", "-").replace("…", "...")


# anchor (cx fraction, cy fraction) inside the VISIBLE area — many varied spots
ZONE_XY = {
    "bottom":       (0.50, 0.84),
    "top":          (0.50, 0.16),
    "lower_left":   (0.32, 0.80),
    "lower_right":  (0.68, 0.80),
    "upper_left":   (0.32, 0.22),
    "upper_right":  (0.68, 0.22),
    "center":       (0.50, 0.52),
}
# rotation order the planner cycles through for editorial variety
ZONE_ROTATION = ["bottom", "upper_right", "lower_left", "top",
                 "lower_right", "upper_left", "center"]


def _measure(font, words, space_w):
    widths = [font.getlength(w) for w in words]
    total = sum(widths) + space_w * (len(words) - 1)
    return widths, total


def chunk_filters(chunk, t0, t1, style, zone, W, H, lang="en", letterbox=False):
    cfg = lang_cfg(lang)
    fontpath = style["font"]
    scale = H / 1080.0
    base = max(22, int(style["size"] * scale))

    vis_h = int(W * 9 / 21) if letterbox else H
    vis_top = (H - vis_h) // 2
    margin = int(0.06 * W)
    safe_w = W - 2 * margin

    # clean each word up front (drop apostrophes/backslashes) so spacing and
    # width-measurement match exactly what gets drawn — e.g. DOESN'T -> DOESNT
    # -> "D O E S N T" (no stray double space)
    words = [w for w in (esc(w) for w in chunk.text.split()) if w]
    if not words:
        return []
    upper = style["upper"] and cfg["allow_upper"]
    disp = [w.upper() if upper else w for w in words]
    if style.get("spaced"):
        disp = [" ".join(list(w)) for w in disp]

    # fit: shrink until the whole line fits the safe width (EXACT measurement)
    fs = base
    while fs > 22:
        font = _font(fontpath, fs)
        space_w = font.getlength("  ")
        widths, total = _measure(font, disp, space_w)
        if total <= safe_w:
            break
        fs = int(fs * 0.92)
    font = _font(fontpath, fs)
    space_w = font.getlength("  ")
    widths, total = _measure(font, disp, space_w)

    cx_f, cy_f = ZONE_XY.get(zone, ZONE_XY["bottom"])
    x0 = int(cx_f * W - total / 2)
    x0 = max(margin, min(x0, W - margin - int(total)))
    y = int(vis_top + cy_f * vis_h - fs * 0.62)
    y = max(vis_top + int(0.03 * vis_h),
            min(y, vis_top + vis_h - int(fs * 1.25)))

    sx, sy = max(2, int(3 * scale)), max(3, int(4 * scale))
    filters, x = [], float(x0)
    for i, (dw, wd) in enumerate(zip(disp, widths)):
        color = chunk.colors[i] if i < len(chunk.colors) else "0xFFFFFF"
        # expressions are single-quoted below, so commas are LITERAL (no
        # backslash escaping — that breaks Windows ffmpeg).
        if style["anim"] == "type":
            s = t0 + 0.10 * i
            yexpr, alpha = str(y), f"if(lt(t,{s}),0,1)"
        elif style["anim"] == "bounce":
            s = t0 + 0.09 * i
            yexpr = (f"{y}+{int(24*scale)}*exp(-max(0,(t-{s}))*11)"
                     f"*cos((t-{s})*19)")
            alpha = f"if(lt(t,{s}),0,min(1,(t-{s})*9))"
        elif style["anim"] == "pop":
            s = t0 + 0.07 * i
            yexpr = f"{y}+{int(12*scale)}*exp(-max(0,(t-{s}))*13)"
            alpha = f"if(lt(t,{s}),0,min(1,(t-{s})*8))"
        else:  # fade
            s = t0 + 0.05 * i
            yexpr = str(y)
            alpha = (f"if(lt(t,{s}),0,if(lt(t,{s}+0.5),(t-{s})/0.5,"
                     f"if(lt(t,{t1-0.4}),1,max(0,({t1}-t)/0.4))))")
        filters.append(
            f"drawtext=fontfile='{_ff_font(fontpath)}':text='{esc(dw)}':fontsize={fs}"
            f":fontcolor={color}:borderw={style['border']}:bordercolor=black@0.92"
            f":shadowcolor=black@0.8:shadowx={sx}:shadowy={sy}"
            f":x={int(round(x))}:y='{yexpr}':alpha='{alpha}'"
            f":enable='between(t,{max(0,t0-0.05)},{t1})'")
        x += wd + space_w
    return filters
