r"""Pre-flight audit of language batches (D:\Movies\<Language>\<Category>\<topic>\ script + refs json), shows may differ per video.
usage: py tools/audit_lang_batch.py "<root>" [...]  -> table per video + totals by language/category"""
import collections, json, os, re, sys
sys.path.insert(0, r"D:\Lib")
from media_index import locator
from editv2 import clue, ctx as _ctx
from editv2.pipeline import detect_language

BAD = [(r"^\s*#{1,6}\s", "markdown heading"), (r"\*\*[^*]+\*\*", "**bold**"), (r"【|†|turn\d+(search|view)", "citation marks"),
       (r"\[(music|pause|b-roll|cut to|scene|visual|sfx)[^\]]*\]", "stage direction"), (r"(?im)^(narrator|host|voice ?over|narrador|narrateur)\s*:", "speaker label"),
       (r"(?i)here(?: is|'s) (?:your|the) (?:script|final)|word count|let me know|^(?:voici|aquí está|aqui está|aqui estão|aquí tienes) (?:le|el|o|tu|seu|votre|ton)\b[^\n]*(?:script|guion|roteiro|texte)", "chat text"), (r"https?://", "URL")]
folders = locator.show_folders(locator.roots())
lib_cache, rows, tot = {}, [], collections.defaultdict(collections.Counter)


def lib_for(keys):
    key = tuple(sorted(keys))
    if key not in lib_cache:
        lib_cache[key] = clue.Library([{"key": k, "name": _ctx._clean_name(folders[k]), "root": folders[k], "indexed": True} for k in key])
    return lib_cache[key]


for root in sys.argv[1:]:
    for cat in sorted(os.listdir(root)):
        cp = os.path.join(root, cat)
        if not os.path.isdir(cp):
            continue
        for d in sorted(os.listdir(cp)):
            topic = os.path.join(cp, d)
            lang = os.path.basename(root)
            if not os.path.isdir(topic):
                rows.append((lang, cat, d, "!! stray file at category level (not a topic folder)")); continue
            txts = [f for f in os.listdir(topic) if f.lower().endswith(".txt") and not f.lower().endswith("_qa.txt")]
            jss = [f for f in os.listdir(topic) if f.lower().endswith(".json")]
            if len(txts) != 1 or not jss:
                rows.append((lang, cat, d, f"!! {len(txts)} scripts, {len(jss)} json")); continue
            script = os.path.join(topic, txts[0])
            text = open(script, encoding="utf-8-sig").read()
            words = len(text.split())
            issues = [n for p, n in BAD if re.search(p, text, re.M)]
            try:
                dl = detect_language(script)
            except Exception as e:
                dl = "?"
            paths = clue.refs_path({"clean": script})
            raw = []
            try:
                for p in paths:
                    j = json.load(open(p, encoding="utf-8-sig"))
                    raw += j.get("refs", []) if isinstance(j, dict) else j
            except Exception as e:
                rows.append((lang, cat, d, f"!! refs unreadable: {e}")); continue
            names = {r.get("show") for r in raw if r.get("show")}
            keys, unknown = set(), set()
            for n in names:
                k = locator._key(n) if n else None
                (keys.add(k) if k in folders else unknown.add(n))
            if not keys:
                rows.append((lang, cat, d, f"!! no known show in refs {sorted(unknown)}")); continue
            lib = lib_for(keys)
            have = {e[1] for e in lib.eps}
            G = clue.Grounder(lib)
            cx = {"shows": [{"key": k, "name": _ctx._clean_name(folders[k])} for k in keys], "clean": script}
            lines = clue.split_lines(text)
            try:
                ext = clue.external_refs(paths, cx, lines, lib, log=lambda *a: None)
            except Exception as e:
                rows.append((lang, cat, d, f"!! external_refs: {type(e).__name__}: {e}")); continue
            c = collections.Counter(); missing = set()
            for j, rs in ext.items():
                for r in rs:
                    c["refs"] += 1; c["quoted"] += bool(clue._toks(r.get("quote")))
                    if r.get("ep") and r["ep"] not in have: missing.add((r.get("show"), r["ep"]))
                    g = G.ground(r["show"], r["ep"], r.get("quote"), r.get("keywords")) if r.get("show") else None
                    if g:
                        c["placed"] += 1; c["exact"] += g[3] == "exact"; c["ep_ok"] += g[0][1] == r.get("ep")
            n = max(1, c["refs"])
            flag = []
            if not ext: flag.append("NO refs mapped to script lines")
            elif len(ext) < 0.25 * len(lines): flag.append(f"refs cover only {len(ext)}/{len(lines)} lines")
            if c["quoted"] < 0.8 * n: flag.append(f"{100 - 100 * c['quoted'] // n}% empty quotes")
            elif c["exact"] < 0.85 * c["quoted"]: flag.append("quotes not word-for-word")
            if c["placed"] < 0.9 * n: flag.append(f"only {100 * c['placed'] // n}% placed")
            if missing: flag.append(f"eps not in library {sorted(missing)[:4]}")
            if unknown: flag.append(f"shows not in library {sorted(unknown)}")
            if issues: flag.append("script: " + ", ".join(issues))
            if words < 2500: flag.append(f"short ({words} w)")
            if dl != {"French": "fr", "Spanish": "es", "Portuguese": "pt"}.get(lang): flag.append(f"language detected '{dl}'")
            for x in c: tot[(lang, cat)][x] += c[x]
            tot[(lang, cat)]["videos"] += 1; tot[(lang, cat)]["flagged"] += bool(flag)
            rows.append((lang, cat, d, f"{words:5} w | refs {c['refs']:3} on {len(ext):3}/{len(lines):3} lines | quote {100 * c['quoted'] // n:3}% | exact {100 * c['exact'] // max(1, c['quoted']):3}% | placed {100 * c['placed'] // n:3}% | ep {100 * c['ep_ok'] // max(1, c['placed']):3}% | shows {len(keys)}"
                                      + ("  <<< " + "; ".join(flag) if flag else "  OK")))
for lang, cat, d, s in rows:
    print(f"[{lang[:2]}/{cat[:5]}] {d[:50]:50} {s}", flush=True)
print()
for (lang, cat), c in sorted(tot.items()):
    n = max(1, c["refs"])
    print(f"{lang}/{cat}: {c['videos']} videos, {c['flagged']} flagged | refs {c['refs']} quote {100*c['quoted']//n}% exact {100*c['exact']//max(1,c['quoted'])}% placed {100*c['placed']//n}% ep {100*c['ep_ok']//max(1,c['placed'])}%")
