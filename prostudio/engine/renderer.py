"""ffmpeg renderer — shots + text events -> final 16:9 MP4 (4K or 1080p).

Human-feel details baked in:
  - camera drift: every static shot gets a subtle seeded sway (never
    mathematical-straight pans)
  - punch-in: occasional quick push mid-shot
  - J/L boundaries come pre-shifted from the planner
  - sentiment grade per shot (niche base + scene mood)
  - format decides transitions, grain, letterbox, glitch pulses, spotlight
"""
from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile

from . import FPS, RESOLUTIONS
from .audio_sync import duration
from .venc import video_codec as _vc, label as _vc_label
from .formats import FORMATS, grade_for, theme_color as _theme_color
from .textlayout import chunk_filters


def _run(cmd, log, timeout=None, cwd=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=cwd)
    except subprocess.TimeoutExpired:
        log(f"ffmpeg timed out after {timeout}s (stuck process, likely waiting "
            "on stdin) -> aborting this shot")
        raise RuntimeError("ffmpeg timed out")
    if p.returncode:
        log("ffmpeg error:\n" + p.stderr[-1500:])
        raise RuntimeError("ffmpeg failed")


_FC_FILE_FLAG = None


def _fc_file_flag():
    """Read a big filter graph from a FILE, on any ffmpeg. ffmpeg >= 7 removed
    `-filter_complex_script FILE`; its replacement is the generic
    `-/filter_complex FILE`. Probe once and use whichever this build accepts, so
    the long graph never has to go on the (length-limited) command line."""
    global _FC_FILE_FLAG
    if _FC_FILE_FLAG is None:
        _FC_FILE_FLAG = "-filter_complex_script"          # older ffmpeg default
        try:
            g = os.path.join(tempfile.gettempdir(), "_fc_probe.txt")
            with open(g, "w", encoding="utf-8") as f:
                f.write("color=c=black:s=32x32:d=1[v]")
            out = os.path.join(tempfile.gettempdir(), "_fc_probe.png")
            p = subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error",
                                "-/filter_complex", g, "-map", "[v]",
                                "-frames:v", "1", out],
                               capture_output=True, text=True, timeout=20)
            if p.returncode == 0 and os.path.isfile(out):
                _FC_FILE_FLAG = "-/filter_complex"         # ffmpeg >= 7
        except Exception:
            pass
    return _FC_FILE_FLAG


def _run_progress(cmd, log, total, work, lo=88, hi=99, stall_secs=900, cwd=None):
    """Run a long ffmpeg with LIVE progress so the final compose step never
    looks frozen. Maps encoded time -> [lo..hi]%. Aborts only if there is NO
    progress at all for `stall_secs` (a genuine hang), not just because it's
    slow."""
    import time
    errpath = os.path.join(work, "compose_err.log")
    with open(errpath, "w", encoding="utf-8") as errf:
        p = subprocess.Popen(cmd + ["-progress", "pipe:1", "-nostats"],
                             stdout=subprocess.PIPE, stderr=errf, text=True,
                             cwd=cwd)
        last_pct, last_beat = lo, time.time()
        try:
            for line in p.stdout:
                line = line.strip()
                if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                    try:
                        val = int(line.split("=", 1)[1])
                    except ValueError:
                        continue
                    # ffmpeg quirk: BOTH out_time_us and out_time_ms are in
                    # microseconds (out_time_ms is misnamed) -> divide by 1e6
                    secs = val / 1e6
                    frac = min(1.0, max(0.0, secs / max(0.1, total)))
                    pct = int(lo + (hi - lo) * frac)
                    now = time.time()
                    if pct > last_pct or now - last_beat > 20:
                        last_pct, last_beat = max(pct, last_pct), now
                        log(f"[{last_pct:3d}%] compositing ... "
                            f"{secs:.0f}/{total:.0f}s encoded")
                elif line.startswith("progress=end"):
                    break
        finally:
            p.stdout.close()
        p.wait()
    if p.returncode:
        try:
            err = open(errpath, encoding="utf-8", errors="replace").read()
        except OSError:
            err = ""
        # keep the FULL ffmpeg error next to the output for diagnosis, and show
        # the actual error lines (not just a blind tail that may hide the cause)
        keep = os.path.splitext(job_out(cmd))[0] + "_ffmpeg_error.log"
        try:
            with open(keep, "w", encoding="utf-8") as f:
                f.write(err)
        except OSError:
            keep = errpath
        hot = [ln for ln in err.splitlines()
               if any(k in ln for k in ("Error", "error", "Invalid",
                                        "No such", "Cannot", "failed"))]
        msg = "\n".join(hot[-8:]) if hot else err[-800:]
        log("ffmpeg error (full log: " + keep + "):\n" + msg)
        raise RuntimeError("ffmpeg failed")


def job_out(cmd):
    """The output path is the last token of an ffmpeg command."""
    return cmd[-1]


