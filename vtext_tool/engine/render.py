"""Overlay rendering: plans -> transparent PNG frames + a concat playlist.

Only frames inside event windows are rendered; the playlist holds a shared
blank frame between events, so a 2-hour video costs no more than its text.
"""
from __future__ import annotations

import math
import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont

_fonts = {}


def _font(path, size):
    key = (path, size)
    if key not in _fonts:
        _fonts[key] = ImageFont.truetype(path, size)
    return _fonts[key]


def _ease(p):
    return 1 - (1 - p) ** 3


def _render_word(text, font, color, heavy_shadow):
    pad = 16
    d = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    box = d.textbbox((0, 0), text, font=font)
    img = Image.new("RGBA", (box[2] - box[0] + pad * 2,
                             box[3] - box[1] + pad * 2), (0, 0, 0, 0))
    ox, oy = pad - box[0], pad - box[1]
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).text((ox, oy + 3), text, font=font,
                            fill=(0, 0, 0, 230 if heavy_shadow else 200))
    sh = sh.filter(ImageFilter.GaussianBlur(5 if heavy_shadow else 4))
    img.alpha_composite(sh)
    dr = ImageDraw.Draw(img)
    if heavy_shadow:  # thin stroke for bright/busy backgrounds
        for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
            dr.text((ox + dx, oy + dy), text, font=font, fill=(0, 0, 0, 150))
    dr.text((ox, oy), text, font=font, fill=tuple(color) + (255,))
    return img, pad


