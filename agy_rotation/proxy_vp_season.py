"""Index ALL pending episodes of a show via proxy video-pass (parallel across all
proxy accounts). Writes catalog beside each video. Resumable + robust.
Usage: proxy_vp_season.py "<show root>" ["<cast dir>"]
"""
import glob, os, re, sys, time
sys.path.insert(0, r"D:\Lib\agy_rotation")
sys.path.insert(0, r"D:\Lib")
import proxy_videopass
from media_index import catalog

ROOT = sys.argv[1]
CAST_DIR = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "Cast")
if os.path.isdir(CAST_DIR):
    CAST = sorted(d for d in os.listdir(CAST_DIR)
                  if os.path.isdir(os.path.join(CAST_DIR, d)))
else:                                  # no Cast folder -> explicit list via env
    CAST = [c.strip() for c in os.environ.get("VP_CAST", "").split(",") if c.strip()]


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


vids = {}
for f in glob.glob(os.path.join(ROOT, "**", "*.*"), recursive=True):
    if os.sep + "Cast" + os.sep in f:
        continue
    if f.lower().endswith((".mkv", ".mp4", ".avi")) and re.search(r"S\d\dE\d\d", os.path.basename(f), re.I):
        tag = re.search(r"S\d\dE\d\d", os.path.basename(f), re.I).group(0).upper()
        vids[tag] = f

from proxy_videopass import _lp
pending = sorted(t for t, v in vids.items()
                 if not catalog._is_complete(_lp(os.path.splitext(v)[0] + ".catalog.json")))
log(f"{os.path.basename(ROOT)}: {len(vids)} episodes, {len(pending)} pending, cast={len(CAST)}")
season_t0 = time.time()
ok = 0
failed = []
for n, tag in enumerate(pending, 1):
    v = vids[tag]
    out = os.path.splitext(v)[0] + ".catalog.json"
    log(f"[{n}/{len(pending)}] {tag}")
    try:
        proxy_videopass.build(v, out, CAST, log=lambda *a: None)
        ok += 1
    except Exception as exc:
        log(f"[{n}/{len(pending)}] {tag} FAILED: {type(exc).__name__}: {exc}")
        failed.append(tag)
    if n % 5 == 0 or n == len(pending):
        el = (time.time() - season_t0) / 60
        log(f"  --- {ok}/{n} done, {el:.0f} min elapsed, ~{el/max(1,n):.1f} min/ep, "
            f"ETA {el/max(1,n)*(len(pending)-n):.0f} min left ---")
# retry pass
for tag in failed:
    v = vids[tag]; out = os.path.splitext(v)[0] + ".catalog.json"
    if catalog._is_complete(_lp(out)):
        continue
    try:
        log(f"[retry] {tag}")
        proxy_videopass.build(v, out, CAST, log=lambda *a: None); ok += 1
    except Exception as exc:
        log(f"[retry] {tag} STILL FAILED: {exc}")
log("=" * 55)
log(f"SEASON DONE: {ok}/{len(pending)} indexed in {(time.time()-season_t0)/60:.1f} min")
