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
from .formats import FORMATS, grade_for, theme_color as _theme_color
from .textlayout import chunk_filters


def _run(cmd, log, timeout=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"ffmpeg timed out after {timeout}s (stuck process, likely waiting "
            "on stdin) -> aborting this shot")
        raise RuntimeError("ffmpeg timed out")
    if p.returncode:
        log("ffmpeg error:\n" + p.stderr[-1500:])
        raise RuntimeError("ffmpeg failed")


def _run_progress(cmd, log, total, work, lo=88, hi=99, stall_secs=900):
    """Run a long ffmpeg with LIVE progress so the final compose step never
    looks frozen. Maps encoded time -> [lo..hi]%. Aborts only if there is NO
    progress at all for `stall_secs` (a genuine hang), not just because it's
    slow."""
    import time
    errpath = os.path.join(work, "compose_err.log")
    with open(errpath, "w", encoding="utf-8") as errf:
        p = subprocess.Popen(cmd + ["-progress", "pipe:1", "-nostats"],
                             stdout=subprocess.PIPE, stderr=errf, text=True)
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
           "-an", "-r", str(FPS), "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-preset", "veryfast", out]
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


def _zoompan_image(shot, W, H, secs, style):
    frames = max(1, int(secs * FPS))
    big = 2 if H <= 1080 else 1.5
    bw, bh = int(W * big), int(H * big)
    rate = 0.0010
    zmax = 1.16 if not style.get("strong_push") else 1.30
    drift = style["drift"]
    a = drift * 5 * (H / 1080)
    ph = shot.drift_seed % 7
    punch = ""
    if shot.punch_in:
        n = int(frames * 0.55)
        punch = f"+if(gte(on\\,{n})\\,min(0.05\\,(on-{n})*0.012)\\,0)"
    move = getattr(shot, "move", "") or ("in" if shot.zoom_in else "out")
    if style.get("pan") == "lr":                 # F8 keeps its horizontal pan
        move = "panr" if shot.drift_seed % 2 == 0 else "panl"
    cx = f"iw/2-(iw/zoom/2)+{a:.1f}*sin(on/37+{ph})"
    cy = f"ih/2-(ih/zoom/2)+{a * 0.7:.1f}*cos(on/43+{ph})"
    x, y = cx, cy
    if move == "out":
        z = f"max({zmax}-{rate}*on{punch},1.0)"
    elif move == "hold":
        z = "1.04"
    elif move in ("panl", "panr", "panu", "pand"):
        z = "1.12"
        d2 = a * 0.5
        if move == "panr":
            x = f"(iw-iw/zoom)*on/{frames}"
            y = f"ih/2-(ih/zoom/2)+{d2:.1f}*cos(on/50+{ph})"
        elif move == "panl":
            x = f"(iw-iw/zoom)*(1-on/{frames})"
            y = f"ih/2-(ih/zoom/2)+{d2:.1f}*cos(on/50+{ph})"
        elif move == "pand":
            y = f"(ih-ih/zoom)*on/{frames}"
            x = f"iw/2-(iw/zoom/2)+{d2:.1f}*sin(on/50+{ph})"
        else:                                     # panu
            y = f"(ih-ih/zoom)*(1-on/{frames})"
            x = f"iw/2-(iw/zoom/2)+{d2:.1f}*sin(on/50+{ph})"
    else:                                         # "in"
        z = f"min(1.0+{rate}*on{punch},{zmax})"
    return (f"scale={bw}:{bh}:force_original_aspect_ratio=increase:"
            f"flags=lanczos,crop={bw}:{bh},"
            f"zoompan=z='{z}':d={frames}:x='{x}':y='{y}':s={W}x{H}:fps={FPS},"
            f"setsar=1")


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
              "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
              tmp], log, timeout=120)
        os.replace(tmp, out)


def _render_full(shot, out, style, niche, W, H, secs, glow, log, timeout):
    """The full styled render (Ken Burns / drift / grade / grain / glitch)."""
    if shot.kind == "image":
        vf = _zoompan_image(shot, W, H, secs, style)
        ins = ["-loop", "1", "-t", f"{secs + 0.4:.3f}", "-i", shot.path]
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
    cmd += ["-t", f"{secs:.3f}", "-an", "-r", str(FPS), "-c:v", "libx264",
            "-pix_fmt", "yuv420p", "-preset", "veryfast", out]
    _run(cmd, log, timeout=timeout)
    _ensure_duration(out, secs, log)


