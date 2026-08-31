"""Premium 'framed' look — the finished footage sits in a rounded card with a
soft shadow, on a textured background, exactly like the reference channel.

Deliberately applied as the LAST step, on the fully-rendered video. Every shot
has already been graded, pushed-in, zoomed, transitioned — all of that is baked
into the footage BEFORE it is placed in the card. So every animation, transition
and zoom keeps working; the frame is just a constant container the moving footage
plays inside. The background is one image per video (chosen from a folder), held
still, so the eye stays on the footage.
"""
from __future__ import annotations

import os
import subprocess
import tempfile

# card size as a fraction of a 1920x1080 frame (the reference leaves ~8% margin)
CARD_W, CARD_H = 1620, 911
CORNER = 40
SHADE = "vignette=a=PI/5"     # soft darkening at the edges = 'shade behind frame'
BG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".avif", ".bmp")


def list_backgrounds(folder: str) -> list:
    if not folder or not os.path.isdir(folder):
        return []
    return sorted(os.path.join(folder, f) for f in os.listdir(folder)
                  if f.lower().endswith(BG_EXT))


def pick_background(folder: str, key: str) -> str:
    """One background per video, chosen deterministically from the folder so the
    same video always gets the same image and different videos rotate through
    them. Drop new images in the folder any time — they join the rotation."""
    bgs = list_backgrounds(folder)
    if not bgs:
        return ""
    h = 0
    for ch in (key or "x"):
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF     # tiny stable string hash
    return bgs[h % len(bgs)]


def _assets(tmp: str, run) -> tuple:
    """Rounded-card alpha mask + a soft drop shadow, generated once per render."""
    from PIL import Image, ImageDraw, ImageFilter
    W, H = 1920, 1080
    x0, y0 = (W - CARD_W) // 2, (H - CARD_H) // 2
    mask = Image.new("L", (CARD_W, CARD_H), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, CARD_W - 1, CARD_H - 1],
                                           radius=CORNER, fill=255)
    mpath = os.path.join(tmp, "mask.png")
    mask.save(mpath)
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle(
        [x0, y0 + 14, x0 + CARD_W, y0 + CARD_H + 14], radius=CORNER,
        fill=(0, 0, 0, 140))
    sh = sh.filter(ImageFilter.GaussianBlur(34))
    spath = os.path.join(tmp, "shadow.png")
    sh.save(spath)
    return mpath, spath


def apply_frame(video_in: str, bg_path: str, out: str,
                run=subprocess.run, log=lambda *a: None) -> str:
    """Wrap the finished video in the rounded card on `bg_path`. Re-encodes once
    (the footage moves inside the card every frame, so a copy is impossible).
    Returns `video_in` unchanged if there is no background to use."""
    if not bg_path or not os.path.isfile(bg_path):
        log("  frame: koi background image nahi mili — skip")
        return video_in
    tmp = tempfile.mkdtemp(prefix="mi_frame_")
    mask, shadow = _assets(tmp, run)
    # Normalise the background to a 1920x1080 PNG first. AVIF/WEBP can't be fed
    # to the image demuxer's `-loop`, and this also bakes the scale/crop once.
    bg_png = os.path.join(tmp, "bg.png")
    run(["ffmpeg", "-y", "-v", "error", "-i", bg_path, "-frames:v", "1",
         "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,"
                "crop=1920:1080", bg_png], check=True)
    fc = (
        f"[0:v]{SHADE}[bg];"
        f"[1:v]scale={CARD_W}:{CARD_H},setsar=1[cl];"
        f"[cl][2:v]alphamerge[card];"
        f"[bg][3:v]overlay=0:0[bgs];"
        f"[bgs][card]overlay=(W-w)/2:(H-h)/2:format=auto[o]"
    )
    run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-i", bg_png,
         "-i", video_in, "-i", mask, "-loop", "1", "-i", shadow,
         "-filter_complex", fc, "-map", "[o]", "-map", "1:a?",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
         "-crf", "20", "-threads", "0", "-c:a", "copy", "-shortest",
         "-movflags", "+faststart", out], check=True)
    log(f"  frame: footage placed in the premium card on "
        f"{os.path.basename(bg_path)}")
    return out
