"""AI upscale of the video's STILLS with Real-ESRGAN (ncnn-vulkan, runs on the
GPU, free, offline — D:\\Lib\\tools\\realesrgan).

Why: a still is one frame held on screen for seconds under a slow zoom, so the
source's softness shows far more than in a moving clip — Malcolm's "1080p" files
are an upscaled SD master, Young Sheldon is 720p. Each still is taken down to
960 px wide (≈ the real detail of those sources), upscaled x4 by
realesrgan-x4plus, brought back to its own size and BLENDED 60/40 with the
original: ESRGAN alone sharpens edges but paints skin like wax; the 40 % of the
original keeps real texture and grain. Measured on true-HD footage (GoT) the
blend does no harm, so every still gets it — "is this source soft?" metrics
could not tell 720p from 1080p reliably.

All stills of a build go through ONE ncnn process (the model loads once),
~1-3 s per still on the RTX 5070. MV_AI_UPSCALE=0 turns it off; a missing
tool or any failure leaves the stills untouched (never blocks a video)."""
from __future__ import annotations

import glob
import re
import os
import shutil
import subprocess
import tempfile
import time

TOOL = os.environ.get("MV_REALESRGAN") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tools", "realesrgan", "realesrgan-ncnn-vulkan.exe")
MODEL = "realesrgan-x4plus"
IN_WIDTH = 960          # the still is reduced to this before the x4 pass
BLEND = 0.6             # share of the AI image in the final still
IMG_EXT = (".jpg", ".jpeg", ".png")


def available() -> bool:
    return os.environ.get("MV_AI_UPSCALE", "1") != "0" and os.path.isfile(TOOL)


def _stills(build_dir: str) -> list:
    out = []
    for sd in sorted(glob.glob(os.path.join(build_dir, "scene_*"))):
        if not os.path.isdir(sd):
            continue
        for n in sorted(os.listdir(sd)):
            p = os.path.join(sd, n)
            if n.lower().endswith(IMG_EXT) and os.path.isfile(p):
                out.append(p)
    return out


# Only LOW-quality sources are worth the GPU time (user, 2026-09-27): a source
# at <= 720 lines, or a show listed here whose "1080p" files are really an
# upscaled SD master (Malcolm's AMZN release). Extend via
# D:\Lib\upscale_shows.json  ->  ["malcolm in the middle", ...]  (show_key form).
MAX_SOURCE_H = int(os.environ.get("MV_AI_UPSCALE_MAX_H", "720"))
DEFAULT_SOFT_SHOWS = ["malcolm in the middle"]
# ultra-long videos (1-2 h, hundreds of stills) skip it — too slow for the gain
MAX_VIDEO_MIN = float(os.environ.get("MV_AI_UPSCALE_MAX_MIN", "40"))


def _soft_shows() -> set:
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "upscale_shows.json")
    try:
        import json                                          # noqa: PLC0415
        with open(p, encoding="utf-8") as f:
            return {str(x).lower() for x in json.load(f)}
    except (OSError, ValueError):
        return set(DEFAULT_SOFT_SHOWS)


_H_CACHE: dict = {}


def _height(path: str) -> int:
    if path not in _H_CACHE:
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=height", "-of", "csv=p=0", path],
                capture_output=True, text=True, timeout=60).stdout.strip()
            _H_CACHE[path] = int(out.split(",")[0]) if out else 0
        except Exception:
            _H_CACHE[path] = 0
    return _H_CACHE[path]


def _needs_upscale(build_dir: str, log=print) -> set:
    """Still paths (in scene folders) whose SOURCE is low quality."""
    import json                                              # noqa: PLC0415
    from .subtitles import show_key                          # noqa: PLC0415
    try:
        with open(os.path.join(build_dir, "manifest.json"), encoding="utf-8") as f:
            man = json.load(f)
    except (OSError, ValueError):
        return set(_stills(build_dir))                        # no manifest: all
    soft = {show_key(x) for x in _soft_shows()}
    want, skipped = set(), 0
    for sc in man.get("scenes") or []:
        sdir = os.path.join(build_dir, f"scene_{int(sc.get('scene') or 0):03d}")
        for a in sc.get("assets") or []:
            if a.get("kind") != "image":
                continue
            src = str(a.get("source_file") or "")
            label = re.sub(r"\s*\bS\d{1,2}\s*E\d{1,3}.*$", "", str(a.get("source") or ""),
                           flags=re.I)
            h = _height(src) if src and os.path.isfile(src) else 0
            if show_key(label) in soft or (0 < h <= MAX_SOURCE_H):
                want.add(os.path.join(sdir, a["file"]))
            else:
                skipped += 1
    if skipped:
        log(f"  AI upscale: {skipped} still(s) from HD sources skipped (already sharp)")
    return want


def enhance_stills(build_dir: str, log=print, video_seconds: float = 0.0) -> int:
    """Upscale the build's stills that come from LOW-quality sources, in place.
    Returns the number of stills improved."""
    if not available():
        if os.environ.get("MV_AI_UPSCALE", "1") != "0":
            log(f"  AI upscale: tool not found ({TOOL}) — stills left as they are")
        return 0
    if video_seconds and video_seconds > MAX_VIDEO_MIN * 60:
        log(f"  AI upscale: skipped — {video_seconds / 60:.0f}-min video is over "
            f"{MAX_VIDEO_MIN:.0f} min (MV_AI_UPSCALE_MAX_MIN)")
        return 0
    from PIL import Image                                    # noqa: PLC0415
    pick = _needs_upscale(build_dir, log)
    stills = [p for p in _stills(build_dir) if p in pick]
    if not stills:
        return 0
    t0 = time.time()
    work = tempfile.mkdtemp(prefix="esr_", dir=build_dir)
    src_dir, up_dir = os.path.join(work, "in"), os.path.join(work, "out")
    os.makedirs(src_dir)
    os.makedirs(up_dir)
    names = {}
    try:
        for i, p in enumerate(stills):
            try:
                im = Image.open(p).convert("RGB")
                w, h = im.size
                if w > IN_WIDTH:
                    im = im.resize((IN_WIDTH, max(2, round(h * IN_WIDTH / w))),
                                   Image.BOX)
                n = f"{i:05d}.png"
                im.save(os.path.join(src_dir, n))
                names[n] = p
            except Exception:
                continue
        r = subprocess.run([TOOL, "-i", src_dir, "-o", up_dir, "-n", MODEL, "-s", "4",
                            "-f", "png"], capture_output=True, text=True,
                           cwd=os.path.dirname(TOOL),
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            log(f"  AI upscale: realesrgan failed (exit {r.returncode}) — stills "
                f"left as they are: {(r.stderr or '').strip()[-160:]}")
            return 0
        done = 0
        for n, p in names.items():
            up = os.path.join(up_dir, n)
            if not os.path.isfile(up):
                continue
            try:
                orig = Image.open(p).convert("RGB")
                ai = Image.open(up).convert("RGB").resize(orig.size, Image.LANCZOS)
                res = Image.blend(orig, ai, BLEND)
                tmp = p + ".esr.tmp"
                fmt = "PNG" if p.lower().endswith(".png") else "JPEG"
                res.save(tmp, fmt, **({"quality": 95, "subsampling": 0}
                                      if fmt == "JPEG" else {}))
                os.replace(tmp, p)
                done += 1
            except Exception:
                continue
        log(f"  AI upscale (Real-ESRGAN): {done}/{len(stills)} stills sharpened "
            f"in {time.time() - t0:.0f}s")
        return done
    finally:
        shutil.rmtree(work, ignore_errors=True)
