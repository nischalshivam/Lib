"""Refine ONE library video outside the worker pool, the way libjobs/worker.py does it (lock, cast, refine, dialogue,
mark done, reset fail counters). Use after a code fix for a video that failed 3x, without restarting the 16 workers
(they imported the old code). Log: tools\\refine_one.log
    py tools\\refine_one.py "<video path>" """
import os, sys, time, traceback

sys.path.insert(0, r"D:\Lib")
sys.path.insert(0, r"D:\Lib\agy_rotation")
from libjobs import common as C

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "refine_one.log")


def log(m):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + m + "\n")


v = sys.argv[1]
lk = C.try_lock(v)
if not lk:
    log(f"locked by a worker right now - not touched: {v}")
    sys.exit(1)
t = time.time()
try:
    from media_index import catalog_refine, dialogue_fill
    st = C.state(v)
    log(f"[{st}] {v}")
    if st == "index":                                   # never catalogued: first pass (the worker's proxy video-pass)
        import proxy_videopass
        cast = C.cast_of(v)
        if len(cast) < 3:
            raise RuntimeError(f"no cast ({len(cast)} names) - add Desktop\\Cast\\<title>")
        proxy_videopass.build(v, os.path.splitext(v)[0] + ".catalog.json", cast, log=lambda *a: None)
        st = C.state(v)
        log(f"   indexed -> {st}")
    if st == "refine":
        r = catalog_refine.refine(v, C.cast_of(v), C.show_name(v), progress=lambda s: None)
        log(f"   refined: {r}")
        st = C.state(v)
    if st == "dialogue":
        log(f"   dialogue: {dialogue_fill.fill(v, progress=lambda s: None)}")
    if C.state(v) == "done":
        C.mark_done(v)
        C.reset_counters(v)
    log(f"   state now {C.state(v)} ({time.time() - t:.0f}s)")
except Exception:
    log("   FAILED: " + traceback.format_exc()[-1500:])
finally:
    try:
        C.unlock(lk)
    except Exception:
        pass
