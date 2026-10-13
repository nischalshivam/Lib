"""Cross-show test: Malcolm in the Middle vs Young Sheldon script through ctx -> clue -> words -> plan -> cut (no render),
then a line-by-line audit: every shot under a line that names ONE show's characters must come from that show."""
import json, os, sys, time
sys.path.insert(0, r"D:\Lib")
from editv2 import ctx as _ctx, clue as _clue, pipeline as P
from editv2.plan import Plan
from editv2.cut import Cutter
d = r"D:\Lib\tools\_xshow\MITM vs Young Sheldon"
clean, B = d + r"\Script.txt", d + r"\Script_v2build"
log = print
ctx = _ctx.build(clean, B, "en", "auto", None, log=log)
print("shows:", [s["name"] for s in ctx["shows"]])
_clue.build(ctx, log)
P.words(B, "en", log)
if not os.path.exists(os.path.join(B, "plan.json")):
    Plan(ctx).run(log)
if not os.path.exists(os.path.join(B, "state.json")):
    Cutter(ctx, log).run()
pl, st = Plan(ctx), json.load(open(os.path.join(B, "state.json"), encoding="utf-8"))
bad, owned, total = [], 0, 0
for it in st["items"]:
    sid = it.get("sid") or (it.get("su") or {}).get("sid")
    line = it.get("line", (it.get("su") or {}).get("line"))
    if sid not in pl.SH or line is None:
        continue
    total += 1
    own = pl.line_owner(line)
    if own:
        owned += 1
        if pl.SH[sid]["ep"][0] != own:
            bad.append((round(it["o0"], 1), own, pl.SH[sid]["ep"][0], pl.LINES[line]["text"][:70]))
print(f"\nAUDIT: {total} shots, {owned} under a one-show line, MISMATCHES {len(bad)}")
for b in bad[:20]:
    print("  ", b)
