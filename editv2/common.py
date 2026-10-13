"""Shared helpers: agy proxy calls (text + vision), long paths, JSON from model text."""
from __future__ import annotations

import base64, json, os, re, subprocess, time, urllib.request

PROXY = os.environ.get("MODEL_ROUTER_URL", "http://127.0.0.1:8317") + "/v1/chat/completions"
KEY = os.environ.get("MODEL_ROUTER_KEY") or open(os.path.expanduser("~/.model-router-proxy-key.txt")).read().strip()
TEXT_MODEL = os.environ.get("CLUE_MODEL", "claude-opus-4-6-thinking")      # agy Claude (NOT this session)
TEXT_FALLBACK = os.environ.get("CLUE_FALLBACK", "claude-sonnet-4-6")
def json_read(path, default=None):
    """a JSON file several builds share (editv2/cache): a reader that hits the moment of a write gets an empty or
    half file (B99 + the cross-show test crashed in cut, 2026-10-13) -> retry briefly, else the default."""
    for _ in range(10):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {} if default is None else default
        except (ValueError, OSError):
            time.sleep(0.3)
    return {} if default is None else default


def json_update(path, fn):
    """read-modify-write a shared JSON file under an OS lock, written atomically (tmp + os.replace); fn(data) edits
    the dict in place. Two builds can no longer overwrite each other's entries or leave a half-written file."""
    import msvcrt
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a+") as lk:
        for _ in range(600):                                  # up to ~60 s
            try:
                lk.seek(0)
                msvcrt.locking(lk.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                time.sleep(0.1)
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


# 2026-10-13: Google retired gemini-3.6-flash-high overnight ("unknown provider") and indexing + the editor stopped for
# 7 h. A chain now: the first model the proxy still knows is used; a model reported gone is skipped from then on.
# (3.8-flash-high is the backend 3.6 was already served by: 'served-by gemini-3.8-flash-n'.)
VISION_CHAIN = [m.strip() for m in os.environ.get("VERIFY_MODELS", "gemini-3.8-flash-high,gemini-3-flash,gemini-3.5-flash-lite").split(",") if m.strip()]
VISION_MODEL = os.environ.get("VERIFY_MODEL") or VISION_CHAIN[0]
_GONE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "gone_models.json")


def model_gone(code, text) -> bool:
    """True if the proxy says this MODEL no longer exists (not a quota / network / content problem)."""
    t = str(text or "").lower()
    return (code in (400, 404) and ("unknown provider" in t or "model_not_found" in t or "is not found" in t)) or \
        (code == 503 and "auth_unavailable" in t and "is not found for api version" in t)


def drop_vision_model(model=None):
    """mark the current vision model gone and switch VISION_MODEL to the next one in the chain (persisted)."""
    global VISION_MODEL
    stamp = time.strftime("%Y-%m-%d %H:%M")
    try:
        gone = json_update(_GONE_FILE, lambda d: d.__setitem__(model or VISION_MODEL, stamp))
    except OSError:
        gone = dict(_gone(), **{model or VISION_MODEL: stamp})
    nxt = next((m for m in VISION_CHAIN if m not in gone), None)
    if nxt:
        VISION_MODEL = nxt
    return nxt


def _gone():
    return json_read(_GONE_FILE, {})
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
os.makedirs(CACHE, exist_ok=True)
if not os.environ.get("VERIFY_MODEL"):            # start from the first chain model not already known to be gone
    VISION_MODEL = next((m for m in VISION_CHAIN if m not in _gone()), VISION_CHAIN[-1])
FACE_MODEL = os.path.join(r"D:\Lib\models", "face_detection_yunet_2023mar.onnx")


def lp(p: str) -> str:
    return "\\\\?\\" + p if p and len(p) >= 250 and not p.startswith("\\\\?\\") else p


def parse_json(txt: str):
    """First JSON object/array in a model reply (tolerates fences, prose, two objects)."""
    txt = re.sub(r"^```(?:json)?|```$", "", (txt or "").strip(), flags=re.M).strip()
    starts = [i for i in (txt.find("{"), txt.find("[")) if i >= 0]
    if not starts:
        raise ValueError("no JSON in reply")
    return json.JSONDecoder().raw_decode(txt[min(starts):])[0]