def layout_plans(plans, W, H):
    """Pre-render word images and compute pixel positions for every plan."""
    margin = int(0.05 * W)
    for pl in plans:
        r, c = pl["zone"]
        heavy = pl["zone_lum"] > 0.48
        max_w = int(W * (0.62 if c == 1 else 0.46))
        # measure at declared sizes, shrink all lines by a common factor
        shrink = 1.0
        for _ in range(4):
            widths = []
            for ln in pl["lines"]:
                f = _font(pl["font"], max(12, int(ln["size"] * shrink)))
                d = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
                wsum = 0
                for k, wd in enumerate(ln["words"]):
                    b = d.textbbox((0, 0), wd["t"], font=f)
                    wsum += b[2] - b[0]
                    if k:
                        wsum += int(f.size * 0.28)
                widths.append(wsum)
            if max(widths) <= max_w:
                break
            shrink *= max_w / max(widths)
        # build word images + line metrics
        gap = 0.30
        total_h, line_meta = 0, []
        for ln in pl["lines"]:
            size = max(12, int(ln["size"] * shrink))
            f = _font(pl["font"], size)
            imgs, lw = [], 0
            for k, wd in enumerate(ln["words"]):
                img, pad = _render_word(wd["t"], f, wd["color"], heavy)
                imgs.append({"img": img, "pad": pad})
                lw += img.size[0] - pad * 2 + (int(size * 0.28) if k else 0)
            lh = max(i["img"].size[1] - 2 * i["pad"] for i in imgs)
            line_meta.append({"imgs": imgs, "w": lw, "h": lh, "size": size,
                              "anchor": ln["anchor"]})
            total_h += lh + int(size * gap)
        # anchor the block inside its zone, clamped to safe margins
        zx0, zx1 = int(c * W / 3), int((c + 1) * W / 3)
        zy0 = int(r * H / 3)
        x_align = "left" if c == 0 else ("right" if c == 2 else "center")
        bx0 = max(margin, zx0 + int(0.02 * W))
        bx1 = min(W - margin, zx1 - int(0.02 * W)) if c == 2 else None
        by = zy0 + int(0.05 * H)
        by = min(by, H - margin - total_h)
        by = max(margin, by)
        y = by
        for lm in line_meta:
            if x_align == "left":
                lm["x"] = bx0
            elif x_align == "right":
                lm["x"] = bx1 - lm["w"]
            else:
                cx = (zx0 + zx1) // 2
                lm["x"] = max(margin, cx - lm["w"] // 2)
            lm["y"] = y
            y += lm["h"] + int(lm["size"] * 0.30)
        pl["_lines"] = line_meta
        pl["_align"] = x_align
    return plans


def _draw_plan(frame, pl, t):
    en = pl["energy"]
    pop = max(0.12, 0.26 - 0.03 * en)
    fade_out = 1.0
    if t > pl["t1"] - 0.22:
        fade_out = max(0.0, (pl["t1"] - t) / 0.22)
    fam = pl["family"]
    for lm in pl["_lines"]:
        age = t - lm["anchor"]
        if age < 0:
            continue
        p = min(1.0, age / pop)
        e = _ease(p)
        # per-line motion params
        dx = dy = 0
        scale = 1.0
        alpha = fade_out
        if fam == "fade_rise":
            alpha *= e
            dy = int((1 - e) * 0.35 * lm["size"] * (0.6 + 0.2 * en))
        elif fam == "scale_pop":
            alpha *= min(1.0, age / (pop * 0.6))
            scale = 0.70 + 0.30 * e
        elif fam == "slide_in":
            alpha *= e
            side = -1 if pl["_align"] == "left" else 1
            dx = int(side * (1 - e) * (18 + 10 * en))
        elif fam == "static_hold":
            alpha *= min(1.0, age / 0.35)
        elif fam == "mask_reveal":
            alpha *= min(1.0, age / (pop * 0.5))
        # word_build handled per word below
        x = lm["x"]
        reveal_w = None
        if fam == "mask_reveal":
            reveal_w = int(lm["w"] * e) if p < 1.0 else None
        drawn_w = 0
        for k, wi in enumerate(lm["imgs"]):
            img, pad = wi["img"], wi["pad"]
            wa = alpha
            wdx, wdy, wscale = dx, dy, scale
            if fam == "word_build":
                wanchor = lm["anchor"] + k * min(0.11, pop * 0.6)
                wage = t - wanchor
                if wage < 0:
                    break
                wp = _ease(min(1.0, wage / pop))
                wa = fade_out * min(1.0, wage / (pop * 0.5))
                wdy = int((1 - wp) * 0.3 * lm["size"])
                wscale = 0.82 + 0.18 * wp
            if wa <= 0.01:
                x += img.size[0] - 2 * pad + int(lm["size"] * 0.28)
                continue
            im2 = img
            if wscale < 0.999:
                nw = max(1, int(img.size[0] * wscale))
                nh = max(1, int(img.size[1] * wscale))
                im2 = img.resize((nw, nh), Image.LANCZOS)
            if wa < 0.995:
                im2 = im2.copy()
                a = im2.getchannel("A").point(lambda v: int(v * wa))
                im2.putalpha(a)
            px = x - pad + wdx + (img.size[0] - im2.size[0]) // 2
            py = (lm["y"] - pad + wdy
                  + (img.size[1] - im2.size[1]) // 2)
            if reveal_w is not None:
                ww = img.size[0] - 2 * pad
                if drawn_w + ww > reveal_w:
                    part = max(0, reveal_w - drawn_w)
                    if part < ww * 0.15:
                        break
                    im2 = im2.crop((0, 0, min(im2.size[0], part + pad),
                                    im2.size[1]))
                drawn_w += ww
            frame.alpha_composite(im2, (px, py))
            x += img.size[0] - 2 * pad + int(lm["size"] * 0.28)


def render_windows(plans, W, H, fps, duration, outdir, progress=None):
    """Write event-window frames + concat playlist. Returns playlist path."""
    os.makedirs(outdir, exist_ok=True)
    blank = os.path.join(outdir, "blank.png")
    Image.new("RGBA", (W, H), (0, 0, 0, 0)).save(blank)

    spans = sorted((pl["t0"], pl["t1"]) for pl in plans)
    windows = []
    for s, e in spans:
        if windows and s <= windows[-1][1] + 0.05:
            windows[-1][1] = max(windows[-1][1], e)
        else:
            windows.append([s, e])

    entries = []  # (path, duration)
    cursor = 0.0
    total_frames = sum(int((e - s) * fps) + 1 for s, e in windows) or 1
    done = 0
    for wi, (ws, we) in enumerate(windows):
        f0 = int(math.floor(ws * fps))
        f1 = int(math.ceil(we * fps))
        t_first = f0 / fps
        if t_first > cursor + 1e-6:
            entries.append((blank, t_first - cursor))
        for n in range(f0, f1 + 1):
            t = n / fps
            frame = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            for pl in plans:
                if pl["t0"] <= t < pl["t1"]:
                    _draw_plan(frame, pl, t)
            p = os.path.join(outdir, f"w{wi:03d}_{n:07d}.png")
            frame.save(p)
            entries.append((p, 1.0 / fps))
            done += 1
            if progress and done % 60 == 0:
                progress(done / total_frames)
        cursor = (f1 + 1) / fps
    if cursor < duration:
        entries.append((blank, duration - cursor + 1.0))

    playlist = os.path.join(outdir, "overlay.ffconcat")
    with open(playlist, "w") as f:
        f.write("ffconcat version 1.0\n")
        for path, dur in entries:
            f.write(f"file '{os.path.basename(path)}'\n")
            f.write(f"duration {max(0.001, dur):.5f}\n")
        f.write(f"file '{os.path.basename(entries[-1][0])}'\n")
    return playlist
