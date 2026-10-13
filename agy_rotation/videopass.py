"""Video-pass cataloguer: instead of cut-detect + per-shot frame calls, send the
episode to Gemini (via the rotating shim) in short low-fps video chunks and get
a structured shot list back. ~17 calls/episode vs ~800. Dialogue is anchored
from subtitles. Output matches the tool's catalog.json format.

Usage: videopass.py <video> <out_json> [cast_csv]
"""
import json, os, re, subprocess, sys, tempfile, time, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.insert(0, r"D:\Lib")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from media_index import subtitles, probe, catalog

SHIM = "http://127.0.0.1:8788/v1/chat/completions"
CHUNK_S = 180          # 3-minute chunks
OVERLAP = 6            # small overlap so a shot on the seam isn't lost
FPS = 8                # 8 fps catches fast cuts + brief character appearances
SCALE = 448
WORKERS = 8
MODEL = "gemini-3.6-flash-low"

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


def shim_call(messages, timeout=600):
    payload = json.dumps({"model": MODEL, "messages": messages}).encode()
    req = urllib.request.Request(SHIM, data=payload, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    return d["choices"][0]["message"]["content"]


def extract_chunk(video, start, dur, out):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}", "-t",
                    f"{dur:.2f}", "-i", video, "-vf", f"scale={SCALE}:-2,fps={FPS}",
                    "-an", "-c:v", "libx264", "-preset", "veryfast", out],
                   check=True, timeout=240)


def catalog_chunk(video, cstart, dur, cast, tmproot):
    clip = os.path.join(tmproot, f"c_{int(cstart)}.mp4")
    try:
        extract_chunk(video, cstart, dur, clip)
        ask = f"Watch the video and list its shots.\nKnown cast (use these exact " \
              f"spellings when you recognise them): {cast}"
        msgs = [{"role": "system", "content": RULES},
                {"role": "user", "content": [
                    {"type": "text", "text": ask},
                    {"type": "video_path", "path": clip}]}]
        txt = shim_call(msgs)
    finally:
        try:
            os.remove(clip)
        except OSError:
            pass
    a, b = txt.find("["), txt.rfind("]")
    if a < 0 or b <= a:
        return []
    try:
        shots = json.loads(txt[a:b + 1])
    except ValueError:
        return []
    out = []
    for s in shots:
        try:
            st = cstart + float(s.get("start", 0))
            en = cstart + float(s.get("end", s.get("start", 0) + 2))
        except (TypeError, ValueError):
            continue
        out.append({"start": round(st, 2), "end": round(en, 2),
                    "characters": [c for c in (s.get("characters") or [])
                                   if c and c.lower() != "unknown"],
                    "description": str(s.get("description") or ""),
                    "action": str(s.get("action") or ""),
                    "shot_type": str(s.get("shot_type") or ""),
                    "safe": bool(s.get("safe", True))})
    return out


def build(video, out_json, cast_names, log=print):
    dur = probe.probe(video).duration
    _k, _s, cues = subtitles.load_for_video(video)
    cast = ", ".join(cast_names)
    chunks = []
    t = 0.0
    while t < dur:
        chunks.append((t, min(CHUNK_S + OVERLAP, dur - t)))
        t += CHUNK_S
    log(f"video-pass: {dur:.0f}s -> {len(chunks)} chunks, {len(cues)} subs, workers={WORKERS}")
    tmproot = tempfile.mkdtemp(prefix="vp_")
    t0 = time.time()
    results = {}
    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futs = {pool.submit(catalog_chunk, video, cs, d, cast, tmproot): cs
                    for cs, d in chunks}
            done = 0
            for f in as_completed(futs):
                cs = futs[f]
                try:
                    results[cs] = f.result()
                except Exception as exc:
                    log(f"  chunk @{cs:.0f}s failed: {exc}"); results[cs] = []
                done += 1
                if done % 3 == 0 or done == len(chunks):
                    log(f"  {done}/{len(chunks)} chunks")
    finally:
        try:
            os.rmdir(tmproot)
        except OSError:
            pass
    # merge in time order, drop overlap-seam duplicates (same-ish start)
    allshots = [s for cs in sorted(results) for s in results[cs]]
    allshots.sort(key=lambda s: s["start"])
    merged = []
    for s in allshots:
        if merged and s["start"] - merged[-1]["start"] < 1.2:
            # near-duplicate on a chunk seam: keep the one with MORE characters
            if len(s["characters"]) > len(merged[-1]["characters"]):
                merged[-1] = s
            continue
        merged.append(s)
    # make shots contiguous (end = next start) so every shot spans its real time
    # window — this is what lets dialogue_for find the subtitle line in it.
    for i in range(len(merged) - 1):
        merged[i]["end"] = merged[i + 1]["start"]
    if merged:
        merged[-1]["end"] = max(merged[-1]["end"], merged[-1]["start"] + 2.0)
    # build tool-format shots + anchor dialogue from subtitles
    slug = re.sub(r"[^a-z0-9]+", "", os.path.basename(video).lower())[:24]
    source = catalog.__dict__.get("naming") and None
    from media_index import naming
    src_label = naming.parse(video).label
    fname = os.path.basename(video)
    shots_json = []
    for i, s in enumerate(merged):
        dlg = catalog.dialogue_for(cues, s["start"], s["end"]) if cues else ""
        shots_json.append({
            "id": f"{slug}__{i:05d}", "source": src_label, "file": fname,
            "start": s["start"], "end": s["end"], "dialogue": dlg,
            "description": s["description"], "tags": [],
            "characters": s["characters"], "action": s["action"],
            "shot_type": s["shot_type"], "quality": "high",
            "safe": s["safe"], "explicit": False})
    out = {"shots": shots_json, "windows": len(shots_json), "complete": True}
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    log(f"video-pass DONE: {len(shots_json)} shots, {sum(1 for s in shots_json if s['characters'])} with-chars, "
        f"{sum(1 for s in shots_json if s['dialogue'])} dialogue, {(time.time()-t0)/60:.1f} min")
    return out


if __name__ == "__main__":
    video, out_json = sys.argv[1], sys.argv[2]
    cast = sys.argv[3].split(",") if len(sys.argv) > 3 else []
    build(video, out_json, cast)
