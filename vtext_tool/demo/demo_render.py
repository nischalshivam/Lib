"""Demo: competitor-style kinetic text overlays for the Gus Fring clip.

Reads word timestamps from gus/align.json (forced alignment of the clean
script), renders per-frame RGBA overlays (Poppins, yellow/white/gray,
line-by-line pop-in synced to the spoken word), then a single ffmpeg pass
composites them onto the video. Positions were chosen per shot so text sits
in empty/dark areas away from faces.
"""
import json
import math
import os
import re

from PIL import Image, ImageDraw, ImageFilter, ImageFont

BASE = os.path.dirname(os.path.abspath(__file__))
W, H = 854, 480
FPS = 30
DUR = 60.98
N_FRAMES = int(DUR * FPS) + 1

WHITE = (255, 255, 255)
YELLOW = (255, 214, 10)
GRAY = (158, 158, 158)

FONT_PATH = os.path.join(BASE, "Poppins-ExtraBold.ttf")

align = json.load(open(os.path.join(BASE, "gus", "align.json")))
for a in align:
    a["w"] = re.sub(r"\(\d+\)$", "", a["w"])


def t(phrase, after=0.0):
    """Start time of the first occurrence of phrase at/after `after`."""
    ph = phrase.lower().split()
    for i in range(len(align) - len(ph) + 1):
        if align[i]["s"] < after:
            continue
        if [x["w"] for x in align[i:i + len(ph)]] == ph:
            return align[i]["s"]
    raise KeyError(phrase)


# (text, color, size, anchor_time)
EVENTS = [
    dict(x=0.07, y=0.28, align="left", end=4.15, lines=[
        ("Two People", WHITE, 38, t("two people")),
        ("Think They're", GRAY, 34, t("think")),
        ("About To Die", YELLOW, 46, t("about to die")),
    ]),
    dict(x=0.94, y=0.12, align="right", end=7.20, lines=[
        ("He Doesn't Say", WHITE, 36, t("doesn't say")),
        ("A Single Word", YELLOW, 44, t("a single word")),
    ]),
    dict(x=0.07, y=0.60, align="left", end=11.80, lines=[
        ("Calm", WHITE, 40, t("calm")),
        ("And Unhurried", YELLOW, 40, t("and unhurried")),
    ]),
    dict(x=0.07, y=0.20, align="left", end=24.05, lines=[
        ("One Of The Most", WHITE, 34, t("one of the most", after=19.0)),
        ("Disturbing", YELLOW, 48, t("disturbing")),
        ("Scenes Ever", WHITE, 38, t("scenes")),
        ("On Television", GRAY, 34, t("on television")),
    ]),
    dict(x=0.07, y=0.58, align="left", end=26.20, lines=[
        ("One Begins", WHITE, 34, t("begins")),
        ("To Beg", YELLOW, 44, t("to beg")),
    ]),
    dict(x=0.07, y=0.30, align="left", end=29.60, lines=[
        ("And Gus Just", WHITE, 36, t("and gus just")),
        ("Gets Dressed", YELLOW, 48, t("gets dressed")),
    ]),
    dict(x=0.06, y=0.09, align="left", end=33.20, lines=[
        ("He Picks Up", WHITE, 34, t("picks up", after=29.0)),
        ("The Box Cutter", YELLOW, 46, t("box cutter")),
    ]),
    dict(x=0.38, y=0.15, align="left", end=38.20, lines=[
        ("They Read It", WHITE, 34, t("read it")),
        ("As RAGE", YELLOW, 50, t("rage")),
    ]),
    dict(x=0.08, y=0.72, align="left", end=43.20, lines=[
        ("That Reading", WHITE, 34, t("that reading")),
        ("Is WRONG", YELLOW, 50, t("wrong")),
    ]),
    dict(x=0.05, y=0.08, align="left", end=52.90, lines=[
        ("He Does", WHITE, 34, t("he does not", after=49.0)),
        ("Not Explode", YELLOW, 46, t("explode")),
    ]),
    dict(x=0.46, y=0.12, align="left", end=56.60, lines=[
        ("Into Procedure.", WHITE, 38, t("into procedure")),
        ("Into Ritual.", YELLOW, 46, t("into ritual")),
    ]),
    dict(x=0.10, y=0.30, align="left", end=60.90, lines=[
        ("He Walks", WHITE, 36, t("walks straight")),
        ("Straight Past", YELLOW, 46, t("straight past")),
        ("The Men Who", GRAY, 34, t("the two men", after=58.0)),
        ("Crossed Him", WHITE, 40, t("crossed him", after=58.0)),
    ]),
]

