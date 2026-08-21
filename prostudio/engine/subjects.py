"""Subject awareness — keep text in the frame's NEGATIVE SPACE.

Face detection via OpenCV (CPU, bundled haarcascade). For each visual we pick
the safest text zone out of: bottom-center, lower-left, lower-right, top-left,
top-right — the one whose area overlaps faces the least (competitor videos
never cover the subject's face with text).

Optional (off by default): rembg subject mask for text-BEHIND-subject.
Gracefully disabled when rembg isn't installed.
"""
from __future__ import annotations

import os
import subprocess
import tempfile

_CASCADE = None


def _cascade():
    global _CASCADE
    if _CASCADE is None:
        try:
            import cv2
            path = os.path.join(cv2.data.haarcascades,
                                "haarcascade_frontalface_default.xml")
            _CASCADE = cv2.CascadeClassifier(path)
        except Exception:
            _CASCADE = False
    return _CASCADE


# zones as fractions of the frame: (x0, y0, x1, y1)
ZONES = {
    "bottom": (0.20, 0.72, 0.80, 0.94),
    "lower_left": (0.06, 0.55, 0.44, 0.92),
    "lower_right": (0.56, 0.55, 0.94, 0.92),
    "top_left": (0.06, 0.08, 0.44, 0.40),
    "top_right": (0.56, 0.08, 0.94, 0.40),
}
ZONE_PRIORITY = ["bottom", "lower_left", "lower_right", "top_left", "top_right"]


def detect_faces(path: str, kind: str):
    """[(x0,y0,x1,y1) fractions] of detected faces in the visual's mid frame."""
    casc = _cascade()
    if casc is False or casc is None or casc.empty():
        return []
    try:
        import cv2
        if kind == "image":
            img = cv2.imread(path)
        else:
            tmp = os.path.join(tempfile.gettempdir(), "_psface.png")
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "2",
                            "-i", path, "-frames:v", "1", tmp],
                           capture_output=True)
            img = cv2.imread(tmp)
        if img is None:
            return []
        h, w = img.shape[:2]
        scale = 640 / max(w, 1)
        small = cv2.resize(img, (640, max(1, int(h * scale))))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        faces = casc.detectMultiScale(gray, 1.15, 5, minSize=(40, 40))
        sh, sw = small.shape[:2]
        return [(x / sw, y / sh, (x + fw) / sw, (y + fh) / sh)
                for (x, y, fw, fh) in faces]
    except Exception:
        return []


def _overlap(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return ix * iy


def best_text_zone(path: str, kind: str) -> str:
    """Zone name whose area the subject's face(s) overlap least."""
    faces = detect_faces(path, kind)
    if not faces:
        return "bottom"
    scores = []
    for name in ZONE_PRIORITY:
        z = ZONES[name]
        scores.append((sum(_overlap(z, f) for f in faces), name))
    scores.sort()
    return scores[0][1]


def try_subject_mask(path: str, out_png: str) -> bool:
    """Optional text-behind-subject cutout via rembg. Returns False when the
    dependency (or its model download) is unavailable — feature simply off."""
    try:
        from rembg import remove
        with open(path, "rb") as f:
            data = remove(f.read())
        with open(out_png, "wb") as f:
            f.write(data)
        return True
    except Exception:
        return False
