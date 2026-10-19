"""How good is grounding WITHOUT a quote? On refs that have a quote: ground with the quote (truth), then with keywords only,
and count how often the keyword hit lands in the same episode within 30 s / 120 s of the quote hit.
usage: py tools/keyword_vs_quote.py "<channel folder>" "<show>" """
import collections, glob, json, os, sys
sys.path.insert(0, r"D:\Lib")
from media_index import locator
from editv2 import clue
base, show = sys.argv[1], sys.argv[2]
k = locator._key(show)
lib = clue.Library([{"key": k, "name": show, "root": locator.show_folders(locator.roots())[k], "indexed": True}])
G = clue.Grounder(lib)
c = collections.Counter()
for p in glob.glob(os.path.join(glob.escape(base), "*", "*refs*.json")):
    for r in json.load(open(p, encoding="utf-8-sig")).get("refs", []):
        if not clue._toks(r.get("quote")):
            continue
        g = G.ground(k, r.get("ep"), r["quote"], r.get("keywords"))
        if not g or g[3] != "exact":
            continue
        c["n"] += 1
        w = G.ground(k, r.get("ep"), "", r.get("keywords"))
        if not w:
            c["none"] += 1; continue
        same = w[0] == g[0]
        c["same_ep"] += same
        c["30s"] += same and abs(w[1] - g[1]) <= 30
        c["120s"] += same and abs(w[1] - g[1]) <= 120
n = max(1, c["n"])
print(f"{c['n']} refs with an exact quote hit | keywords-only: no hit {100*c['none']//n}% | same episode {100*c['same_ep']//n}% | "
      f"within 30 s {100*c['30s']//n}% | within 2 min {100*c['120s']//n}%")
