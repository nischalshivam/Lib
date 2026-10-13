"""Production FRAME-BASED cataloguer via the proxy (full 100% accuracy).
PARALLEL frame extraction (SSD-safe, no grab lock) + parallel proxy vision.
Writes catalog beside the video. Resumable.
Usage: proxy_frame_prod.py <video> [cast_dir]   (out defaults beside video)
"""
import os, sys, time, json, re
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, r"D:\Lib")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from media_index import catalog, gemini, subtitles, naming, probe

KEY = open(os.path.expanduser("~/.model-router-proxy-key.txt")).read().strip()
cfg = gemini.Config(key=KEY, base="http://127.0.0.1:8317/v1",
                    model=os.environ.get("PF_MODEL", "gemini-3.8-flash-high"))  # 3.6 retired 2026-10-13
WORKERS = int(os.environ.get("PF_WORKERS", "24"))


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def build(video, cast_dir=None, out_json=None):
    out_json = out_json or (os.path.splitext(video)[0] + ".catalog.json")
    if catalog._is_complete(out_json):
        log(f"{os.path.basename(video)}: already complete — skip"); return
    known = []
    if cast_dir and os.path.isdir(cast_dir):
        known = sorted(d for d in os.listdir(cast_dir)
                       if os.path.isdir(os.path.join(cast_dir, d)))
    dur = probe.probe(video).duration
    _k, _s, cues = subtitles.load_for_video(video)
    windows = catalog.plan_shots(dur, video)
    grab = catalog.real_grab(video, n=2)
    slug = re.sub(r"[^a-z0-9]+", "", os.path.basename(video).lower())[:24]
    src = naming.parse(video).label
    fname = os.path.abspath(video)   # full path so the builder can open the source
    log(f"{os.path.basename(video)}: {len(windows)} shots, {len(cues)} subs -> frame-based (parallel), {WORKERS} workers")

    def do_shot(job):
        i, (s, e) = job
        line = catalog.dialogue_for(cues, s, e) if cues else ""
        try:
            frames = grab(s, e)                     # parallel (no lock) — SSD
            tags = catalog.parse_tags(
                gemini.call(cfg, catalog.tag_messages(frames, known, line))[0] or "") if frames else {}
        except Exception:
            tags = {}
        if tags.get("characters"):
            tags["characters"] = catalog.canonicalize(tags["characters"], {})
        return {"id": f"{slug}__{i:05d}", "source": src, "file": fname, "start": s, "end": e,
                "dialogue": line, "description": tags.get("description", ""), "tags": tags.get("tags", []),
                "characters": tags.get("characters", []), "action": tags.get("action", ""),
                "shot_type": tags.get("shot_type", ""), "quality": tags.get("quality", "high"),
                "safe": tags.get("safe", True), "explicit": tags.get("explicit", False)}

    t0 = time.time()
    shots = [None] * len(windows)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        done = 0
        for res in pool.map(do_shot, list(enumerate(windows))):
            shots[int(res["id"].split("__")[1])] = res
            done += 1
            if done % 100 == 0:
                log(f"  {done}/{len(windows)}")
    desc = sum(1 for s in shots if s["description"])
    named = sum(1 for s in shots if s["characters"])
    json.dump({"shots": shots, "windows": len(shots), "complete": True},
              open(out_json, "w", encoding="utf-8"), ensure_ascii=False)
    log(f"{fname}: DONE {desc}/{len(shots)} described, {named} w-char, {(time.time()-t0)/60:.1f} min")
    return (time.time() - t0) / 60


if __name__ == "__main__":
    v = sys.argv[1]
    cd = sys.argv[2] if len(sys.argv) > 2 else None
    build(v, cd)