def _safe_still(path, kind, W, H, work, log, timeout=60, src_in=0.0):
    """Decode ONE frame (single pass, bounded) and normalize it to WxH.

    This is the escape hatch for a pathological input (huge / corrupt /
    exotic-codec video): one decode instead of per-frame heavy filtering.
    Returns the still path or None."""
    still = os.path.join(work, "safe_" + str(abs(hash(path)) % 10**8) + ".png")
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
    z = 1.10
    x = "iw/2-(iw/zoom/2)"
    y = "ih/2-(ih/zoom/2)"
    frames = max(1, int(secs * FPS))
    if shot.zoom_in:
        zexpr = f"min(1.0+0.0009*on,{z})"
    else:
        zexpr = f"max({z}-0.0009*on,1.0)"
    vf = (f"scale={int(W*1.2)}:{int(H*1.2)}:flags=bilinear,"
          f"zoompan=z='{zexpr}':d={frames}:x='{x}':y='{y}':s={W}x{H}:fps={FPS},"
          f"setsar=1," + grade_for(niche, shot.mood, style["sepia"]))
    if style["letterbox"]:
        vh = int(W * 9 / 21)
        vf += f",crop={W}:{vh},pad={W}:{H}:0:(oh-ih)/2:black"
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-loop", "1",
           "-t", f"{secs + 0.4:.3f}", "-i", still, "-vf", vf,
           "-t", f"{secs:.3f}", "-an", "-r", str(FPS), "-c:v", "libx264",
           "-pix_fmt", "yuv420p", "-preset", "veryfast", out]
    _run(cmd, log, timeout=timeout)
    _ensure_duration(out, secs, log)


def _render_filler(out, W, H, secs, log):
    """Last resort: a neutral dark clip of the exact duration, so the
    timeline never breaks even if a file is completely unusable."""
    _run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi",
          "-i", f"color=c=0x0b0d10:s={W}x{H}:r={FPS}:d={secs:.3f}",
          "-t", f"{secs:.3f}", "-c:v", "libx264", "-pix_fmt", "yuv420p",
          "-preset", "veryfast", out], log, timeout=60)


def render_shot(shot, out, style, niche, W, H, pad, glow, log, work=None):
    """Render one shot, GUARANTEED to produce a valid segment.

    Tier 1: full styled render (bounded timeout).
    Tier 2: on timeout/failure, a single-decode safe still (gentle motion).
    Tier 3: on any further failure, neutral filler of the right length.
    A single bad/huge/corrupt file can never stall or fail the whole job."""
    secs = shot.secs + pad
    work = work or os.path.dirname(out)
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


def _compose_chain(clips, nets, joins, out, W, H, crf, preset, work, log,
                   job=None, text_events=None, audio=None, total=None,
                   tag="c", progress=False):
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
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    if filt:
        graph_file = os.path.join(work, f"graph_{tag}.txt")
        with open(graph_file, "w", encoding="utf-8") as f:
            f.write(";\n".join(filt))
        cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", *inputs,
               "-filter_complex_script", graph_file, *maps,
               "-c:v", "libx264", "-crf", str(crf), "-preset", preset,
               "-pix_fmt", "yuv420p", "-r", str(FPS), *acodec,
               "-movflags", "+faststart", "-t", f"{total:.3f}", out]
    else:                                   # single clip, no filters -> copy
        cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", clips[0],
               "-c", "copy", "-t", f"{total:.3f}", out]
    if progress:
        _run_progress(cmd, log, total=total, work=work)
    else:
        _run(cmd, log, timeout=None)
    return out, comp_total


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
        joins = []
        for i in range(n - 1):
            ttype, tdur = vplan[i]
            tdur = min(tdur, shots[i].secs * 0.5, shots[i + 1].secs * 0.5)
            joins.append((ttype, max(0.05, tdur)))

        log(f"  rendering {n} shots at {W}x{H} ...")
        segs, degraded, cached = [], 0, 0
        for i, sh in enumerate(shots):
            seg = os.path.join(work, f"s{i:03d}.mp4")
            pad = joins[i][1] if i < n - 1 else 0.0
            want = sh.secs + pad
            # RESUME: reuse a segment only if it is fully rendered AND matches
            # the current resolution (a stale segment from a different-res run
            # would break the xfade compose).
            if (resume and os.path.isfile(seg)
                    and abs(duration(seg) - want) < 0.2
                    and _dims(seg) == (W, H)):
                segs.append(seg)
                cached += 1
                pct = 25 + int(60 * (i + 1) / n)
                log(f"[{pct:3d}%] shot {i + 1}/{n} (already done — skipped)")
                continue
            tier = render_shot(sh, seg, style, job.niche, W, H, pad, glow, log,
                               work=work)
            if tier != "full":
                degraded += 1
            segs.append(seg)
            # shots span 25%..85% of the whole job
            pct = 25 + int(60 * (i + 1) / n)
            log(f"[{pct:3d}%] rendered shot {i + 1}/{n}")
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

        if n <= G:
            # small enough to cross-fade in one memory-safe pass
            log(f"[ 88%] compositing {kind} (~{total:.0f}s at {W}x{H}/{preset}) "
                "— live progress below ...")
            _compose_chain(segs, nets, joins, out, W, H, crf, preset, work, log,
                           job=job, text_events=text_events, audio=job.audio,
                           total=total, tag="final", progress=True)
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
                           progress=True)
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
