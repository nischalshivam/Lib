"""Per-episode burned-in-text zones (opening/closing credits, title cards).
Scans the first 300 s and the last 90 s of an episode at 1 fps in 4x4 grids;
Gemini marks frames with credits/titles. Cached forever in text_zones.json,
so every later video that uses the episode gets it for free."""
import base64, io, json, os, subprocess, sys, time, urllib.request
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
                for i in range(40):                       # 2026-10-20 TAHM 11: 'Access is denied' while another build reads it
                    try:
                        os.replace(tmp, path)
                        break
                    except PermissionError:
                        if i == 39:
                            raise
                        _t.sleep(0.25)
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
# 2026-10-16: was a fixed "gemini-3.6-flash-high" - retired 2026-10-13, so every call failed, _ask() returned [] and
# zones() CACHED "no credits" for every episode seen since (164 of 302, all 60 The Wire). Now the shared vision chain.
try:
    from . import common as _E
except ImportError:
    _E = None
MODEL = "gemini-3.8-flash-high"


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
    # 2026-10-17: through common.vision (the paid provider / outage waits), no own proxy call any more
    v = _E.vision(q, base64.b64decode(g), tag="zones") if _E else None
    if not isinstance(v, dict):
        return None                       # the call FAILED - not "no text here"
    try:
        return [t0 + int(k) - 1 for k in v.get("text_frames", []) if 1 <= int(k) <= 16]
    except (TypeError, ValueError):
        return None


_MEMO, _KEYLOCKS, _GUARD = {}, {}, __import__("threading").Lock()


def zones(video, log=print):
    """2026-10-18: cut asks for every CLIP - an episode whose scan had one failed check was never cached and was scanned
    again for each clip, by 8 threads and by both parallel builds at once (Mentalist O'Laughlin: 977 zone calls,
    ~$3.8). Now: one scan per episode - memo in this build, a lock per episode across builds, and the result is cached
    even when a check failed (those 16 s count as 'no text'; the catalogue's own text/safe flags still apply)."""
    key = os.path.basename(video)
    if key in _MEMO:
        return _MEMO[key]
    with _GUARD:
        kl = _KEYLOCKS.setdefault(key, __import__("threading").Lock())
    with kl:
        if key in _MEMO:
            return _MEMO[key]
        cache = json_read(CACHE, {})
        if key in cache:
            _MEMO[key] = cache[key]
            return cache[key]
        import hashlib, msvcrt
        lpth = os.path.join(os.path.dirname(CACHE), "zones_" + hashlib.md5(key.encode()).hexdigest()[:12] + ".lock")
        with open(lpth, "a+") as lk:                 # the other build scanning this episode: wait for its result
            for _ in range(1800):
                try:
                    msvcrt.locking(lk.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(1)
            try:
                cache = json_read(CACHE, {})
                if key in cache:
                    _MEMO[key] = cache[key]
                    return cache[key]
                iv = _scan(video, key, log)
                _MEMO[key] = iv
                return iv
            finally:
                try:
                    lk.seek(0)
                    msvcrt.locking(lk.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass


def _scan(video, key, log):
    dur = _dur(video)
    starts = list(range(0, min(300, int(dur)), 16)) + list(range(max(0, int(dur) - 90), max(1, int(dur) - 17), 16))
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(lambda s: _ask(video, s), starts))
    failed = sum(1 for r in res if r is None)
    hits = sorted(t for lst in res if lst for t in lst)
    # merge 1-fps hits into intervals (pad 0.6 s each side, join gaps < 2.5 s)
    iv = []
    for t in hits:
        a, b = t - 0.6, t + 1.6
        if iv and a - iv[-1][1] < 2.5:
            iv[-1][1] = b
        else:
            iv.append([a, b])
    if failed:
        log(f"  text zones {key[:50]}: {failed}/{len(starts)} checks failed - cached anyway (no re-billing per clip)")
    json_update(CACHE, lambda d: d.__setitem__(key, iv))
    log(f"  text zones {key[:50]}: {[(round(a), round(b)) for a, b in iv]}")
    return iv


if __name__ == "__main__":
    for v in sys.argv[1:]:
        print(zones(v))