def _post(body: dict, timeout: int):
    req = urllib.request.Request(PROXY, json.dumps(body).encode(),
                                 {"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["choices"][0]["message"].get("content") or ""


def _proxy_down(e) -> bool:
    """the proxy / internet is down for everyone (not this request's fault): 503 auth_unavailable, 502/504, no route."""
    import urllib.error
    if isinstance(e, urllib.error.HTTPError):
        return e.code in (500, 502, 503, 504)          # 500: the proxy's upstream hiccup (B99 lost its punch-ins, 10-13)
    return isinstance(e, (urllib.error.URLError, ConnectionError, TimeoutError, OSError))


def wait_proxy(log=print, limit=1800):
    """2026-10-13: B99/House/TWD test builds died in 1 min because the proxy was stuck after a network drop. Wait (up
    to 30 min, probing every 60 s) - the libjobs supervisor restarts a stuck proxy within ~25 min."""
    t0, last = time.time(), 0.0
    while time.time() - t0 < limit:
        try:
            _post({"model": VISION_MODEL, "max_tokens": 5, "messages": [{"role": "user", "content": "ok"}]}, 60)
            if time.time() - t0 > 5:
                log(f"  model proxy back after {(time.time() - t0) / 60:.0f} min")
            return True
        except Exception:
            pass
        if time.time() - last > 300:
            log(f"  model proxy unavailable (internet / proxy) - waiting ({(time.time() - t0) / 60:.0f} of {limit // 60} min)")
            last = time.time()
        time.sleep(60)
    return False


def llm(system: str, user: str, max_tokens: int = 16000, tries: int = 4, temperature: float = 0.2):
    """agy Claude (text). Returns (parsed_json, model_used). A proxy/internet outage is waited out (wait_proxy),
    not counted as a try."""
    last, k, waited = None, 0, False
    while k < tries:
        model = TEXT_MODEL if k < tries - 1 else TEXT_FALLBACK
        try:
            txt = _post({"model": model, "temperature": temperature, "max_tokens": max_tokens,
                         "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}, 600)
            return parse_json(txt), model
        except Exception as e:
            last = e
            if _proxy_down(e) and not waited:
                waited = True
                if wait_proxy():
                    continue
            time.sleep(4 + 6 * k)
            k += 1
    raise RuntimeError(f"LLM failed after {tries} tries: {last}")


def vision(question: str, jpeg_bytes: bytes, tries: int = 3, timeout: int = 180):
    """Gemini (vision) on one image. Returns parsed JSON or None."""
    import urllib.error
    uri = "data:image/jpeg;base64," + base64.b64encode(jpeg_bytes).decode()
    k, waited = 0, False
    while k < tries:
        body = {"model": VISION_MODEL, "temperature": 0, "messages": [{"role": "user", "content": [
            {"type": "text", "text": question}, {"type": "image_url", "image_url": {"url": uri}}]}]}
        try:
            return parse_json(_post(body, timeout))
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace") if hasattr(e, "read") else ""
            if model_gone(e.code, txt) and drop_vision_model():
                continue                          # the model was retired: same request on the next one, not a try
            if _proxy_down(e) and not waited:     # an outage would otherwise mark every shot 'rejected'
                waited = True
                if wait_proxy():
                    continue
        except Exception as e:
            if _proxy_down(e) and not waited:
                waited = True
                if wait_proxy():
                    continue
        time.sleep(2 + 3 * k)
        k += 1
    return None


def frame_jpeg(video: str, t: float, width: int = 640) -> bytes:
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0, t):.2f}", "-i", lp(video), "-frames:v", "1",
                        "-vf", f"scale={width}:-2", "-f", "image2", "-c:v", "mjpeg", "-"], capture_output=True)
    return r.stdout


def duration(path: str) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", lp(path)],
                         capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def faces(video: str, t: float):
    """YuNet faces [[x,y,w,h],...] and (W,H) of the frame at t."""
    import cv2, numpy as np
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0, t):.2f}", "-i", lp(video), "-frames:v", "1",
                        "-f", "image2", "-c:v", "png", "-"], capture_output=True)
    a = cv2.imdecode(np.frombuffer(r.stdout, np.uint8), cv2.IMREAD_COLOR)
    if a is None:
        return [], (1920, 1080)
    det = cv2.FaceDetectorYN.create(FACE_MODEL, "", (a.shape[1], a.shape[0]), 0.75)
    _, f = det.detect(a)
    return ([] if f is None else [x[:4].tolist() for x in f]), (a.shape[1], a.shape[0])


def is_person(video: str, t: float, name: str, show: str):
    """Gemini identity check on one frame: is `name` clearly the main person?"""
    # "who" asks for the CHARACTER (+ actor separately): for a famous show Gemini answered the actor ('Jon Cryer'), so a
    # correct ALAN HARPER card failed the name match in look.label_on_person (2026-10-13)
    v = vision(f'Frame from "{show}". Is {name} clearly visible and the main person in this frame? Judge only by '
               'the face/appearance you see. Reply ONLY JSON {"yes": true|false, "who": "<CHARACTER name of the main '
               'person in this show, or unknown>", "actor": "<their actor, or unknown>"}',
               frame_jpeg(video, t))
    if not v:
        return False, "check-error"
    return bool(v.get("yes")), (str(v.get("who", "")) + " / " + str(v.get("actor", ""))).strip(" /")


def log_to(path: str):
    f = open(path, "a", encoding="utf-8")

    def log(*a):
        line = time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a)
        print(line, flush=True)
        f.write(line + "\n"); f.flush()
    return log


def mark_unusable(shots):
    """catalog_refine v4/v5 fields -> editor rule: a shot with nudity/sex, one Gemini refused to judge, or a black /
    dissolve transition piece is never shown (safe=False), even when it has no burned-in text."""
    for s in shots:
        if s.get("explicit") or s.get("nsfw") in ("nudity", "sex", "blocked") or s.get("blocked") or s.get("transition"):
            s["safe"] = False
    return shots