def _dims(path):
    """(width, height) of the first video stream, or None."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path],
            capture_output=True, text=True, timeout=30).stdout.strip()
        w, h = out.split("x")[:2]
        return int(w), int(h)
    except Exception:
        return None


def _wants_blurfill(shot, W, H):
    """Use a blurred-fill background when the SOURCE is not ~16:9 (portrait,
    4:3, square — common for cartoons/anime/old footage). Cropping those to
    fill loses content; black bars look cheap. Blur-fill is the premium fix."""
    d = _dims(shot.path)
    if not d or d[1] == 0:
        return False
    src = d[0] / d[1]
    return src < 1.55 or src > 2.15          # narrower than 14:9 or ultrawide


def _render_inset(shot, out, style, niche, W, H, secs, log, inset=0.90,
                  border_px=0, border_color="0x000000", timeout=180):
    """Sharp, gently-floating foreground over a blurred, darkened fill of the
    same frame. One primitive, three looks:
      - blurfill : inset ~0.90, no border  (non-16:9 footage, no crop/bars)
      - card     : inset ~0.85, thin dark edge  (cinematic floating card)
      - border   : inset ~0.82, thick themed edge  (cartoon/anime frame)
    """
    grade = grade_for(niche, shot.mood, style["sepia"])
    fw = max(2, int(W * inset) // 2 * 2)
    fh = max(2, int(H * inset) // 2 * 2)
    frames = max(1, int(secs * FPS))
    if shot.kind == "image":
        ins = ["-loop", "1", "-t", f"{secs + 0.4:.3f}", "-i", shot.path]
    else:
        ss = max(0.0, getattr(shot, "src_in", 0.0) or 0.0)
        ins = (["-ss", f"{ss:.3f}"] if ss > 0 else []) + \
            ["-t", f"{secs + 0.4:.3f}", "-i", shot.path]
    post = ""
    if style["grain"]:
        post += f",noise=alls={style['grain']}:allf=t+u"
    if style["vignette"]:
        post += ",vignette=PI/5"
    borderf = ""
    if border_px > 0:
        borderf = (f",pad=iw+{2*border_px}:ih+{2*border_px}:"
                   f"{border_px}:{border_px}:color={border_color}")
    # the FOREGROUND frame stays perfectly still (overlay at a fixed centre);
    # for a still, life comes from a slow zoom on the BLURRED BACKGROUND only,
    # so the framed content never slides around the screen.
    bg_move = (f",zoompan=z='min(1.0+0.0004*on,1.06)':d={frames}"
               f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
               f":s={W}x{H}:fps={FPS}") if shot.kind == "image" else ""
    fc = (
        f"[0:v]split=2[a][b];"
        f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
        f"boxblur=26:1,setsar=1{bg_move}[bg];"
        f"[b]scale={fw}:{fh}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"setsar=1,{grade}{borderf}[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2:format=auto{post}[v]"
    )
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", *ins,
           "-filter_complex", fc, "-map", "[v]", "-t", f"{secs:.3f}",
           "-an", "-r", str(FPS), *_vc(preset="veryfast"), "-pix_fmt", "yuv420p",
           out]
    _run(cmd, log, timeout=timeout)
    _ensure_duration(out, secs, log)


_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".avif", ".bmp", ".gif")
_VID_EXT = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")


import threading as _threading
_ASSET_LOCK = _threading.Lock()


def _moving_card_assets(work, cw, ch, rad):
    """Rounded-card alpha mask (cw×ch) + a soft drop-shadow PNG on a slightly
    larger transparent canvas, both cached by size. The shadow gives the card a
    'floating over a shade' look."""
    from PIL import Image, ImageDraw, ImageFilter
    m = os.path.join(work, f"mcmask_{cw}x{ch}.png")
    s = os.path.join(work, f"mcshadow_{cw}x{ch}.png")
    # shots render in PARALLEL: build each asset once, under a lock, and write it
    # to a temp name first so no thread can read a half-written PNG.
    with _ASSET_LOCK:
        if not os.path.isfile(m):
            mk = Image.new("L", (cw, ch), 0)
            ImageDraw.Draw(mk).rounded_rectangle([0, 0, cw - 1, ch - 1],
                                                 radius=rad, fill=255)
            mk.save(m + ".tmp.png")
            os.replace(m + ".tmp.png", m)
        if not os.path.isfile(s):
            P = 60
            sh = Image.new("RGBA", (cw + 2 * P, ch + 2 * P), (0, 0, 0, 0))
            ImageDraw.Draw(sh).rounded_rectangle([P, P + 10, cw + P, ch + P + 10],
                                                 radius=rad, fill=(0, 0, 0, 150))
            sh.filter(ImageFilter.GaussianBlur(28)).save(s + ".tmp.png")
            os.replace(s + ".tmp.png", s)
    return m, s


def _render_moving_card(shot, out, style, niche, W, H, secs, bg_path, log,
                        timeout=200):
    """The competitor look: the footage rides in a ROUNDED CARD whose SIZE and
    ENTRANCE change shot to shot — it slides in from a different edge (left /
    right / top / bottom), or pops in place, at a scale from ~0.6 to ~0.92 — over
    a background that can be a still image OR a moving video. Per-shot variety is
    seeded from the shot so it is stable but never repetitive."""
    import random as _r
    rng = _r.Random(getattr(shot, "drift_seed", 0) or 1)
    grade = grade_for(niche, shot.mood, style["sepia"])
    # card size (even numbers) — varied per shot, but never tiny (min ~0.74)
    scale = rng.choice([0.74, 0.78, 0.82, 0.86, 0.90, 0.94])
    cw = max(2, int(W * scale) // 2 * 2)
    ch = max(2, int(H * scale) // 2 * 2)
    # resting position: usually centred, sometimes pushed toward a third
    slack_x, slack_y = (W - cw), (H - ch)
    rx = slack_x // 2 + int(rng.choice([-0.35, 0, 0, 0.35]) * slack_x / 2)
    ry = slack_y // 2 + int(rng.choice([-0.30, 0, 0, 0.30]) * slack_y / 2)
    rx = max(0, min(slack_x, rx)); ry = max(0, min(slack_y, ry))
    ENT = 0.5                                        # entrance length (s)
    frm = rng.choice(["left", "right", "top", "bottom", "left", "right",
                      "pop", "static"])
    ease = f"(1-pow(1-min(1,t/{ENT}),3))"            # ease-out cubic 0->1
    if frm == "left":
        xexpr, yexpr = f"({rx}+({-cw-rx})*(1-{ease}))", f"{ry}"
    elif frm == "right":
        xexpr, yexpr = f"({rx}+({W}-{rx})*(1-{ease}))", f"{ry}"
    elif frm == "top":
        xexpr, yexpr = f"{rx}", f"({ry}+({-ch-ry})*(1-{ease}))"
    elif frm == "bottom":
        xexpr, yexpr = f"{rx}", f"({ry}+({H}-{ry})*(1-{ease}))"
    else:                                            # pop / static: hold in place
        xexpr, yexpr = f"{rx}", f"{ry}"
    frames = max(1, int(secs * FPS))
    # background source: image (loop) or video (loop)
    bg_is_vid = bg_path and bg_path.lower().endswith(_VID_EXT)
    if bg_is_vid:
        bg_in = ["-stream_loop", "-1", "-t", f"{secs:.3f}", "-i", bg_path]
    else:
        bg_in = ["-loop", "1", "-framerate", str(FPS),
                 "-t", f"{secs + 0.3:.3f}", "-i", bg_path]
    # foreground shot: image (loop) or video (seek)
    if shot.kind == "image":
        fg_in = ["-loop", "1", "-framerate", str(FPS),
                 "-t", f"{secs + 0.3:.3f}", "-i", shot.path]
    else:
        ss = max(0.0, getattr(shot, "src_in", 0.0) or 0.0)
        fg_in = (["-ss", f"{ss:.3f}"] if ss > 0 else []) + \
            ["-t", f"{secs + 0.3:.3f}", "-i", shot.path]
    rad = max(18, int(0.02 * W))
    mask, shadow = _moving_card_assets(os.path.dirname(out), cw, ch, rad)
    # border look, varied per shot: a crisp WHITE edge, a soft BLACK edge, or no
    # border (the card just floats on its shade). Matches the reference channels.
    bstyle = rng.choice(["white", "black", "none", "none", "white"])
    if bstyle == "white":
        bw, bcol = max(4, int(0.006 * W)), "white"
    elif bstyle == "black":
        bw, bcol = max(4, int(0.006 * W)), "0x14181f"
    else:
        bw, bcol = 0, "black"
    iw, ih = cw - 2 * bw, ch - 2 * bw
    post = ""
    if style.get("grain"):
        post += f",noise=alls={style['grain']}:allf=t+u"
    if style.get("vignette"):
        post += ",vignette=PI/5"
    # a gentle push/pull INSIDE the card so it is never dead-still — direction
    # (zoom-in vs zoom-out) varies per shot for more life. Stills are built up
    # from one frame (d=frames); a video advances one output frame per input
    # frame (d=1) so the zoom rides along its own motion.
    zdir = rng.choice(["in", "out", "in", "out", "none"])
    if shot.kind == "image":
        # subpixel-smooth Ken Burns on the card image (no zoompan shake)
        mv = {"in": "in", "out": "out", "none": "hold"}[zdir]
        card_src = _kb_perspective(iw, ih, frames, mv)
    else:
        # a video already carries its own motion: a light zoompan (d=1) rides
        # along it and the truncation is masked by that motion, so keep it.
        base = (f"scale={iw}:{ih}:force_original_aspect_ratio=increase,"
                f"crop={iw}:{ih}")
        if zdir == "none":
            card_src = base + ",setsar=1"
        else:
            zexpr = ("min(1.0+0.0006*on,1.12)" if zdir == "in"
                     else "max(1.12-0.0006*on,1.0)")
            card_src = (base + f",zoompan=z='{zexpr}':d=1:x='iw/2-(iw/zoom/2)':"
                        f"y='ih/2-(ih/zoom/2)':s={iw}x{ih}:fps={FPS},setsar=1")
    pad = (f",pad={cw}:{ch}:{bw}:{bw}:color={bcol}" if bw > 0 else "")
    # shadow rides just behind the card (same slide, small down-right offset)
    P = 60
    sxexpr, syexpr = f"({xexpr})-{P}+8", f"({yexpr})-{P}+10"
    fc = (
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
        f"boxblur=22:1,eq=brightness=-0.06:saturation=0.9,setsar=1[bg];"
        f"[1:v]{card_src},{grade}{pad}[fgc];"
        f"[fgc][2:v]alphamerge[card];"
        f"[bg][3:v]overlay=x='{sxexpr}':y='{syexpr}':format=auto[bgs];"
        f"[bgs][card]overlay=x='{xexpr}':y='{yexpr}':format=auto{post}[v]"
    )
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error",
           *bg_in, *fg_in, "-loop", "1", "-i", mask, "-loop", "1", "-i", shadow,
           "-filter_complex", fc, "-map", "[v]", "-t", f"{secs:.3f}",
           "-an", "-r", str(FPS), *_vc(preset="veryfast"), "-pix_fmt", "yuv420p",
           out]
    _run(cmd, log, timeout=timeout)
    _ensure_duration(out, secs, log)


def _render_blurfill(shot, out, style, niche, W, H, secs, log, timeout=180):
    _render_inset(shot, out, style, niche, W, H, secs, log, inset=0.90,
                  timeout=timeout)


def _render_framed(shot, out, style, niche, W, H, secs, log, mode, log_theme,
                   timeout=180):
    if mode == "border":
        _render_inset(shot, out, style, niche, W, H, secs, log, inset=0.82,
                      border_px=max(6, int(0.012 * W)), border_color=log_theme,
                      timeout=timeout)
    else:                                         # card
        _render_inset(shot, out, style, niche, W, H, secs, log, inset=0.85,
                      border_px=max(2, int(0.0025 * W)),
                      border_color="0x0d1014", timeout=timeout)


def _glow_png(path, size=1000):
    from PIL import Image
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = img.load()
    c = size // 2
    for y in range(size):
        for x in range(size):
            d = math.hypot(x - c, y - c) / c
            if d < 1:
                px[x, y] = (255, 238, 190, int(160 * (1 - d) ** 2.3))
    img.save(path)


def _kb_perspective(ow, oh, frames, move, zoom_per_sec=0.010):
    """Subpixel-smooth Ken Burns for a STILL image — the shake-free replacement
    for zoompan. zoompan keeps its crop origin on a WHOLE pixel and truncates the
    fractional x/y every frame; at ~3.6 px/frame of intended motion that rounding
    stumbles ~6 times a second and reads as a fine shake (worst on the vertical
    axis of a zoom). Instead: supersample to a 16-aligned canvas, then let the
    `perspective` filter (cubic, FLOAT source corners) pull a slowly shrinking /
    sliding rectangle — no truncation — and downscale with lanczos.
    `move`: in|out|hold|panl|panr|panu|pand. Corners evaluate per output frame
    index `on`. Feed the image with `-framerate FPS` or frames get duplicated.
    (Method + measurements: KEN-BURNS-FIX.md, sd 0.446 px -> 0.000 px.)"""
    dur = frames / float(FPS)
    z = min(0.20, max(0.012, zoom_per_sec * dur))       # total zoom over the shot
    CW = int(round(ow * (1 + z) / 16) * 16)             # canvas: 16-aligned so CH
    CH = int(round(CW * oh / ow))                       # lands cleanly on 16:9
    N1 = max(1, frames - 1)
    f = lambda v: f"{v:.4f}"
    if move in ("panl", "panr", "panu", "pand"):        # fixed-size rect, slide it
        if move in ("panl", "panr"):
            sx = (CW - ow) / N1
            ty = (CH - oh) / 2.0
            p = f"({N1}-on)" if move == "panl" else "on"
            L, R = f"{f(sx)}*{p}", f"{f(sx)}*{p}+{ow}"
            T, B = f"{f(ty)}", f"{f(ty + oh)}"
        else:
            sy = (CH - oh) / N1
            tx = (CW - ow) / 2.0
            p = f"({N1}-on)" if move == "panu" else "on"
            T, B = f"{f(sy)}*{p}", f"{f(sy)}*{p}+{oh}"
            L, R = f"{f(tx)}", f"{f(tx + ow)}"
    else:                                               # zoom in / out / hold
        ax = (CW - ow) / 2.0 / N1
        ay = (CH - oh) / 2.0 / N1
        p = {"out": f"({N1}-on)", "hold": "0"}.get(move, "on")
        L, T = f"{f(ax)}*{p}", f"{f(ay)}*{p}"
        R, B = f"{CW}-{f(ax)}*{p}", f"{CH}-{f(ay)}*{p}"
    persp = (f"perspective=eval=frame:sense=source:interpolation=cubic:"
             f"x0='{L}':y0='{T}':x1='{R}':y1='{T}':"
             f"x2='{L}':y2='{B}':x3='{R}':y3='{B}'")
    # Stills are single frames of (often upscaled-SD, compressed) episodes:
    # a light denoise + unsharp BEFORE the zoom takes the block noise off and
    # restores edges. PS_IMG_ENHANCE=0 turns it off.
    enh = ("hqdn3d=1.5:1.2:0:0,unsharp=5:5:0.7:5:5:0,"
           if os.environ.get("PS_IMG_ENHANCE", "1") != "0" else "")
    return (f"{enh}scale={CW}:{CH}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={CW}:{CH},{persp},scale={ow}:{oh}:flags=lanczos,setsar=1")


def _zoompan_image(shot, W, H, secs, style):
    frames = max(1, int(secs * FPS))
    move = getattr(shot, "move", "") or ("in" if shot.zoom_in else "out")
    if style.get("pan") == "lr":                 # F8 keeps its horizontal pan
        move = "panr" if shot.drift_seed % 2 == 0 else "panl"
    zps = 0.020 if style.get("strong_push") else 0.010
    return _kb_perspective(W, H, frames, move, zoom_per_sec=zps)


def _video_vf(shot, W, H, style):
    fit = (f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
           f"crop={W}:{H},setsar=1,fps={FPS}")
    sk = style["shake"]
    if sk > 0:
        mx = int(70 * sk * (W / 1920)) + 8
        my = int(40 * sk * (H / 1080)) + 6
        return (f"scale={W + 2*mx}:{H + 2*my}:force_original_aspect_ratio=increase"
                f":flags=lanczos,crop={W + 2*mx}:{H + 2*my},"
                f"crop={W}:{H}:x='{mx}+{mx*0.4:.0f}*sin(t*15)+{mx*0.25:.0f}*sin(t*31)'"
                f":y='{my}+{my*0.45:.0f}*cos(t*19)',setsar=1,fps={FPS}")
    if style.get("pushin") or style.get("strong_push"):
        zmax = 1.13 if not style.get("strong_push") else 1.22
        return fit + (f",zoompan=z='min(pzoom+0.0011,{zmax})':d=1"
                      f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                      f":s={W}x{H}:fps={FPS}")
    return fit


def _ensure_duration(out, secs, log):
    """Pad a short segment up to `secs` so the composite offsets stay exact."""
    got = duration(out)
    if got + 0.05 < secs:
        tmp = out + ".p.mp4"
        _run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", out, "-vf",
              f"tpad=stop_mode=clone:stop_duration={secs - got:.3f}",
              *_vc(preset="veryfast"), "-pix_fmt", "yuv420p",
              tmp], log, timeout=120)
        os.replace(tmp, out)


def _render_full(shot, out, style, niche, W, H, secs, glow, log, timeout):
    """The full styled render (Ken Burns / drift / grade / grain / glitch)."""
    if shot.kind == "image":
        vf = _zoompan_image(shot, W, H, secs, style)
        ins = ["-loop", "1", "-framerate", str(FPS),
               "-t", f"{secs + 0.4:.3f}", "-i", shot.path]
    else:
        vf = _video_vf(shot, W, H, style)
        ss = max(0.0, getattr(shot, "src_in", 0.0) or 0.0)   # user in-point
        ins = (["-ss", f"{ss:.3f}"] if ss > 0 else []) + \
            ["-t", f"{secs + 0.4:.3f}", "-i", shot.path]
    vf += "," + grade_for(niche, shot.mood, style["sepia"])
    if style["grain"]:
        vf += f",noise=alls={style['grain']}:allf=t+u"
    if style["glitch"]:
        s4 = max(2, int(4 * W / 1920))
        vf += (f",rgbashift=rh={s4}:bv=-{s4}"
               f":enable='lt(mod(t\\,2.7)\\,0.13)'")
    if style["vignette"]:
        vf += ",vignette=PI/4.6"
    if style.get("spotlight"):
        vf += ",eq=brightness=-0.16:saturation=0.85"
    if style["letterbox"]:
        vh = int(W * 9 / 21)
        vf += f",crop={W}:{vh},pad={W}:{H}:0:(oh-ih)/2:black"

    if style.get("spotlight"):
        gs = int(min(W, H) * 1.15)
        fc = (f"[0:v]{vf}[b];[1:v]scale={gs}:{gs}[g];"
              f"[b][g]overlay=x=(W-w)/2:y=(H-h)/2-{int(0.06*H)}:format=auto[v]")
        cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", *ins,
               "-loop", "1", "-t", f"{secs + 0.4:.3f}", "-i", glow,
               "-filter_complex", fc, "-map", "[v]"]
    else:
        cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", *ins, "-vf", vf]
    cmd += ["-t", f"{secs:.3f}", "-an", "-r", str(FPS), *_vc(preset="veryfast"),
            "-pix_fmt", "yuv420p", out]
    _run(cmd, log, timeout=timeout)
    _ensure_duration(out, secs, log)


def _safe_still(path, kind, W, H, work, log, timeout=60, src_in=0.0):
    """Decode ONE frame (single pass, bounded) and normalize it to WxH.

    This is the escape hatch for a pathological input (huge / corrupt /
    exotic-codec video): one decode instead of per-frame heavy filtering.
    Returns the still path or None."""
    still = os.path.join(work, "safe_" + str(abs(hash((path, _threading.get_ident())))
                                             % 10**8) + ".png")
    seek = []
    if kind == "video":
        d = duration(path)
        if src_in > 0:
            seek = ["-ss", f"{src_in:.2f}"]        # honor the user's in-point
        elif d > 0.2:
            seek = ["-ss", f"{min(max(d * 0.5, 0.0), max(0.0, d - 0.1)):.2f}"]
    cmd = (["ffmpeg", "-nostdin", "-y", "-v", "error", *seek, "-i", path,
            "-frames:v", "1", "-vf",
            f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=bilinear,"
            f"crop={W}:{H}", still])
    try:
        _run(cmd, log, timeout=timeout)
    except Exception:
        return None
    return still if os.path.isfile(still) else None


def _render_still_simple(still, out, shot, W, H, secs, style, niche, log,
                         timeout=90):
    """A gentle, robust render from a NORMALIZED still (input already WxH,
    so this is always fast). Slow zoom + grade only — no grain/glitch."""
    frames = max(1, int(secs * FPS))
    move = "in" if shot.zoom_in else "out"
    vf = (_kb_perspective(W, H, frames, move) + ","
          + grade_for(niche, shot.mood, style["sepia"]))
    if style["letterbox"]:
        vh = int(W * 9 / 21)
        vf += f",crop={W}:{vh},pad={W}:{H}:0:(oh-ih)/2:black"
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-loop", "1",
           "-framerate", str(FPS),
           "-t", f"{secs + 0.4:.3f}", "-i", still, "-vf", vf,
           "-t", f"{secs:.3f}", "-an", "-r", str(FPS), *_vc(preset="veryfast"),
           "-pix_fmt", "yuv420p", out]
    _run(cmd, log, timeout=timeout)
    _ensure_duration(out, secs, log)


def _render_filler(out, W, H, secs, log):
    """Last resort: a neutral dark clip of the exact duration, so the
    timeline never breaks even if a file is completely unusable."""
    _run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi",
          "-i", f"color=c=0x0b0d10:s={W}x{H}:r={FPS}:d={secs:.3f}",
          "-t", f"{secs:.3f}", *_vc(preset="veryfast"), "-pix_fmt", "yuv420p",
          out], log, timeout=60)


def _render_bare(shot, out, W, H, secs, log, timeout=120):
    """No-Filter render: the clip/still scaled to fill the frame and NOTHING
    else — no zoom, grade, grain, frame, vignette or text. It exists for long
    (1-4 hr) videos where the point is the footage under the narration, not the
    editing, and where the full effects pass would take many hours."""
    fit = (f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=bilinear,"
           f"crop={W}:{H},setsar=1,fps={FPS}")
    if shot.kind == "image":
        ins = ["-loop", "1", "-t", f"{secs:.3f}", "-i", shot.path]
    else:
        ss = max(0.0, getattr(shot, "src_in", 0.0) or 0.0)
        ins = (["-ss", f"{ss:.3f}"] if ss > 0 else []) + \
            ["-t", f"{secs + 0.4:.3f}", "-i", shot.path]
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", *ins, "-vf", fit,
           "-t", f"{secs:.3f}", "-an", "-r", str(FPS), *_vc(preset="veryfast"),
           "-pix_fmt", "yuv420p", out]
    _run(cmd, log, timeout=timeout)
    _ensure_duration(out, secs, log)


def render_shot(shot, out, style, niche, W, H, pad, glow, log, work=None,
                bg=None):
    """Render one shot, GUARANTEED to produce a valid segment.

    Tier 1: full styled render (bounded timeout).
    Tier 2: on timeout/failure, a single-decode safe still (gentle motion).
    Tier 3: on any further failure, neutral filler of the right length.
    A single bad/huge/corrupt file can never stall or fail the whole job."""
    secs = shot.secs + pad
    work = work or os.path.dirname(out)
    # Moving-card formats: the footage rides a varied-size card that slides in
    # from an edge over the (image OR video) background. Its own compositing path.
    if bg and getattr(shot, "framing", "") == "movingcard":
        try:
            _render_moving_card(shot, out, style, niche, W, H, secs, bg, log)
            return "full"
        except Exception as exc:
            log(f"  moving-card slow/failed ({exc}); safe mode ...")
            # fall through to the safe tiers
    if style.get("bare"):                      # No-Filter format: clip only
        try:
            _render_bare(shot, out, W, H, secs, log)
            return "full"
        except Exception as exc:
            log(f"  bare shot slow/failed ({exc}); safe mode ...")
            # fall through to the safe-still tiers below
    # framing: explicit shot.framing wins; otherwise auto (blurfill for non-16:9
    # so nothing is cropped / no black bars, else full-bleed). Spotlight and
    # letterbox formats keep their own compositing path when framing is auto.
    framing = getattr(shot, "framing", "")
    if not framing:
        framing = ("blurfill" if (not style.get("spotlight")
                                  and not style.get("letterbox")
                                  and _wants_blurfill(shot, W, H)) else "full")
    try:
        if framing == "card":
            _render_framed(shot, out, style, niche, W, H, secs, log, "card",
                           _theme_color(niche), timeout=180)
        elif framing == "border":
            _render_framed(shot, out, style, niche, W, H, secs, log, "border",
                           _theme_color(niche), timeout=180)
        elif framing == "blurfill":
            _render_blurfill(shot, out, style, niche, W, H, secs, log,
                             timeout=180)
        elif framing == "letterbox":
            st = dict(style)
            st["letterbox"] = True
            st["spotlight"] = False
            _render_full(shot, out, st, niche, W, H, secs, glow, log,
                         timeout=180)
        else:
            _render_full(shot, out, style, niche, W, H, secs, glow, log,
                         timeout=180)
        return "full"
    except Exception as exc:
        log(f"  shot slow/failed ({exc}); retrying in safe mode "
            f"[{os.path.basename(shot.path)}] ...")
    try:
        still = _safe_still(shot.path, shot.kind, W, H, work, log,
                            src_in=max(0.0, getattr(shot, "src_in", 0.0) or 0.0))
        if still:
            _render_still_simple(still, out, shot, W, H, secs, style, niche, log)
            return "safe"
    except Exception as exc:
        log(f"  safe mode failed ({exc}); using neutral filler ...")
    _render_filler(out, W, H, secs, log)
    return "filler"


def _frame_assets(work, W, H, cw, ch, rad=40):
    """Rounded-card alpha mask + soft drop shadow (PIL), cached in `work`."""
    from PIL import Image, ImageDraw, ImageFilter
    x0, y0 = (W - cw) // 2, (H - ch) // 2
    m = os.path.join(work, "frame_mask.png")
    s = os.path.join(work, "frame_shadow.png")
    mask = Image.new("L", (cw, ch), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, cw - 1, ch - 1], radius=rad,
                                           fill=255)
    mask.save(m)
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle([x0, y0 + 14, x0 + cw, y0 + ch + 14],
                                         radius=rad, fill=(0, 0, 0, 140))
    sh.filter(ImageFilter.GaussianBlur(34)).save(s)
    return m, s


def _compose_chain(clips, nets, joins, out, W, H, crf, preset, work, log,
                   job=None, text_events=None, audio=None, total=None,
                   tag="c", progress=False, frame_bg=None):
    """Cross-fade a list of clips into one, with optional text + audio.

    Kept SMALL on purpose: it is called on a bounded number of inputs at a
    time (see render_job's chunking), so ffmpeg never has to open ~150 4K
    decoders at once (that caused 'Cannot allocate memory' and truncated
    videos). `nets[i]` is how much clip i advances the timeline; `joins` are
    the (type, dur) transitions between clips."""
    n = len(clips)
    inputs = []
    for c in clips:
        inputs += ["-i", c]
    durs = [duration(c) for c in clips]
    filt, prev, acc = [], "0:v", 0.0
    for i in range(1, n):
        ttype, tdur = joins[i - 1]
        acc += nets[i - 1]
        filt.append(f"[{prev}][{i}:v]xfade=transition={ttype}"
                    f":duration={tdur:.3f}:offset={acc:.3f}[x{i}]")
        prev = f"x{i}"
    comp_total = (acc + durs[-1]) if n > 1 else durs[0]
    if total is None:
        total = comp_total
    if text_events and job is not None:
        style = FORMATS[job.format_key]
        tfilters = []
        for (t0, t1, si, chunk, zone) in text_events:
            tfilters += chunk_filters(chunk, t0, t1, style, zone, W, H,
                                      lang=job.language,
                                      letterbox=style["letterbox"])
        if tfilters:
            batch, label = 8, prev
            for i in range(0, len(tfilters), batch):
                grp = tfilters[i:i + batch]
                ol = "vt" if i + batch >= len(tfilters) else f"vt{i}"
                filt.append(f"[{label}]" + ",".join(grp) + f"[{ol}]")
                label = ol
            prev = "vt"
    vmap = f"[{prev}]" if prev != "0:v" else "0:v"
    maps, acodec = ["-map", vmap], ["-an"]
    if audio:
        filt.append(f"[{n}:a]atrim=0:{total:.3f},afade=t=in:d=0.25,"
                    f"afade=t=out:st={max(0, total - 1.2):.3f}:d=1.2[a]")
        inputs += ["-i", audio]
        maps += ["-map", "[a]"]
        acodec = ["-c:a", "aac", "-b:a", "160k"]

    # Premium frame — done HERE, in the single final encode (no separate ~20-min
    # re-encode pass). The composed footage (with every grade/zoom/transition/
    # text already baked in) is placed in a rounded card on the background, so
    # the frame is a constant container and everything keeps working inside it.
    loop_inputs = []
    if frame_bg and os.path.isfile(frame_bg):
        cw, ch = int(W * 0.844), int(H * 0.844)
        mask, shadow = _frame_assets(work, W, H, cw, ch)
        # normalise the bg to PNG first — AVIF/WEBP can't be fed to -loop, and
        # this bakes the scale/crop once
        bg_png = os.path.join(work, "frame_bg.png")
        _run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", frame_bg,
              "-frames:v", "1", "-vf",
              f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}",
              bg_png], log)
        base = vmap if vmap.startswith("[") else f"[{vmap}]"
        nin = len(inputs) // 2
        bgi, mi, si = nin, nin + 1, nin + 2
        loop_inputs = ["-loop", "1", "-i", bg_png,
                       "-loop", "1", "-i", mask, "-loop", "1", "-i", shadow]
        filt.append(
            f"[{bgi}:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},vignette=a=PI/5[fbg];"
            f"{base}scale={cw}:{ch},setsar=1[fcl];[fcl][{mi}:v]alphamerge[fcard];"
            f"[fbg][{si}:v]overlay=0:0[fbgs];"
            f"[fbgs][fcard]overlay=(W-w)/2:(H-h)/2:format=auto[vframed]")
        maps[maps.index(vmap)] = "[vframed]"

    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    log(f"  video encoder: {_vc_label()}")
    if filt:
        graph_file = os.path.join(work, f"graph_{tag}.txt")
        with open(graph_file, "w", encoding="utf-8") as f:
            f.write(";\n".join(filt))
        cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", *inputs, *loop_inputs,
               _fc_file_flag(), graph_file, *maps,
               *_vc(crf=crf, preset=preset),
               "-pix_fmt", "yuv420p", "-r", str(FPS), *acodec,
               "-movflags", "+faststart", "-t", f"{total:.3f}", out]
    else:                                   # single clip, no filters -> copy
        cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", clips[0],
               "-c", "copy", "-t", f"{total:.3f}", out]
    # drawtext references fonts by BARE filename (a Windows drive colon breaks the
    # filtergraph), so the compose must run from the fonts directory.
    fdir = None
    if text_events and job is not None:
        from .textlayout import FONTS_DIR
        fdir = FONTS_DIR
    if progress:
        _run_progress(cmd, log, total=total, work=work, cwd=fdir)
    else:
        _run(cmd, log, timeout=None, cwd=fdir)
    return out, comp_total


def _concat_copy(segs, out, audio, log):
    """Join equal-format segments with hard cuts and NO video re-encode (concat
    demuxer + stream copy), then lay the narration over them. This is what makes
    the No-Filter format finish a multi-hour video in seconds of muxing instead
    of hours of compositing. Falls back to a single re-encode only if copy fails
    (mismatched segment params)."""
    lst = out + ".concat.txt"
    with open(lst, "w", encoding="utf-8") as f:
        for s in segs:
            f.write("file '%s'\n" % os.path.abspath(s).replace("'", "'\\''"))
    base = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "concat",
            "-safe", "0", "-i", lst]
    amap = (["-i", audio, "-map", "0:v:0", "-map", "1:a:0",
             "-c:a", "aac", "-b:a", "160k", "-shortest"]
            if audio and os.path.isfile(audio) else [])
    try:
        _run(base + amap + ["-c:v", "copy", "-movflags", "+faststart", out],
             log, timeout=None)
        if os.path.isfile(out) and duration(out) > 1.0:
            return
    except Exception as exc:
        log(f"  stream-copy join failed ({exc}); re-encoding once ...")
    # fallback: one clean re-encode (still a single pass, no effects)
    _run(base + amap + [*_vc(crf=20, preset="veryfast"),
                        "-pix_fmt", "yuv420p", "-movflags", "+faststart", out],
         log, timeout=None)


def _group_size(W, H):
    """How many clips to cross-fade at once. Bounded so ffmpeg never opens too
    many decoders — scaled down for higher resolutions (4K is memory-heavy)."""
    import os as _os
    env = _os.environ.get("PS_GROUP")
    if env and env.isdigit():
        return max(2, int(env))
    return max(6, int(48 * (1920 * 1080) / max(1, W * H)))   # 4K->12, 1080p->48


def _neutralize(style):
    """Strip the look-dulling / darkening effects so every format shows the
    footage as the source actually looks. Motion (Ken Burns / drift / push /
    pan), the frame-inset system, transitions and text stay — only the colour
    grade, vignette, grain, glitch, sepia and spotlight-darkening are removed.
    Opt back in to the old cinematic treatments per render with PS_GRADE=1."""
    if os.environ.get("PS_GRADE") == "1":
        return style
    style.update(grain=0, vignette=False, glitch=False, sepia=False,
                 spotlight=False)
    return style


_BGM_DIR = os.environ.get("PS_BGM_DIR", r"C:\Users\Dell\Desktop\ProStudio\BGM")
_BGM_EXT = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma")


def _bgm_tracks():
    if not os.path.isdir(_BGM_DIR):
        return []
    return sorted(os.path.join(_BGM_DIR, f) for f in os.listdir(_BGM_DIR)
                  if f.lower().endswith(_BGM_EXT)
                  and os.path.isfile(os.path.join(_BGM_DIR, f)))


def mix_bgm(vo_path, work, log, seed=0):
    """Return an audio track = narration with a background-music bed mixed UNDER
    it. Music is auto-picked from the BGM folder (drop any number of tracks in
    C:\\Users\\Dell\\Desktop\\ProStudio\\BGM), looped to the narration length, kept
    low, and SIDECHAIN-DUCKED so it drops automatically whenever the voice speaks
    — the narration is always clearly on top. No folder / no tracks / PS_BGM=0 →
    the plain narration is used unchanged."""
    if os.environ.get("PS_BGM", "1").strip().lower() in ("0", "", "false", "no"):
        return vo_path
    tracks = _bgm_tracks()
    if not tracks or not (vo_path and os.path.isfile(vo_path)):
        return vo_path
    import random as _r
    rng = _r.Random(seed or 0)
    # Audible-but-gentle bed. The narration is almost continuous, so HARD
    # sidechain ducking (what we had) pinned the music near-silent the whole time
    # = "no music at all". A clearly-audible bed sits ~18-20% under the voice, so
    # the base is ~0.20 and the duck is LIGHT (it only dips the bed ~30% while the
    # voice speaks, then it breathes back up in the gaps).
    vol = os.environ.get("PS_BGM_VOL", "0.05")           # base bed level (~ -26 dB, faint)
    out = os.path.join(work, "vo_bgm.m4a")
    if os.path.isfile(out):
        return out
    # Long video, short tracks: with ONE track we loop it seamlessly to cover the
    # whole narration; with SEVERAL we cycle through all of them (shuffled, then
    # looped) so a 20-30 min video doesn't repeat the same 4-5 min bed over and
    # over. Either way the bed is trimmed to the narration length by amix.
    src_inputs = []
    if len(tracks) == 1:
        src_inputs = ["-stream_loop", "-1", "-i", tracks[0]]
    else:
        # `-stream_loop -1` on the concat demuxer is rejected on this ffmpeg
        # ("Operation not permitted"), so instead repeat the shuffled playlist
        # enough times to outlast any video (amix trims to the narration length).
        # Forward slashes only — backslashes in a concat list break the parse.
        order = tracks[:]
        rng.shuffle(order)
        listfile = os.path.join(work, "bgm_list.txt")
        with open(listfile, "w", encoding="utf-8") as lf:
            for _ in range(40):
                for tk in order:
                    safe = tk.replace("\\", "/").replace("'", "'\\''")
                    lf.write("file '" + safe + "'\n")
        src_inputs = ["-f", "concat", "-safe", "0", "-i", listfile]
    fc = (f"[1:a]volume={vol},aresample=48000[bg];"
          f"[bg][0:a]sidechaincompress=threshold=0.08:ratio=3:attack=40:"
          f"release=600:makeup=1[duck];"
          f"[0:a][duck]amix=inputs=2:duration=first:normalize=0,"
          f"alimiter=limit=0.95[a]")
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error",
           "-i", vo_path, *src_inputs,
           "-filter_complex", fc, "-map", "[a]",
           "-c:a", "aac", "-b:a", "192k", out]
    label = (os.path.basename(tracks[0]) if len(tracks) == 1
             else f"{len(tracks)} tracks (cycled)")
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=900)
        if r.returncode == 0 and os.path.isfile(out):
            log(f"  background music ON: {label} "
                f"(auto-ducked under narration, bed vol {vol})")
            return out
        log("  BGM mix failed — using plain narration: "
            + r.stderr.decode("utf-8", "replace")[:140])
    except Exception as exc:
        log(f"  BGM mix error ({type(exc).__name__}) — using plain narration")
    return vo_path


def render_job(job, shots, text_events, log=print, proxy=False, resume=False):
    style = _neutralize(dict(FORMATS[job.format_key]))
    W, H = RESOLUTIONS[job.resolution]
    out_path = job.out_path
    crf, preset = job.crf, job.preset
    if proxy:
        # a fast, watchable draft: small + ultrafast + no heavy effects, so the
        # review page appears in minutes. Placement/timing/text are identical
        # to the final, which is all the review needs to verify.
        W, H = 960, 540
        crf, preset = 30, "ultrafast"
        style["grain"] = 0
        style["glitch"] = False
        style["vignette"] = False
        style["spotlight"] = False
        out_path = os.path.splitext(job.out_path)[0] + "_proxy.mp4"
    # A persistent work dir per output lets a stopped render RESUME: already-
    # rendered shot segments are kept and skipped next time. Proxies are quick
    # and always start clean.
    keep_work = not proxy
    if proxy:
        work = tempfile.mkdtemp(prefix="prostudio_")
    else:
        work = os.path.splitext(out_path)[0] + "_work"
        if not resume:
            shutil.rmtree(work, ignore_errors=True)   # fresh start
        os.makedirs(work, exist_ok=True)
    # Mix a background-music bed under the narration (auto-picked + auto-ducked).
    # Proxies skip it for speed. Done once here so both the concat and the
    # crossfade compose paths pick up the music via job.audio.
    if not proxy and getattr(job, "audio", None):
        job.audio = mix_bgm(job.audio, work, log, seed=getattr(job, "seed", 0))
    try:
        glow = os.path.join(work, "glow.png")
        if style.get("spotlight"):
            _glow_png(glow)

        # transitions: format signature + curated variety, anti-repeat, rare
        # accents (see engine/variety). Clamp each to the neighbouring shots.
        import random as _random
        from .variety import plan_transitions
        n = len(shots)
        vplan = plan_transitions(shots, style,
                                 _random.Random(getattr(job, "seed", 0) or 1234))
        bare = bool(style.get("bare"))
        joins = []
        for i in range(n - 1):
            if bare:
                joins.append((None, 0.0))          # hard cuts, no overlap/pad
                continue
            ttype, tdur = vplan[i]
            tdur = min(tdur, shots[i].secs * 0.5, shots[i + 1].secs * 0.5)
            joins.append((ttype, max(0.05, tdur)))

        # Moving-card mode: a `want_frame` format WITH a background (image or
        # video) puts each shot in a varied-size card that slides in per shot,
        # instead of the one fixed whole-video frame. Each shot is tagged so
        # render_shot takes the moving-card path, and the final compose then skips
        # the static frame (the card IS the frame now).
        # Moving-card is the standard premium look now: ANY framed video with a
        # background (image or video) gets the per-shot varied card, not just the
        # N-formats. Bare/letterbox/spotlight keep their own dedicated looks.
        _fb = getattr(job, "frame_bg", None)
        mc_bg = _fb if (_fb and os.path.isfile(_fb)
                        and not style.get("bare")
                        and not style.get("letterbox")
                        and not style.get("spotlight")) else None
        if mc_bg:
            orig_bg = mc_bg
            # A still image is normalised to a WxH PNG ONCE (ffmpeg can't -loop an
            # AVIF/WEBP directly); a video background is used as-is.
            if not mc_bg.lower().endswith(_VID_EXT):
                bgp = os.path.join(work, "mc_bg.png")
                if not os.path.isfile(bgp):
                    _run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", mc_bg,
                          "-frames:v", "1", "-vf",
                          f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                          f"crop={W}:{H}", bgp], log)
                mc_bg = bgp
            # Card vs full-screen mix: most shots ride the card, but ~30% (biased
            # to VIDEO clips) play FULL-SCREEN to break the rhythm and keep the
            # viewer engaged — the "sometimes a card, sometimes full video" look.
            import random as _rr
            fulls = 0
            for i2, sh in enumerate(shots):
                r = _rr.Random((getattr(sh, "drift_seed", 0) or 1) * 7 + i2)
                # stills are the softest material (single, often upscaled-SD
                # frames): keep them in the card, full-screen only rarely
                p_full = 0.42 if sh.kind == "video" else 0.06
                if i2 > 0 and r.random() < p_full:
                    sh.framing = "full"; fulls += 1
                else:
                    sh.framing = "movingcard"
            log(f"  moving-card frame ON — per-shot varied size/border/slide over "
                f"{os.path.basename(orig_bg)}"
                + (" (video bg)" if orig_bg.lower().endswith(_VID_EXT) else "")
                + f"; {fulls}/{len(shots)} shots full-screen for variety")
        log(f"  rendering {n} shots at {W}x{H} ...")
        segs, degraded, cached = [], 0, 0
        todo = []
        for i, sh in enumerate(shots):
            seg = os.path.join(work, f"s{i:03d}.mp4")
            pad = joins[i][1] if i < n - 1 else 0.0
            want = sh.secs + pad
            segs.append(seg)
            # RESUME: reuse a segment only if it is fully rendered AND matches
            # the current resolution (a stale segment from a different-res run
            # would break the xfade compose).
            if (resume and os.path.isfile(seg)
                    and abs(duration(seg) - want) < 0.2
                    and _dims(seg) == (W, H)):
                cached += 1
                continue
            todo.append((i, sh, seg, pad))
        # Shots are independent -> render several at once. They used to go one
        # by one on a 24-core machine (~8 s each, ~20 min per video). Default 4
        # workers (RTX 50: NVENC sessions are limited); PS_RENDER_WORKERS=1
        # restores the old serial behaviour.
        workers = max(1, int(os.environ.get("PS_RENDER_WORKERS", "4")))
        done_n = cached

        def _one(item):
            i, sh, seg, pad = item
            return render_shot(sh, seg, style, job.niche, W, H, pad, glow, log,
                               work=work, bg=mc_bg)

        if workers == 1:
            results = ((it, _one(it)) for it in todo)
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            pool = ThreadPoolExecutor(max_workers=workers)
            futs = {pool.submit(_one, it): it for it in todo}
            results = ((futs[f], f.result()) for f in as_completed(futs))
        log(f"  rendering with {workers} parallel worker(s)"
            + (f", {cached} shot(s) reused" if cached else ""))
        for (i, sh, seg, pad), tier in results:
            if tier != "full":
                degraded += 1
            done_n += 1
            # shots span 25%..85% of the whole job
            pct = 25 + int(60 * done_n / n)
            log(f"[{pct:3d}%] rendered shot {done_n}/{n}")
        if workers > 1:
            pool.shutdown(wait=True)
        if cached:
            log(f"  resumed: {cached}/{n} shot(s) reused from the last run")
        if degraded:
            log(f"  note: {degraded}/{n} shot(s) used a simplified render "
                "(a source file was too large/slow/corrupt) — the video is "
                "complete; you can swap those clips in your editor if needed.")

        total = sum(sh.secs for sh in shots)     # audio-driven target length
        nets = [sh.secs for sh in shots]
        # at 4K, "medium" is needlessly slow — "fast" at the same CRF looks all
        # but identical and cuts compose time a lot.
        if not proxy and W * H >= 3840 * 2160 and preset in (
                "medium", "slow", "slower"):
            preset = "fast"
        out = out_path
        G = _group_size(W, H)
        kind = "draft proxy" if proxy else "final video"

        if bare:
            # No-Filter: hard-cut concat with NO video re-encode (stream copy)
            # + the narration. On a multi-hour video this is seconds, not hours.
            log(f"[ 88%] joining {len(segs)} clips + audio (No Filter, no re-encode) ...")
            _concat_copy(segs, out, job.audio, log)
        elif n <= G:
            # small enough to cross-fade in one memory-safe pass
            log(f"[ 88%] compositing {kind} (~{total:.0f}s at {W}x{H}/{preset}) "
                "— live progress below ...")
            _compose_chain(segs, nets, joins, out, W, H, crf, preset, work, log,
                           job=job, text_events=text_events, audio=job.audio,
                           total=total, tag="final", progress=True,
                           frame_bg=(None if mc_bg else getattr(job, "frame_bg", None)))
        else:
            # MANY shots: cross-fade in bounded groups first (so ffmpeg never
            # opens ~150 decoders at once -> no 'Cannot allocate memory'), then
            # cross-fade the group clips (with text + audio) at the top.
            starts = list(range(0, n, G))
            log(f"[ 86%] compositing {kind} in {len(starts)} memory-safe groups "
                f"(~{total:.0f}s at {W}x{H}) ...")
            gclips, gnets, gjoins = [], [], []
            for gi, a in enumerate(starts):
                b = min(a + G, n)
                gc = os.path.join(work, f"g{gi:03d}.mp4")
                # intermediate clips near-lossless (crf 16) so the second pass
                # doesn't visibly degrade them
                _compose_chain(segs[a:b], nets[a:b], joins[a:b - 1], gc, W, H,
                               16, "veryfast", work, log, tag=f"g{gi}")
                gclips.append(gc)
                gnets.append(sum(nets[a:b]))
                if b < n:
                    gjoins.append(joins[b - 1])
                pct = 86 + int(6 * (gi + 1) / len(starts))
                log(f"[{pct:3d}%] group {gi + 1}/{len(starts)} composed")
            log(f"[ 94%] joining {len(gclips)} groups + audio — live progress ...")
            _compose_chain(gclips, gnets, gjoins, out, W, H, crf, preset, work,
                           log, job=job, text_events=text_events,
                           audio=job.audio, total=total, tag="final",
                           progress=True, frame_bg=(None if mc_bg else getattr(job, "frame_bg", None)))
        log(f"[100%] done: {out} ({total:.1f}s, {os.path.getsize(out)/1e6:.1f} MB)")
        # success -> the checkpoint is no longer needed
        shutil.rmtree(work, ignore_errors=True)
        return out, total
    except BaseException:
        # a stop / crash leaves the checkpoint in place so the next run resumes
        # (proxies keep no checkpoint)
        if not keep_work:
            shutil.rmtree(work, ignore_errors=True)
        raise
