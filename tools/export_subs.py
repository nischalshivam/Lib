"""Subtitles of our OWN footage for ChatGPT: one plain-text file per catalogued episode / film ("<Show> SxxEyy <Title>.txt",
one subtitle cue per line with its time), zipped per show. Attach the zip in ChatGPT with the refs prompt - its quotes are
then copied from exactly the subtitles the Movie Editor searches (word-for-word hits ~100% instead of ~85%).
usage: py tools/export_subs.py "<show name>" ["<show name>" ...]      -> Desktop\\Subtitles for ChatGPT\\<Show>.zip"""
import glob, json, os, re, sys, zipfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from media_index import locator
from editv2.clue import Library

OUT = os.path.join(os.path.expanduser("~"), "Desktop", "Subtitles for ChatGPT")
os.makedirs(OUT, exist_ok=True)
folders = locator.show_folders(locator.roots())
for name in sys.argv[1:]:
    key = locator._key(name)
    if key not in folders:
        print(f"not in the library: {name}")
        continue
    lib = Library([{"key": key, "name": name, "root": folders[key], "indexed": True}])
    zp = os.path.join(OUT, re.sub(r'[\\/:*?"<>|]', "", name) + ".zip")
    n = 0
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for (k, ep), e in sorted(lib.eps.items()):
            cues = e.get("cues") or []
            if not cues:
                continue
            title = e.get("title") or ""
            body = "\n".join(f"[{int(c[0] // 60):02d}:{int(c[0] % 60):02d}] {' '.join(str(c[2]).split())}" for c in cues)
            z.writestr(f"{name} {ep} {title}".strip() + ".txt",
                       f"{name} {ep} {title}\n(English subtitles of the editor's own footage; [mm:ss] = time in the episode)\n\n{body}\n")
            n += 1
    print(f"{name}: {n} episodes/films -> {zp} ({os.path.getsize(zp) // 1024} KB)")
