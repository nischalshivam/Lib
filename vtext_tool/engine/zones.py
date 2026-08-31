"""Frame analysis -> best text zone per event (3x3 grid, dynamically scored).

Scores brightness, busyness (edge density) and faces on frames sampled from
the event window; the text goes to quiet negative space, never on a face.
"""
from __future__ import annotations

import os
import subprocess

import numpy as np
from PIL import Image

from .util import ffmpeg_exe

try:
    import cv2
    _CASCADES = [cv2.CascadeClassifier(cv2.data.haarcascades + f)
                 for f in ("haarcascade_frontalface_default.xml",
                           "haarcascade_profileface.xml")]
except Exception:
    cv2 = None
    _CASCADES = []

ZONES = [(r, c) for r in range(3) for c in range(3)]
# mild built-in preferences: bottom-center reads as subtitles, dead-center
# fights the subject
_ZONE_BIAS = {(1, 1): 0.35, (2, 1): 0.25, (0, 1): 0.10}
_AW, _AH = 480, 270  # analysis resolution


def grab_frame(video: str, t: float, path: str, scale: int = 0):
    vf = ["-vf", f"scale={scale}:-2"] if scale else []
    subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", f"{max(0, t):.3f}", "-i", video, "-frames:v", "1",
                    *vf, path], check=True, capture_output=True)
    return path if os.path.exists(path) else None


def analyze_event_window(video: str, t0: float, t1: float, tmpdir: str,
                         key: str):
    """Return zone_scores dict {(r,c): score 0..1 (lower=better)} plus
    per-zone luminance for readability treatment."""
    times = [t0 + 0.15, (t0 + t1) / 2, max(t0 + 0.2, t1 - 0.25)]
    imgs = []
    for i, t in enumerate(times):
        p = os.path.join(tmpdir, f"z_{key}_{i}.jpg")
        try:
            if grab_frame(video, t, p, scale=_AW):
                imgs.append(np.asarray(Image.open(p).convert("RGB")))
        except Exception:
            continue
    if not imgs:
        return {z: 0.5 for z in ZONES}, {z: 0.3 for z in ZONES}

    scores = {z: [] for z in ZONES}
    lums = {z: [] for z in ZONES}
    for arr in imgs:
        h, w = arr.shape[:2]
        gray = arr.mean(axis=2) / 255.0
        gy, gx = np.gradient(gray)
        edges = np.hypot(gx, gy)
        faces = _detect_faces(arr)
        for (r, c) in ZONES:
            ys, ye = int(r * h / 3), int((r + 1) * h / 3)
            xs, xe = int(c * w / 3), int((c + 1) * w / 3)
            zl = float(gray[ys:ye, xs:xe].mean())
            zstd = float(gray[ys:ye, xs:xe].std())
            zedge = float(np.clip(edges[ys:ye, xs:xe].mean() * 8.0, 0, 1))
            face_frac = _overlap_frac(faces, xs, ys, xe, ye, w, h)
            s = (2.2 * face_frac + 0.9 * zedge + 0.55 * zstd
                 + 0.35 * max(0.0, zl - 0.55)
                 + _ZONE_BIAS.get((r, c), 0.0))
            scores[(r, c)].append(s)
            lums[(r, c)].append(zl)
    zone_scores = {z: float(np.mean(v)) for z, v in scores.items()}
    zone_lum = {z: float(np.mean(v)) for z, v in lums.items()}
    return zone_scores, zone_lum


def _detect_faces(arr):
    if not _CASCADES:
        return []
    g = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    boxes = []
    for cas in _CASCADES:
        if cas.empty():
            continue
        for (x, y, w, h) in cas.detectMultiScale(g, 1.15, 4,
                                                 minSize=(28, 28)):
            boxes.append((int(x), int(y), int(x + w), int(y + h)))
    return boxes


def _overlap_frac(boxes, xs, ys, xe, ye, w, h):
    if not boxes:
        return 0.0
    zone_area = (xe - xs) * (ye - ys)
    ov = 0.0
    for (x0, y0, x1, y1) in boxes:
        # pad face boxes: keep text off hair/chin too
        px, py = int((x1 - x0) * 0.2), int((y1 - y0) * 0.3)
        x0, y0 = max(0, x0 - px), max(0, y0 - py)
        x1, y1 = min(w, x1 + px), min(h, y1 + py)
        ix = max(0, min(x1, xe) - max(x0, xs))
        iy = max(0, min(y1, ye) - max(y0, ys))
        ov += ix * iy
    return min(1.0, ov / max(1, zone_area))


def pick_zone(zone_scores: dict, recent_zones: list):
    """Lowest score wins; recently used zones get a growing penalty so
    placement varies naturally with the footage."""
    best, best_s = None, 1e9
    for z, s in zone_scores.items():
        pen = 0.0
        for age, rz in enumerate(reversed(recent_zones[-4:])):
            if rz == z:
                pen += 0.30 / (age + 1)
        if s + pen < best_s:
            best, best_s = z, s + pen
    return best
