"""Per-episode burned-in-text zones (opening/closing credits, title cards).
Scans the first 300 s and the last 90 s of an episode at 1 fps in 4x4 grids;
Gemini marks frames with credits/titles. Cached forever in text_zones.json,
so every later video that uses the episode gets it for free."""
import base64, io, json, os, subprocess, sys, urllib.request
from concurrent.futures import ThreadPoolExecutor
try:                                     # shared cache: several builds read/write it at once (2026-10-13 crash in cut)
    from .common import json_read, json_update
except ImportError:                      # a build started before common.py had these (module already loaded): local copy
    import time as _t

    def json_read(path, default=None):
        for _ in range(10):
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except FileNotFoundError:
                return {} if default is None else default
            except (ValueError, OSError):
                _t.sleep(0.3)
        return {} if default is None else default

    def json_update(path, fn):
        import msvcrt
        with open(path + ".lock", "a+") as lk:
            for _ in range(600):
                try:
                    lk.seek(0)
                    msvcrt.locking(lk.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    _t.sleep(0.1)
            try:
                data = json_read(path, {})
                fn(data)
                tmp = f"{path}.{os.getpid()}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=0)
                os.replace(tmp, path)
                return data
            finally:
                try:
                    lk.seek(0)
                    msvcrt.locking(lk.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache", "text_zones.json")
PROXY = "http://127.0.0.1:8317/v1/chat/completions"
KEY = open(os.path.expanduser("~/.model-router-proxy-key.txt")).read().strip()
MODEL = "gemini-3.6-flash-high"


def lp(p):
    return "\\\\?\\" + p if len(p) >= 250 and not p.startswith("\\\\?\\") else p


def _dur(v):
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", lp(v)],
                                capture_output=True, text=True).stdout or 0)


def _grid(video, t0):
    f = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", 30)
    # one ffmpeg pass: 16 frames at 1 fps
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t0:.2f}", "-t", "16", "-i", lp(video),
                        "-vf", "fps=1,scale=320:180,tile=4x4", "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg", "-"],
                       capture_output=True)
    if len(r.stdout) < 1000:
        return None                                   # past the end / unreadable -> no grid
    G = Image.open(io.BytesIO(r.stdout)).convert("RGB")
    d = ImageDraw.Draw(G)
    for k in range(16):
        x, y = (k % 4) * 320, (k // 4) * 180
        d.rectangle([x, y, x + 40, y + 34], fill="black")
        d.text((x + 5, y + 1), str(k + 1), font=f, fill="yellow")
    b = io.BytesIO(); G.save(b, "JPEG", quality=80)
    return base64.b64encode(b.getvalue()).decode()


def _ask(video, t0):
    q = ("16 numbered frames, 1 second apart, from a TV episode. List the numbers of frames that show BURNED-IN text "
         "laid over the picture: opening/closing credits (names of actors, writers, producers, creators), the show's "
         "title card / title sequence graphics, network logos. Ignore text that is part of the set (signs, books). "
         'Reply ONLY JSON {"text_frames":[...]}')
    g = _grid(video, t0)
    if g is None:
        return []
    body = {"model": MODEL, "temperature": 0, "messages": [{"role": "user", "content": [
        {"type": "text", "text": q}, {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + g}}]}]}
    for _ in range(3):
        try:
            req = urllib.request.Request(PROXY, json.dumps(body).encode(), {"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
            txt = json.load(urllib.request.urlopen(req, timeout=120))["choices"][0]["message"]["content"]
            v = json.JSONDecoder().raw_decode(txt[txt.find("{"):])[0]
            return [t0 + int(k) - 1 for k in v.get("text_frames", []) if 1 <= int(k) <= 16]
        except Exception:
            continue
    return []


def zones(video, log=print):
    cache = json_read(CACHE, {})
    key = os.path.basename(video)
    if key in cache:
        return cache[key]
    dur = _dur(video)
    starts = list(range(0, min(300, int(dur)), 16)) + list(range(max(0, int(dur) - 90), max(1, int(dur) - 17), 16))
    with ThreadPoolExecutor(8) as ex:
        hits = sorted(t for lst in ex.map(lambda s: _ask(video, s), starts) for t in lst)
    # merge 1-fps hits into intervals (pad 0.6 s each side, join gaps < 2.5 s)
    iv = []
    for t in hits:
        a, b = t - 0.6, t + 1.6
        if iv and a - iv[-1][1] < 2.5:
            iv[-1][1] = b
        else:
            iv.append([a, b])
    json_update(CACHE, lambda d: d.__setitem__(key, iv))
    log(f"  text zones {key[:50]}: {[(round(a), round(b)) for a, b in iv]}")
    return iv


if __name__ == "__main__":
    for v in sys.argv[1:]:
        print(zones(v))
