"""Per-show typography: the font that looks like the show's own title logo, an
accent colour from the logo/branding, and whether the show sets type in CAPS.
Chosen once per show by Gemini comparing the verified logo against rendered
samples of ~45 free fonts (all of them cover es/pt/fr/de accents). Cached in
cache/styles.json; hand overrides in OVERRIDES win."""
from __future__ import annotations

import glob, io, json, os
from PIL import Image, ImageDraw, ImageFont

from .common import CACHE, json_read, json_update, vision
from . import assets

FONTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
STYLES = os.path.join(CACHE, "styles.json")
# proven by the user-approved samples (2026-10-05)
OVERRIDES = {
    "malcolminthemiddle": {"font": "PermanentMarker-Regular.ttf", "accent": "#F7A21B", "upper": True, "stroke": True},
    "youngsheldon": {"font": "ZillaSlab-Bold.ttf", "accent": "#6EC6FF", "upper": False, "stroke": False},
}
BODY = "Kalam-Bold.ttf"          # handwritten annotation / role lines


def _fonts():
    return sorted(os.path.basename(f) for f in glob.glob(os.path.join(FONTS, "*.ttf")))


def _font(path, size):
    f = ImageFont.truetype(path, size)
    try:
        axes = f.get_variation_axes()
        f.set_variation_by_axes([700 if a.get("name", b"").lower() in (b"weight", "weight") else a["default"]
                                 for a in axes])
    except Exception:
        pass
    return f


def _sheet(show, logo_path, fonts, upper):
    cols, cw, ch = 4, 600, 120
    rows = (len(fonts) + cols - 1) // cols
    top = 260
    S = Image.new("RGB", (cols * cw, top + rows * ch), (30, 30, 30))
    d = ImageDraw.Draw(S)
    if logo_path:
        L = Image.open(logo_path).convert("RGBA")
        L.thumbnail((cols * cw - 40, top - 20))
        S.paste(L, (20, 10), L)
    num = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", 26)
    text = show.upper() if upper else show
    for i, f in enumerate(fonts):
        x, y = (i % cols) * cw, top + (i // cols) * ch
        d.text((x + 8, y + 8), str(i + 1), font=num, fill=(255, 220, 0))
        size = 60
        ft = _font(os.path.join(FONTS, f), size)
        while ft.getlength(text) > cw - 70 and size > 18:
            size -= 4
            ft = _font(os.path.join(FONTS, f), size)
        d.text((x + 50, y + 25), text, font=ft, fill=(255, 255, 255))
    b = io.BytesIO()
    S.save(b, "JPEG", quality=82)
    return b.getvalue()


def style(key: str, show: str, log=print) -> dict:
    db = json_read(STYLES, {})
    if key in OVERRIDES:
        return dict(OVERRIDES[key], key=key, show=show, logo=assets.logo(key, show, log), body=BODY)
    if key in db:
        return db[key]
    lg = assets.logo(key, show, log)
    fonts = _fonts()
    v = None
    if lg:
        v = vision(f'The top image is the title logo of "{show}". Below are numbered samples of {len(fonts)} fonts '
                   f'writing the title. Pick the 2 fonts whose LETTERFORMS look most like the logo (serif vs sans, '
                   'weight, width, texture/distress, era). Also: is the logo set in ALL CAPS? And give one accent '
                   'colour (hex) that represents the show\'s branding (from the logo if it has colour). '
                   'Reply ONLY JSON {"best":[n,n],"caps":true|false,"accent":"#RRGGBB","why":"<short>"}',
                   _sheet(show, lg, fonts, upper=True))
    if not v or not v.get("best"):
        # no logo: ask for a mood-based pick from the same sheet without a logo
        v = vision(f'Numbered font samples. Which 2 fonts best match the mood/era of the TV show or film "{show}"? '
                   'Also an accent colour hex that fits its look, and whether its title is usually in caps. '
                   'Reply ONLY JSON {"best":[n,n],"caps":true|false,"accent":"#RRGGBB","why":"<short>"}',
                   _sheet(show, None, fonts, upper=True)) or {"best": [fonts.index("Montserrat_wght.ttf") + 1], "caps": True,
                                                              "accent": "#F2C230"}
    pick = fonts[int(v["best"][0]) - 1] if 1 <= int(v["best"][0]) <= len(fonts) else "Montserrat_wght.ttf"
    st = {"key": key, "show": show, "font": pick, "accent": v.get("accent") or "#F2C230", "upper": bool(v.get("caps", True)),
          "stroke": False, "logo": lg, "body": BODY, "why": v.get("why", "")}
    db[key] = st
    json_update(STYLES, lambda d: d.update(db))
    log(f"  style {show}: {pick} accent {st['accent']} caps={st['upper']} ({st['why'][:60]})")
    return st


def preview(keys_shows, out_jpg):
    """One sheet: every show's logo + its chosen font + accent, for a human glance."""
    rows = []
    for key, show in keys_shows:
        st = style(key, show)
        im = Image.new("RGB", (1800, 220), (25, 25, 25))
        d = ImageDraw.Draw(im)
        if st.get("logo"):
            L = Image.open(st["logo"]).convert("RGBA")
            L.thumbnail((520, 190))
            im.paste(L, (15, 15), L)
        ft = _font(os.path.join(FONTS, st["font"]), 84)
        word = "The Real Story" if not st["upper"] else "THE REAL STORY"
        d.text((600, 30), word, font=ft, fill=(255, 255, 255))
        d.text((600, 130), (show.upper() if st["upper"] else show)[:28], font=_font(os.path.join(FONTS, st["font"]), 56), fill=st["accent"])
        d.text((1500, 180), st["font"][:22], font=ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 20), fill=(150, 150, 150))
        rows.append(im)
    S = Image.new("RGB", (1800, 220 * len(rows)))
    for i, r in enumerate(rows):
        S.paste(r, (0, i * 220))
    S.save(out_jpg, quality=85)
    return out_jpg
