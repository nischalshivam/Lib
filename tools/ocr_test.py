"""RapidOCR as a pre-filter for the burned-in-text check: recall on catalogue shots that 3.8 marked text:true, flag
rate on text:false shots (flagged ones still go to Gemini, so a false flag only costs one call)."""
import glob, json, os, random, subprocess, sys, time
import numpy as np, cv2
from rapidocr_onnxruntime import RapidOCR

ocr = RapidOCR()
cats = []
for root in (r"I:\My Drive\Movies DATA\Reacher", r"E:\Movies\The Wire"):
    cats += glob.glob(os.path.join(root, "**", "*.catalog.json"), recursive=True)
rnd = random.Random(5)
pos, neg = [], []
for f in rnd.sample(cats, min(24, len(cats))):
    d = json.load(open(f, encoding="utf-8"))
    vid = next((f[:-13] + e for e in (".mkv", ".mp4") if os.path.exists(f[:-13] + e)), None)
    if not vid:
        continue
    sh = [s for s in d["shots"] if s.get("luma", 99) >= 25 and s["end"] - s["start"] >= 1]
    pos += [(vid, s) for s in rnd.sample([s for s in sh if s.get("text")], min(4, sum(1 for s in sh if s.get("text"))))]
    neg += [(vid, s) for s in rnd.sample([s for s in sh if not s.get("text")], 6)]


def frame(vid, t):
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", vid, "-frames:v", "1", "-vf", "scale=960:-2",
                        "-f", "image2", "-c:v", "png", "-"], capture_output=True)
    return cv2.imdecode(np.frombuffer(r.stdout, np.uint8), cv2.IMREAD_COLOR)


def has_text(img):
    res, _ = ocr(img)
    words = [(t, c) for _, t, c in (res or []) if float(c) >= 0.6 and len(t.strip()) >= 3]
    return bool(words), words


T = time.time()
out = {}
for name, items in (("text:true", pos), ("text:false", neg)):
    hit, ex = 0, []
    for vid, s in items:
        img = frame(vid, (s["start"] + s["end"]) / 2)
        if img is None:
            continue
        h, w = has_text(img)
        hit += h
        if len(ex) < 4 and ((name == "text:true") != h):
            ex.append(f"{os.path.basename(vid)[:30]} @{s['start']:.0f}s {s.get('description', '')[:60]} -> {[x[0] for x in w][:3]}")
    out[name] = (hit, len(items), ex)
for k, (h, n, ex) in out.items():
    print(f"{k}: OCR found text in {h}/{n} ({100 * h // max(1, n)}%)")
    for e in ex:
        print("   e.g.", e)
print(f"{(time.time() - T) / max(1, len(pos) + len(neg)):.2f} s per frame")
