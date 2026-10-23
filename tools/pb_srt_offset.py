"""How far the Prison Break .srt files are from our BluRay rips: match srt lines to the whisper dialogue in the catalogue
shots and print the time offset per episode (median, spread)."""
import glob, json, os, re, sys, difflib, statistics
sys.path.insert(0, r"D:\Lib")
sys.path.insert(0, r"D:\Lib\tools")
from editv2.common import lp
import importlib.util
spec = importlib.util.spec_from_file_location("ex", r"D:\Lib\tools\export_subs_srt.py")
def srt_cues(path):
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try: txt = raw.decode(enc); break
        except UnicodeDecodeError: continue
    out = []
    for blk in re.split(r"\r?\n\r?\n", txt.strip()):
        ln = [x for x in blk.splitlines() if x.strip()]
        i = next((k for k, x in enumerate(ln) if "-->" in x), None)
        if i is None: continue
        m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", ln[i].strip())
        if not m: continue
        g = [int(x) for x in m.groups()]
        a = g[0]*3600+g[1]*60+g[2]+g[3]/1000; b = g[4]*3600+g[5]*60+g[6]+g[7]/1000
        t = " ".join(re.sub(r"<[^>]+>|\{[^}]*\}", "", x).strip() for x in ln[i+1:])
        if t: out.append((a, b, " ".join(t.split())))
    return out
def norm(s): return re.sub(r"[^a-z ]", "", s.lower().replace("l", "l"))
root = r"E:\Movies\Prison Break"
srts = {}
for f in glob.glob(os.path.join(root, "**", "*.srt"), recursive=True):
    m = re.search(r"\b(\d{1,2})x(\d{2})\b", os.path.basename(f))
    if m and re.search(r"\.en\.srt$", f, re.I):
        srts.setdefault(f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}", []).append(f)
for code in sys.argv[1:]:
    cat = [c for c in glob.glob(os.path.join(root, "**", f"*{code}*.catalog.json"), recursive=True)][0]
    shots = json.load(open(lp(cat), encoding="utf-8"))["shots"]
    dl = [(float(s["start"]), norm(s.get("dialogue") or "")) for s in shots if len(norm(s.get("dialogue") or "").split()) >= 4]
    for f in srts.get(code, []):
        cues = srt_cues(f); offs = []
        for a, b, t in cues:
            nt = norm(t)
            if len(nt.split()) < 4: continue
            best = max(dl, key=lambda d: difflib.SequenceMatcher(None, nt, d[1]).ratio() if abs(d[0] - a) < 120 else 0)
            if difflib.SequenceMatcher(None, nt, best[1]).ratio() > 0.7 and abs(best[0] - a) < 120:
                offs.append(best[0] - a)
        if len(offs) > 5:
            q = sorted(offs); n = len(q)
            print(f"{code} {os.path.basename(f)[:45]:45} matched {n:3}/{len(cues)} | offset median {statistics.median(q):+6.2f}s | p10 {q[n//10]:+6.2f} p90 {q[9*n//10]:+6.2f} | first {statistics.median(q[:n//4]):+.2f} last {statistics.median(q[-n//4:]):+.2f}")
        else:
            print(code, os.path.basename(f)[:45], "too few matches", len(offs))
