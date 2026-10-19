"""All Mentalist QA warnings with exact times + which model verified + whether the cut predates the 2026-10-18 21:43 dark-shot fix."""
import glob, json, os, re, sys, datetime
base = sys.argv[1]
FIX = datetime.datetime(2026, 10, 18, 21, 43).timestamp()
mm = lambda t: f"{int(t//60)}:{int(t%60):02d}"
rows = []
for d in sorted(glob.glob(os.path.join(glob.escape(base), "*", "*_v2build"))):
    topic = os.path.basename(os.path.dirname(d))
    qa = os.path.join(d, "QA.txt")
    if not os.path.exists(qa): continue
    txt = open(qa, encoding="utf-8").read()
    verify = {}
    up = os.path.join(d, "api_usage.jsonl")
    if os.path.exists(up):
        for l in open(up, encoding="utf-8"):
            try: j = json.loads(l)
            except Exception: continue
            if j.get("kind") == "verify": verify[j.get("model")] = verify.get(j.get("model"), 0) + 1
    # state.json at render time: cut time = comp/look time is unreliable, use clue/plan file chain
    st = os.path.join(d, "state.json")
    cutt = os.path.getmtime(st) if os.path.exists(st) else 0
    warns = [l[6:] for l in txt.splitlines() if l.startswith("WARN: ")]
    rows.append((topic, txt.splitlines()[0], warns, verify, cutt < FIX))
for t, head, w, v, pre in rows:
    print("="*100); print(t); print(" ", head[:12], "| verify:", v, "| cut BEFORE dark-shot fix" if pre else "| cut after fix")
    for x in w: print("   WARN", x)
