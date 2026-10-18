"""Batch status + API spend per video for a channel folder (online done / video done / failed, verify model, list-price $).
usage: py tools/batch_status_api.py "<channel folder>" """
import collections, glob, json, os, sys
sys.path.insert(0, r"D:\Lib")
from editv2.common import _price

base = sys.argv[1]
finals = {os.path.splitext(f)[0].lower() for d in glob.glob(os.path.join(glob.escape(base), "*")) if os.path.basename(d).lower()
          in ("final videos", "final video") for f in os.listdir(d) if f.lower().endswith(".mp4")}
rows, tot = [], collections.Counter()
for topic in sorted(glob.glob(os.path.join(glob.escape(base), "*"))):
    name = os.path.basename(topic)
    if not os.path.isdir(topic) or name.lower() in ("final videos", "final video") or name.startswith("_"):
        continue
    b = next(iter(glob.glob(os.path.join(glob.escape(topic), "*_v2build"))), None)
    log = open(os.path.join(b, "build.log"), encoding="utf-8", errors="replace").read() if b and os.path.exists(os.path.join(b, "build.log")) else ""
    last = log[log.rfind("[") :] if log else ""
    if name.lower() in finals:
        st = "VIDEO DONE"
    elif not b or not log:
        st = "waiting"
    elif "FAILED:" in log[log.rfind("preflight"):]:
        st = "FAILED" + (" (budget)" if "BudgetExceeded" in log[log.rfind("preflight"):] else "")
    elif "ONLINE PHASE DONE" in log:
        st = "online done"
    else:
        st = "online running"
    usd, n, vm = 0.0, 0, collections.Counter()
    if b and os.path.exists(os.path.join(b, "api_usage.jsonl")):
        for line in open(os.path.join(b, "api_usage.jsonl"), encoding="utf-8"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            pi, po = _price(r.get("model", ""))
            usd += (r["in"] * pi + (r["out"] + (r.get("think") or 0)) * po) / 1e6
            n += 1
            if r["kind"] == "verify":
                vm["3.8" if "3.8" in r["model"] else "2.5" if "2.5" in r["model"] else r["model"]] += 1
    verify = "/".join(f"{k}:{v}" for k, v in sorted(vm.items())) or "-"
    rows.append((st, name, n, usd, verify))
    tot[st] += 1
    tot["usd"] += usd
    if vm:
        tot["verify on 2.5" if vm.get("2.5", 0) >= vm.get("3.8", 0) else "verify on 3.8"] += 1
for st, name, n, usd, verify in rows:
    print(f"{st:17} {name[:60]:60} {n:5} calls  ${usd:5.2f}  verify {verify}")
done = tot["VIDEO DONE"]
online = done + tot["online done"]
print(f"\nvideos: {len(rows)} | online work done: {online} | videos in Final Videos: {done} | online running: {tot['online running']}"
      f" | failed: {sum(v for k, v in tot.items() if k.startswith('FAILED'))} | waiting: {tot['waiting']}")
print(f"verify mostly on 3.8: {tot['verify on 3.8']} videos | on 2.5: {tot['verify on 2.5']} videos | API total ~${tot['usd']:.2f} (Google list price)")
