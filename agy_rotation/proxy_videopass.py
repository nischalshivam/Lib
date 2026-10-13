"""Video-pass cataloguer via the CLIProxyAPI proxy (round-robin across all
Gemini accounts, raw-API, true parallel). Sends each low-fps video chunk INLINE
(base64 video data-URI) — no per-shot frame extraction, no agy overhead. This is
the fast path: ~7s/chunk raw-API, many chunks concurrent across accounts.

Output matches the tool's catalog.json format; dialogue anchored from subtitles.
Usage: proxy_videopass.py <video> <out_json> [cast_csv]
"""
import base64, json, os, re, subprocess, sys, tempfile, time, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.insert(0, r"D:\Lib")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from media_index import subtitles, probe, catalog, naming

PROXY = os.environ.get("MODEL_ROUTER_URL", "http://127.0.0.1:8317") + "/v1/chat/completions"
KEY = os.environ.get("MODEL_ROUTER_KEY") or \
    open(os.path.expanduser("~/.model-router-proxy-key.txt")).read().strip()
MODEL = os.environ.get("VP_MODEL", "gemini-3.6-flash-high")
CHUNK_S = 180
OVERLAP = 6
FPS = 8
SCALE = 448
WORKERS = int(os.environ.get("VP_WORKERS", "4"))   # 16 x 9 indexing workers = ~144 calls at once -> most chunks failed (Andor, 10-13)

RULES = ("You label footage for a searchable clip library by WATCHING a short "
         "video. Break it into shots (a new shot at each camera cut) and describe "
         "only what is visibly there. Reply ONLY with a strict JSON array, one "
         "object per shot, in time order:\n"
         '[{"start": <sec from clip start>, "end": <sec>, '
         '"characters": ["<named cast member actually visible>"], '
         '"description": "<one vivid line: who + what + setting>", '
         '"action": "<what happens, few words>", '
         '"shot_type": "<wide|medium|close-up|insert|aerial>", '
         '"safe": <false if burned-in subtitles/captions/on-screen text/credits/'
         'logo, else true>}]\n'
         "Rules: characters = only people actually on screen; use the exact cast "
         "names given, anyone else or unsure is \"unknown\". A new object at every "
         "cut. No prose, no code fences — JSON array only.")


def proxy_call(clip_bytes, cast, timeout=300):
    uri = "data:video/mp4;base64," + base64.b64encode(clip_bytes).decode()
    ask = f"Watch the video and list its shots.\nKnown cast (exact spellings): {cast}"
    msgs = [{"role": "system", "content": RULES},
            {"role": "user", "content": [
                {"type": "text", "text": ask},
                {"type": "image_url", "image_url": {"url": uri}}]}]
    # 2026-10-13: the hard-coded gemini-3.6-flash-high was retired -> every chunk of every NEW show (Andor) failed
    # ("17/17 chunks failed"). Use editv2's model chain, which skips models the proxy reports as gone.
    import urllib.error
    from editv2 import common as E
    for _ in range(len(E.VISION_CHAIN) + 1):
        model = os.environ.get("VP_MODEL") or E.VISION_MODEL
        body = json.dumps({"model": model, "messages": msgs, "temperature": 0}).encode()
        req = urllib.request.Request(PROXY, data=body, method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            return d["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")
            if not os.environ.get("VP_MODEL") and E.model_gone(e.code, txt) and E.drop_vision_model(model):
                continue
            raise


def extract_chunk(video, start, dur, out):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}", "-t",
                    f"{dur:.2f}", "-i", video, "-vf", f"scale={SCALE}:-2,fps={FPS}",
                    "-an", "-c:v", "libx264", "-preset", "veryfast", out],
                   check=True, timeout=240)


def _one_call(video, cstart, dur, cast, tmproot):
    clip = os.path.join(tmproot, f"c_{int(cstart)}_{os.getpid()}.mp4")
    try:
        extract_chunk(video, cstart, dur, clip)
        data = open(clip, "rb").read()
        txt = proxy_call(data, cast)
    finally:
        try:
            os.remove(clip)
        except OSError:
            pass
    a, b = txt.find("["), txt.rfind("]")
    if a < 0 or b <= a:
        raise ValueError("no JSON array in response")
    return json.loads(txt[a:b + 1])


def catalog_chunk(video, cstart, dur, cast, tmproot):
    # A chunk that yields nothing is almost always a transient proxy/limit hiccup,
    # not a genuinely empty 3-min stretch of TV — retry a few times before giving up
    # so we never silently write an empty catalog.
    shots = None
    for attempt in range(4):
        try:
            shots = _one_call(video, cstart, dur, cast, tmproot)
            if shots:
                break
        except Exception:
            shots = None
        time.sleep(2 * (attempt + 1))
    if not shots:
        raise RuntimeError(f"chunk @{cstart:.0f}s produced no shots after retries")
    out = []
    for s in shots:
        try:
            st = cstart + float(s.get("start", 0)); en = cstart + float(s.get("end", s.get("start", 0) + 2))
        except (TypeError, ValueError):
            continue
        out.append({"start": round(st, 2), "end": round(en, 2),
                    "characters": [c for c in (s.get("characters") or []) if c and c.lower() != "unknown"],
                    "description": str(s.get("description") or ""), "action": str(s.get("action") or ""),
                    "shot_type": str(s.get("shot_type") or ""), "safe": bool(s.get("safe", True))})
    return out