POP = 0.20     # pop-in duration per line (s)
FADE = 0.25    # event fade-out (s)
_fonts = {}


def font(size):
    if size not in _fonts:
        _fonts[size] = ImageFont.truetype(FONT_PATH, size)
    return _fonts[size]


def render_line(text, color, size):
    """Pre-render one line (text + soft shadow) as RGBA, return (img, pad)."""
    f = font(size)
    d = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    box = d.textbbox((0, 0), text, font=f)
    pad = 14
    img = Image.new("RGBA", (box[2] - box[0] + pad * 2, box[3] - box[1] + pad * 2), (0, 0, 0, 0))
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).text((pad - box[0], pad - box[1] + 3), text, font=f, fill=(0, 0, 0, 210))
    sh = sh.filter(ImageFilter.GaussianBlur(4))
    img.alpha_composite(sh)
    ImageDraw.Draw(img).text((pad - box[0], pad - box[1]), text, font=f, fill=color + (255,))
    return img, pad


for ev in EVENTS:
    y = int(ev["y"] * H)
    rendered = []
    for (text, color, size, anchor) in ev["lines"]:
        img, pad = render_line(text, color, size)
        rendered.append(dict(img=img, pad=pad, anchor=anchor, ytop=y))
        y += int(size * 1.32)
    ev["rl"] = rendered
    ev["start"] = min(l["anchor"] for l in rendered)

OUT = os.path.join(BASE, "ovframes")
os.makedirs(OUT, exist_ok=True)

blank_path = os.path.join(OUT, "blank.png")
Image.new("RGBA", (W, H), (0, 0, 0, 0)).save(blank_path)


def ease_out(p):
    return 1 - (1 - p) ** 3


for n in range(N_FRAMES):
    tt = n / FPS
    path = os.path.join(OUT, f"ov_{n:04d}.png")
    active = [e for e in EVENTS if e["start"] <= tt < e["end"]]
    if not active:
        if os.path.exists(path):
            os.remove(path)
        os.link(blank_path, path)
        continue
    frame = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    for ev in active:
        ev_alpha = 1.0
        if tt > ev["end"] - FADE:
            ev_alpha = max(0.0, (ev["end"] - tt) / FADE)
        for l in ev["rl"]:
            age = tt - l["anchor"]
            if age < 0:
                continue
            p = min(age / POP, 1.0)
            scale = 0.70 + 0.30 * ease_out(p)
            a = ev_alpha * min(age / (POP * 0.6), 1.0)
            img = l["img"]
            wpx, hpx = img.size
            sw, sh_ = max(1, int(wpx * scale)), max(1, int(hpx * scale))
            im2 = img.resize((sw, sh_), Image.LANCZOS) if scale < 0.999 else img
            if a < 0.999:
                im2 = im2.copy()
                im2.putalpha(im2.getchannel("A").point(lambda v: int(v * a)))
            if ev["align"] == "left":
                x0 = int(ev["x"] * W) - l["pad"]
            else:
                x0 = int(ev["x"] * W) - sw + l["pad"]
            yc = l["ytop"] + hpx // 2
            frame.alpha_composite(im2, (x0, yc - sh_ // 2))
    frame.save(path)

print("frames done:", N_FRAMES)
