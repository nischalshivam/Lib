"""Pre-flight for a batch of topic folders (one script + one refs file each): refs quality per video (quote share, word
for word in OUR subtitles, placed, named episode, episodes we lack) + script text sanity (ChatGPT leftovers).
usage: py tools/batch_refs_check.py "<channel folder>" "<show name>" """
import collections, json, os, re, sys
sys.path.insert(0, r"D:\Lib")
from media_index import locator
from editv2 import clue

base, show = sys.argv[1], sys.argv[2]
k = locator._key(show)
lib = clue.Library([{"key": k, "name": show, "root": locator.show_folders(locator.roots())[k], "indexed": True}])
G = clue.Grounder(lib)
have = {e[1] for e in lib.eps}
BAD = [(r"^\s*#{1,6}\s", "markdown heading"), (r"\*\*[^*]+\*\*", "**bold**"), (r"【|†|turn\d+(search|view)", "citation marks"),
       (r"\[(music|pause|b-roll|cut to|scene|visual|sfx)[^\]]*\]", "stage direction"), (r"(?im)^(narrator|host|voice ?over)\s*:", "speaker label"),
       (r"(?i)here(?: is|'s) (?:your|the) (?:script|final)|word count|let me know", "chat text"), (r"https?://", "URL")]
rows, tot = [], collections.Counter()
for d in sorted(os.listdir(base)):
    topic = os.path.join(base, d)
    if not os.path.isdir(topic) or d.lower() == "final videos":
        continue
    txts = [f for f in os.listdir(topic) if f.lower().endswith(".txt") and not f.lower().endswith("_qa.txt")]
    script = os.path.join(topic, txts[0]) if len(txts) == 1 else None
    if not script:
        rows.append((d, f"!! {len(txts)} scripts in the folder")); continue
    text = open(script, encoding="utf-8-sig").read()
    words = len(text.split())
    issues = [n for p, n in BAD if re.search(p, text, re.M)]
    ctx = {"shows": [{"key": k, "name": show}], "clean": script}
    paths = clue.refs_path(ctx)
    if not paths:
        rows.append((d, f"!! no refs file | {words} words")); continue
    lines = clue.split_lines(text)
    try:
        ext = clue.external_refs(paths, ctx, lines, lib, log=lambda *a: None)
    except Exception as e:
        rows.append((d, f"!! refs file unreadable: {type(e).__name__}: {e}")); continue
    c = collections.Counter()
    missing, eps = set(), collections.Counter()
    for j, rs in ext.items():
        for r in rs:
            c["refs"] += 1
            c["quoted"] += bool(clue._toks(r.get("quote")))
            eps[r.get("ep")] += 1
            if r.get("ep") and r["ep"] not in have:
                missing.add(r["ep"])
            g = G.ground(r["show"], r["ep"], r.get("quote"), r.get("keywords")) if r.get("show") else None
            if g:
                c["placed"] += 1
                c["exact"] += g[3] == "exact"
                c["ep_ok"] += g[0][1] == r.get("ep")
    for x in c:
        tot[x] += c[x]
    n = max(1, c["refs"])
    flag = []
    if c["quoted"] < 0.8 * n: flag.append(f"{100 - 100 * c['quoted'] // n}% empty quotes")
    if c["quoted"] and c["exact"] < 0.85 * c["quoted"]: flag.append("quotes not word-for-word")
    if c["placed"] < 0.9 * n: flag.append("refs not placed")
    if missing: flag.append(f"episodes not in library {sorted(missing)}")
    if issues: flag.append("script: " + ", ".join(issues))
    if words < 2500: flag.append(f"short script ({words} words)")
    rows.append((d, f"{words:5} w | refs {c['refs']:3} on {len(ext):3}/{len(lines):3} lines | quote {100 * c['quoted'] // n:3}% | "
                    f"word-for-word {100 * c['exact'] // max(1, c['quoted']):3}% of quoted | placed {100 * c['placed'] // n:3}% | "
                    f"named ep {100 * c['ep_ok'] // max(1, c['placed']):3}% | eps {len(eps):2}" + ("  <<< " + "; ".join(flag) if flag else "  OK")))
for d, s in rows:
    print(f"{d[:58]:58} {s}")
n = max(1, tot["refs"])
print(f"\nALL {len(rows)} videos: refs {tot['refs']} | quote {100 * tot['quoted'] // n}% | word-for-word {100 * tot['exact'] // max(1, tot['quoted'])}% of quoted"
      f" | placed {100 * tot['placed'] // n}% | named ep {100 * tot['ep_ok'] // max(1, tot['placed'])}%")
