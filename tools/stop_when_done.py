"""User 2026-10-13: finish only the priority shows (libjobs/only.json), then STOP indexing so the PC is free for the
Movie Editor. Checks every 5 min; when every queue item is done (or failed out as a bad source) it sets
config.json workers=0 (the supervisor then starts none) and ends the worker processes. Run outside Claude."""
import json, os, sys, time
sys.path.insert(0, r"D:\Lib\libjobs")
import common as C
import psutil

STATUS = r"D:\Lib\tools\indexing_stop.txt"


def note(msg):
    with open(STATUS, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M ") + msg + "\n")


note("watcher started (priority: " + ", ".join(json.load(open(os.path.join(C.HERE, "only.json"), encoding="utf-8"))) + ")")
while True:
    q = json.load(open(C.QUEUE, encoding="utf-8"))
    left = [v for v in q if not C.is_done(v) and C.fails(v)["n"] < C.MAX_FAILS]
    if not left:
        break
    note(f"{len(left)} priority videos left")
    time.sleep(300)
cfg = os.path.join(C.HERE, "config.json")
json.dump({"workers": 0}, open(cfg, "w"))
n = 0
for p in psutil.process_iter(["cmdline"]):
    c = " ".join(p.info["cmdline"] or [])
    if "worker.py" in c:
        try:
            for k in p.children(recursive=True):
                k.kill()
            p.kill()
            n += 1
        except Exception:
            pass
bad = [v for v in q if not C.is_done(v)]
note(f"PRIORITY DONE - indexing stopped ({n} workers ended, config workers=0). Not done (bad source): {len(bad)}")
