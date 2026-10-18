r"""Watch every <topic>\*_v2build\build.log under a channel folder and print one line per event worth knowing
(clue done, refs warnings, verify result, online done, QA, failures, budget stops). For a Monitor.
usage: py -u tools/watch_builds.py "<channel folder>" """
import glob, os, re, sys, time

base = sys.argv[1]
KEEP = re.compile(r"clue done|refs check|refs WARNING|verify: rejected|ONLINE PHASE DONE|QA (PASS|WARN)|FAILED|Traceback|"
                  r"BudgetExceeded|API spend|stalled|Error|error:|HTTP 429|provider busy|DONE ", re.I)
pos = {}
for f in glob.glob(os.path.join(glob.escape(base), "*", "*_v2build", "build.log")):
    pos[f] = os.path.getsize(f)
last_any = time.time()
while True:
    time.sleep(45)
    for f in glob.glob(os.path.join(glob.escape(base), "*", "*_v2build", "build.log")):
        try:
            sz = os.path.getsize(f)
        except OSError:
            continue
        p = pos.get(f, 0)
        if sz <= p:
            continue
        with open(f, encoding="utf-8", errors="replace") as fh:
            fh.seek(p)
            new = fh.read()
        pos[f] = sz
        last_any = time.time()
        name = os.path.basename(os.path.dirname(os.path.dirname(f)))[:42]
        for line in new.splitlines():
            if KEEP.search(line) and "clue_v2.json exists" not in line:
                print(f"[{name}] {line.strip()[:230]}", flush=True)
    if time.time() - last_any > 3600:
        print("NOTHING written to any build.log for 60 min - batch stalled or stopped?", flush=True)
        last_any = time.time()
