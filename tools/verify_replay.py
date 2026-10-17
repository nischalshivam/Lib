"""Replay saved plan-verify verdicts (agy gemini-3.8-flash-high, the production baseline) with other models through
the paid provider: same grid, same prompt (editv2.plan.Plan.reanchor). Saves every new verdict + its grid for judging.
usage: py tools/verify_replay.py <out_dir> <n_per_build> <model> [<model> ...]   (builds: archived Reacher 01, 02)"""
import concurrent.futures as cf, glob, json, os, random, sys
os.environ["EDITV2_PROVIDER"] = "1"
sys.path.insert(0, r"D:\Lib")
from editv2 import common as C
from editv2.plan import Plan

out, n_per, models = sys.argv[1], int(sys.argv[2]), sys.argv[3:]
os.makedirs(os.path.join(out, "grids"), exist_ok=True)
BUILDS = sorted(glob.glob(r"D:\Movies\_done_builds\Reacher\0[12] *\Script_v2build"))
items = []
for b in BUILDS:
    ctx = json.load(open(os.path.join(b, "ctx.json"), encoding="utf-8"))
    ctx["build"] = b
    P = Plan(ctx)
    cache = json.load(open(os.path.join(b, "verify_cache.json"), encoding="utf-8"))
    keys = [k for k in cache if k.split("|", 1)[0] in P.SH]
    rej = [k for k in keys if cache[k].get("score", 0) < 5 or not cache[k].get("pick")]
    acc = [k for k in keys if k not in rej]
    rnd = random.Random(b)
    pick = rnd.sample(rej, min(len(rej), n_per // 3)) + rnd.sample(acc, min(len(acc), n_per - min(len(rej), n_per // 3)))
    items += [(P, k, cache[k]) for k in pick]
print(f"{len(items)} shots from {len(BUILDS)} builds; models {models}", flush=True)
res = {}
for m in models:
    C.VISION_MODEL = m

    def one(it):
        P, k, old = it
        sid, line = k.split("|", 1)
        v = P.reanchor(sid, line)
        return k, v
    with cf.ThreadPoolExecutor(8) as ex:
        res[m] = dict(ex.map(one, items))
    print(f"{m}: done", flush=True)
for P, k, old in items:                        # the grid each verdict was made on (for a blind judge)
    sid = k.split("|", 1)[0]
    g, ts = P._grid(sid)
    gp = os.path.join(out, "grids", f"{abs(hash(k)) % 10**10}.jpg")
    open(gp, "wb").write(g)
    res.setdefault("_grid", {})[k] = gp
    res.setdefault("_old", {})[k] = old
json.dump(res, open(os.path.join(out, "replay.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def rejected(v):
    return v.get("score", 0) < 5 or not v.get("pick")


for m in models:
    a = r = p1 = n = 0
    for P, k, old in items:
        v = res[m][k]
        n += 1
        same = rejected(v) == rejected(old)
        a += same
        if same and not rejected(v):
            p1 += abs(int(v.get("pick") or 0) - int(old.get("pick") or 0)) <= 1
            r += 1
    print(f"{m}: accept/reject agrees with agy-3.8 {a}/{n} ({100 * a // n}%) | both accept: pick within 1 frame {p1}/{r}")