def _lp(p):
    """Windows extended-length path (\\\\?\\...) for paths over the 260-char
    MAX_PATH limit — without it ffmpeg/os.open raise FileNotFoundError even though
    the file exists (long release-folder names on Google-Drive shows). Short paths
    are returned as a normal absolute path (unchanged)."""
    if os.name != "nt" or not p:
        return p
    ap = os.path.abspath(p)
    if len(ap) >= 255 and not ap.startswith("\\\\?\\"):
        return "\\\\?\\" + ap
    return ap


def build(video, out_json, cast_names, log=print):
    video = _lp(video)                     # long-path safe for every file op below
    out_json = _lp(out_json)
    dur = probe.probe(video).duration
    _k, _s, cues = subtitles.load_for_video(video)
    cast = ", ".join(cast_names)
    chunks = []; t = 0.0
    while t < dur:
        chunks.append((t, min(CHUNK_S + OVERLAP, dur - t))); t += CHUNK_S
    log(f"proxy video-pass: {dur:.0f}s -> {len(chunks)} chunks, {len(cues)} subs, workers={WORKERS}, model={os.environ.get("VP_MODEL") or __import__("editv2.common", fromlist=["x"]).VISION_MODEL}")
    tmproot = tempfile.mkdtemp(prefix="pvp_")
    t0 = time.time(); results = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(catalog_chunk, video, cs, d, cast, tmproot): cs for cs, d in chunks}
        done = 0
        for f in as_completed(futs):
            cs = futs[f]
            try:
                results[cs] = f.result()
            except Exception as exc:
                log(f"  chunk @{cs:.0f}s failed: {exc}"); results[cs] = None
            done += 1
            if done % 3 == 0 or done == len(chunks):
                log(f"  {done}/{len(chunks)} chunks")
    try:
        os.rmdir(tmproot)
    except OSError:
        pass
    for cs, d in chunks:          # second chance, one at a time and patient: a burst / rate-limit failure is not the file
        if results.get(cs) is None:
            os.makedirs(tmproot, exist_ok=True)       # removed just above
            for wait in (20, 60, 120):
                time.sleep(wait)
                try:
                    results[cs] = catalog_chunk(video, cs, d, cast, tmproot)
                    log(f"  chunk @{cs:.0f}s ok on retry")
                    break
                except Exception as exc:
                    log(f"  chunk @{cs:.0f}s retry failed: {exc}")
    bad = [cs for cs, r in results.items() if r is None]
    if bad:     # never a catalogue with a hole in it (Wi-Fi / proxy drop) -> fail, the queue retries later
        raise RuntimeError(f"{len(bad)}/{len(chunks)} chunks failed (e.g. @{bad[0]:.0f}s) - catalogue NOT written")
    allshots = [s for cs in sorted(results) for s in results[cs]]
    allshots.sort(key=lambda s: s["start"])
    merged = []
    for s in allshots:
        if merged and s["start"] - merged[-1]["start"] < 1.2:
            if len(s["characters"]) > len(merged[-1]["characters"]):
                merged[-1] = s
            continue
        merged.append(s)
    for i in range(len(merged) - 1):
        merged[i]["end"] = merged[i + 1]["start"]
    if merged:
        merged[-1]["end"] = max(merged[-1]["end"], merged[-1]["start"] + 2.0)
    slug = re.sub(r"[^a-z0-9]+", "", os.path.basename(video).lower())[:24]
    src_label = naming.parse(video).label; fname = video  # full (long-path-safe) path so the builder can open the source
    shots_json = []
    for i, s in enumerate(merged):
        dlg = catalog.dialogue_for(cues, s["start"], s["end"]) if cues else ""
        shots_json.append({"id": f"{slug}__{i:05d}", "source": src_label, "file": fname,
            "start": s["start"], "end": s["end"], "dialogue": dlg, "description": s["description"],
            "tags": [], "characters": s["characters"], "action": s["action"],
            "shot_type": s["shot_type"], "quality": "high", "safe": s["safe"], "explicit": False})
    if not shots_json:
        # every chunk came back empty = proxy/limit outage, NOT a real result.
        # Do not write a bogus "complete" catalog; fail so the runner retries later.
        raise RuntimeError(f"{fname}: 0 shots from all {len(chunks)} chunks — not writing empty catalog")
    tmp = out_json + ".tmp"                       # atomic: a half-written catalogue can never be left behind
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"shots": shots_json, "windows": len(shots_json), "complete": True}, fh, ensure_ascii=False)
    os.replace(tmp, out_json)
    log(f"proxy video-pass DONE: {len(shots_json)} shots, "
        f"{sum(1 for s in shots_json if s['characters'])} with-chars, "
        f"{sum(1 for s in shots_json if s['dialogue'])} dialogue, {(time.time()-t0)/60:.1f} min")
    return shots_json


if __name__ == "__main__":
    video, out_json = sys.argv[1], sys.argv[2]
    cast = sys.argv[3].split(",") if len(sys.argv) > 3 else []
    build(video, out_json, cast)
