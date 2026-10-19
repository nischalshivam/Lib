"""A/B of the clue 'plan' call: rebuild clue_v2.json of a finished build in a scratch folder with another plan model
(EDITV2_MODEL_PLAN / EDITV2_THINK_PLAN), then compare its shot choices with the original.
usage: py tools/plan_ab.py "<build dir>" "<scratch dir>" """
import json, os, shutil, sys, re
sys.path.insert(0, r"D:\Lib")
src, dst = sys.argv[1], sys.argv[2]
os.makedirs(dst, exist_ok=True)
ctx = json.load(open(os.path.join(src, "ctx.json"), encoding="utf-8"))
ctx["build"] = os.path.abspath(dst)
json.dump(ctx, open(os.path.join(dst, "ctx.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
os.environ["EDITV2_BUILD"] = os.path.abspath(dst)
os.environ.setdefault("EDITV2_PROVIDER", "1")
from editv2 import clue
if not os.path.exists(os.path.join(dst, "clue_v2.json")):
    clue.build(ctx, log=lambda *a: None)
A = json.load(open(os.path.join(src, "clue_v2.json"), encoding="utf-8"))
B = json.load(open(os.path.join(dst, "clue_v2.json"), encoding="utf-8"))
def t(sid, shots):
    s = shots.get(sid) or {}
    return sid.rsplit("_", 1)[0], s.get("start")
def near(a, b, shots_a, shots_b, sec=20):
    ea, ta = t(a, shots_a); eb, tb = t(b, shots_b)
    return ea is not None and ea == eb and ta is not None and tb is not None and abs(ta - tb) <= sec
SA = {s["sid"]: s for s in A.get("shots", [])} if isinstance(A.get("shots"), list) else A.get("shots", {})
SB = {s["sid"]: s for s in B.get("shots", [])} if isinstance(B.get("shots"), list) else B.get("shots", {})
la, lb = {x.get("line"): x for x in A["lines"]}, {x.get("line"): x for x in B["lines"]}
n = same = close = cov_a = cov_b = 0
types = sum(1 for k in la if k in lb and la[k].get("type") == lb[k].get("type"))
for k in la:
    if k not in lb: continue
    a, b = la[k].get("shots") or [], lb[k].get("shots") or []
    cov_a += bool(a); cov_b += bool(b)
    if a and b:
        n += 1
        same += a[0] == b[0]
        close += any(near(x, y, SA, SB) for x in a for y in b)
m = len(la)
print(f"{os.path.basename(os.path.dirname(src))[:50]} | lines {m} | with shots: A {100*cov_a//m}% B {100*cov_b//m}% | "
      f"same first shot {100*same//max(1,n)}% | same moment (±20 s) {100*close//max(1,n)}% | same line type {100*types//m}% | "
      f"soundups A {sum(1 for x in la.values() if x.get('soundup'))} B {sum(1 for x in lb.values() if x.get('soundup'))}")
