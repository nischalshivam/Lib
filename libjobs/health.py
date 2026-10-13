"""One-shot health + quality report of the library indexing (used by the periodic check).
    py health.py [hours_back=2]"""
import ast, collections, datetime as dt, glob, json, os, re, statistics as S, sys, time
import psutil
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

H = float(sys.argv[1]) if len(sys.argv) > 1 else 2.0
now = dt.datetime.now()
since = now - dt.timedelta(hours=H)
print(f"=== {now:%Y-%m-%d %H:%M}  (last {H:g} h)")
ws = [p for p in psutil.process_iter(["cmdline"]) if "worker.py" in " ".join(p.info["cmdline"] or [])
      and "python" in ((p.info["cmdline"] or [""])[0]).lower()]
print(f"worker processes: {len(ws)} (wanted {C.cfg().get('workers')})")
for f in sorted(glob.glob(os.path.join(C.HERE, "heartbeat_*.json"))):
    try:
        h = json.load(open(f))
    except Exception:
        continue
    age = time.time() - h["t"]
    print(f"  w{os.path.basename(f)[10:-5]:<2} {age:5.0f}s {'STALE' if age > 900 else 'ok   '} {h.get('stage', '')[:32]:32s} {os.path.basename(h.get('video', ''))[:60]}")
print(f"proxy {C.proxy_ok()}  drive {C.drive_ok()}  I: {os.path.isdir('I:/My Drive')}  J: {os.path.isdir('J:/')}  "
      f"alert file {os.path.exists(os.path.expanduser('~/Desktop/INDEXING ALERT.txt'))}")
sup = [ln.strip() for ln in open(os.path.join(C.LOGS, "supervisor.log"), encoding="utf-8", errors="replace")
       if ln[:19] >= f"{since:%Y-%m-%d %H:%M:%S}" and re.search(r"timed out|failed|error|STALL|Traceback", ln)]
print("supervisor issues:", sup[-5:] or "none")
oks, probs, rows = [], [], []
for f in glob.glob(os.path.join(C.LOGS, "worker_*.log")):
    cur = None
    for ln in open(f, encoding="utf-8", errors="replace"):
        m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (.*)", ln)
        if not m:
            continue
        t = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        e = m.group(2)
        mm = re.match(r"\[(\w+)\] (.*)", e)
        if mm:
            cur = mm.group(2)
        if t < since:
            continue
        if e.strip().startswith("ok ("):
            oks.append((t, cur))
        if re.search(r"FAILED|BAD SOURCE|STALL|parked|not counted|temporary", e):
            probs.append((t, os.path.basename(cur or ""), e.strip()[:150]))
        r = re.search(r"refined: (\{.*\})", e)
        if r:
            try:
                d = ast.literal_eval(r.group(1))
                d["_v"] = cur
                rows.append(d)
            except Exception:
                pass
print(f"finished: {len(oks)} in {H:g} h -> {len(oks) / H:.1f}/h")
print("by show:", dict(collections.Counter(C.show_name(v) for t, v in oks if v)))
print(f"problems: {len(probs)}")
for p in probs[-12:]:
    print(f"  {p[0]:%H:%M} {p[1][:50]} | {p[2]}")
if rows:
    def med(k):
        return round(S.median([r.get(k, 0) or 0 for r in rows]), 3)
    print(f"quality ({len(rows)} refined): unverified max {max(r.get('unverified', 0) for r in rows)}  blocked max "
          f"{max(r.get('blocked', 0) for r in rows)}  dialogue median {med('dialogue')} min {min(r.get('dialogue', 0) for r in rows)}"
          f"  job median {med('total_s')}s  read median {med('read_s')}s")
    print("  dialogue sources:", dict(collections.Counter(str(r.get("dialogue_source", "")).split(":")[0][:14] for r in rows)))
    print("  cast scope:", dict(collections.Counter(str(r.get("cast_scope", "")) for r in rows)))
    for r in rows:
        fl = []
        if r.get("unverified", 0):
            fl.append(f"unverified {r['unverified']}")
        if r.get("blocked", 0) > 5:
            fl.append(f"blocked {r['blocked']}")
        if r.get("dialogue", 1) < 0.4:
            fl.append(f"dialogue {r['dialogue']}")
        if r.get("allowed_cast", 9) < 3:
            fl.append(f"cast {r.get('allowed_cast')}")
        if r.get("transitions", 0) > 30:
            fl.append(f"transitions {r['transitions']}")
        if fl:
            print("  OUTLIER", os.path.basename(r["_v"] or "")[:60], fl)
q = json.load(open(C.QUEUE, encoding="utf-8"))
tot, done = collections.Counter(), collections.Counter()
for v in q:
    s = C.show_name(v)
    tot[s] += 1
    if C.is_done(v):
        done[s] += 1
for s in ["House", "Brooklyn Nine-Nine", "Reacher", "Two and a Half Men"] + [C.show_name(v) for t, v in oks[-3:] if v]:
    if s in tot:
        print(f"  {s}: {done[s]}/{tot[s]}")
print(f"library done {sum(done.values())}/{len(q)}")
