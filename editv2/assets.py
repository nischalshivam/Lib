"""External assets: show logos via Serper image search, verified by Gemini; cast
cutouts from the show's Cast folder (rembg). Everything cached per show."""
from __future__ import annotations

import glob, io, json, os, urllib.request
from PIL import Image, ImageFilter

from .common import CACHE, vision

SERPER_KEY_FILE = r"D:\Lib\secrets\serper.txt"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"}
LOGOS = os.path.join(CACHE, "logos")
CUTS = os.path.join(CACHE, "cutouts")
os.makedirs(LOGOS, exist_ok=True)
os.makedirs(CUTS, exist_ok=True)


def _search(q, n=10):
    key = open(SERPER_KEY_FILE, encoding="utf-8").read().strip()
    req = urllib.request.Request("https://google.serper.dev/images", json.dumps({"q": q, "num": n}).encode(),
                                 {"X-API-KEY": key, "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30)).get("images", [])


def _download(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as r:
            im = Image.open(io.BytesIO(r.read()))
            im.load()
            return im
    except Exception:
        return None


def _jpeg(im):
    b = io.BytesIO()
    bg = Image.new("RGB", im.size, (35, 35, 35))
    rgba = im.convert("RGBA")
    bg.paste(rgba, (0, 0), rgba)
    bg.save(b, "JPEG", quality=85)
    return b.getvalue()


def _real_alpha(im):
    if "A" not in im.mode:
        return False
    a = im.getchannel("A")
    lo, hi = a.getextrema()
    return lo < 16 and hi > 240 and sum(a.histogram()[:16]) / (im.width * im.height) > 0.15


def _key_border(im):
    import numpy as np
    a = np.asarray(im.convert("RGB")).astype(np.float32)
    border = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
    bg = np.median(border, axis=0)
    if np.std(border, axis=0).max() > 18:
        return None
    dist = np.sqrt(((a - bg) ** 2).sum(-1))
    alpha = np.clip((dist - 18) / 40, 0, 1) * 255
    return Image.fromarray(np.dstack([a, alpha]).astype(np.uint8), "RGBA")


def _sticker_if_dark(im):
    import numpy as np
    a = np.asarray(im).astype(np.float32)
    op = a[..., 3] > 128
    if not op.any():
        return im
    if (0.3 * a[..., 0] + 0.59 * a[..., 1] + 0.11 * a[..., 2])[op].mean() > 90:
        return im
    pad = max(8, im.width // 60)
    big = Image.new("RGBA", (im.width + 4 * pad, im.height + 4 * pad), (0, 0, 0, 0))
    big.paste(im, (2 * pad, 2 * pad), im)
    alpha = big.getchannel("A").filter(ImageFilter.MaxFilter(2 * pad + 1)).filter(ImageFilter.GaussianBlur(1.2))
    white = Image.new("RGBA", big.size, (255, 255, 255, 255))
    white.putalpha(alpha)
    white.alpha_composite(big)
    return white


def logo(key: str, show: str, log=print) -> str | None:
    """Verified, transparent, readable-on-dark title logo of `show` (cached)."""
    out = os.path.join(LOGOS, f"{key}.png")
    if os.path.exists(out):
        return out
    if not os.path.exists(SERPER_KEY_FILE):
        log("  logo: no Serper key file — skipped")
        return None
    good = []
    for c in [c for c in _search(f"{show} title logo png transparent") if (c.get("imageWidth") or 0) >= 600][:8]:
        im = _download(c["imageUrl"])
        if im is None:
            continue
        v = vision(f'Is this image the official title LOGO (wordmark) of "{show}"? Not fan art, not a poster with '
                   'actors, not another title, no extra taglines. Reply ONLY JSON {"match":true|false,'
                   '"watermark":true|false,"score":0-10}', _jpeg(im), tag="logo")
        if v and v.get("match") and not v.get("watermark"):
            good.append((_real_alpha(im), v.get("score", 0), im.width, im, c.get("domain")))
    good.sort(key=lambda g: (g[0], g[1], g[2]), reverse=True)
    for has_a, sc, _, im, dom in good:
        cut = im.convert("RGBA") if has_a else _key_border(im)
        if cut is None or cut.getchannel("A").getbbox() is None:
            continue
        cut = _sticker_if_dark(cut.crop(cut.getchannel("A").getbbox()))
        v = vision(f'This is the "{show}" logo on a DARK GREY background. Is it clearly readable against this dark '
                   'background, complete, with no leftover panel/box and no broken letters? Reply ONLY JSON '
                   '{"match":true|false,"score":0-10}', _jpeg(cut), tag="logo")
        if v and v.get("match") and v.get("score", 0) >= 7:
            cut.save(out)
            log(f"  logo for {show}: {dom}")
            return out
    log(f"  logo for {show}: none verified")
    return None


def cutout(key: str, cast_dir: str, name: str) -> str | None:
    """The cutout intro is decoration: any failure (a broken photo, rembg, a locked file) skips it - never the video."""
    try:
        return _cutout(key, cast_dir, name)
    except Exception as e:                                           # noqa: BLE001
        print(f"  cutout of {name} skipped: {type(e).__name__}: {e}", flush=True)
        return None


def _cutout(key: str, cast_dir: str, name: str) -> str | None:
    """Clean cutout of a character from its Cast photos (single face, biggest)."""
    import cv2, numpy as np
    from .common import FACE_MODEL
    out = os.path.join(CUTS, f"{key}__{name.replace(' ', '_')}.png")
    if os.path.exists(out):
        return out
    d = os.path.join(cast_dir, name)
    best = None
    for f in sorted([f for e in ("jpg", "jpeg", "png", "webp") for f in glob.glob(os.path.join(d, "*." + e))])[:10]:
        try:                                   # 2026-10-22 French Sitcoms 'Malcolm': one unreadable cast photo
            im = Image.open(f).convert("RGB")  # (OSError Errno 22) failed the whole video - skip it, use the others
            a = cv2.cvtColor(np.array(im), cv2.COLOR_RGB2BGR)
            det = cv2.FaceDetectorYN.create(FACE_MODEL, "", (a.shape[1], a.shape[0]), 0.8)
            _, faces = det.detect(a)
        except (OSError, ValueError, cv2.error):
            continue
        if faces is None or len(faces) != 1:
            continue
        sc = min(faces[0][3] / a.shape[0], 0.35) * 10 + min(im.width * im.height, 4e6) / 1e6
        if best is None or sc > best[0]:
            best = (sc, f)
    if not best:
        return None
    from rembg import remove, new_session
    im = Image.open(best[1]).convert("RGB")
    im.thumbnail((1600, 1600))
    cut = remove(im, session=new_session("isnet-general-use"))
    if cut.getbbox() is None:
        return None
    cut.crop(cut.getbbox()).save(out)
    return out
