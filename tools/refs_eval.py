"""Grounding quality of every ChatGPT refs file with the editv2 code under <root> (A/B of tool versions).
usage: py tools/refs_eval.py <root of the editv2 checkout>"""
import json, os, sys, time
root = sys.argv[1]
sys.path.insert(0, root)
from media_index import locator
from editv2 import clue

T = r"C:\Users\Dell\Downloads\Test"
R = r"D:\Movies\English\Reacher"
W = "Carcetti's transformation from reform candidate into a system-maintaining politician"
PAIRS = [("wire", rf"D:\Movies\English\The Wire\{W}\{W}.txt", r"D:\Lib\tools\_clue_ab\gptchat\carcetti_refs.json", "The Wire")]
for t, s in [("m1", r"Test-1\SceneHush_11_Ordinary_Evidence_TTS_READY_ENGLISH.txt"), ("m2", r"Test -2\23_SH-41_CHO_SUMMER_VEGA_TTS_EN_DRAFT_V1.txt"),
             ("m3", r"Test-3\SceneHush_11_Ordinary_Evidence_TTS_READY_ENGLISH.txt"), ("m4", r"Test-4\23_SH-41_CHO_SUMMER_VEGA_TTS_EN_DRAFT_V1.txt")]:
    p = os.path.join(T, s)
    PAIRS.append((t, p, os.path.join(os.path.dirname(p), "refs.json"), "The Mentalist"))
for n in ("16", "17", "18", "19", "20"):
    d = next(os.path.join(R, x) for x in os.listdir(R) if x.startswith(n + " "))
    rf = next(os.path.join(d, x) for x in os.listdir(d) if x.startswith("refs") and x.endswith(".json"))
    PAIRS.append(("r" + n, os.path.join(d, "Script.txt"), rf, "Reacher"))
libs = {}
folders = locator.show_folders(locator.roots())
tot = {"refs": 0, "quoted": 0, "exact": 0, "placed": 0, "ep_ok": 0}
for tag, script, refs, show in PAIRS:
    k = locator._key(show)
    if show not in libs:
        libs[show] = clue.Library([{"key": k, "name": show, "root": folders[k], "indexed": True}])
    lib = libs[show]
    G = clue.Grounder(lib)
    ctx = {"shows": [{"key": k, "name": show}], "clean": script}
    lines = clue.split_lines(open(script, encoding="utf-8-sig").read())
    ext = clue.external_refs([refs], ctx, lines, lib, log=lambda *a: None)
    c = {"refs": 0, "quoted": 0, "exact": 0, "placed": 0, "ep_ok": 0}
    t0 = time.time()
    for j, rs in ext.items():
        for r in rs:
            c["refs"] += 1
            c["quoted"] += bool(clue._toks(r.get("quote")))
            g = G.ground(r["show"], r["ep"], r.get("quote"), r.get("keywords")) if r.get("show") else None
            if g:
                c["placed"] += 1
                c["exact"] += len(g) > 3 and g[3] == "exact"
                c["ep_ok"] += g[0][1] == r.get("ep")
    for x in tot:
        tot[x] += c[x]
    n = max(1, c["refs"])
    print(f"{tag:5} refs {c['refs']:4} | quote {100 * c['quoted'] // n:3}% | word-for-word {c['exact']:4} ({100 * c['exact'] // n:3}%)"
          f" | placed {100 * c['placed'] // n:3}% | in named ep {100 * c['ep_ok'] // max(1, c['placed']):3}% | {time.time() - t0:.0f}s", flush=True)
n = max(1, tot["refs"])
print(f"ALL   refs {tot['refs']} | quote {100 * tot['quoted'] // n}% | word-for-word {tot['exact']} ({100 * tot['exact'] // n}%)"
      f" | of quoted {100 * tot['exact'] // max(1, tot['quoted'])}% | placed {100 * tot['placed'] // n}%")
