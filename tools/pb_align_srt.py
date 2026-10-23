"""Give catalogues without subtitle cues (Prison Break, 2026-10-23) the English .srt lying next to the footage:
each srt line is matched to the whisper dialogue of the catalogue's shots, a line t_ours = a * t_srt + b is fitted to the
matches (RANSAC: an offset AND a frame-rate drift, e.g. PAL DVD subtitles), and the best-fitting srt is written into the
catalogue as 'cues' (dialogue_source 'srt-aligned:...'). A backup '<catalog>.pre_srt.bak' is kept. No API, no GPU.
usage: py tools/pb_align_srt.py "<show root>" [--dry] [--prefer "<srt path part>"...]"""
import glob, json, os, random, re, shutil, sys, collections
sys.path.insert(0, r"D:\Lib")
from editv2.common import lp

root = sys.argv[1]
DRY = "--dry" in sys.argv


def srt_cues(path):
    raw = open(path, "rb").read()
    txt = None
    for enc in (("utf-16",) if raw[:2] in (bytes([255, 254]), bytes([254, 255])) else ()) + ("utf-8-sig", "cp1252", "latin-1"):
        try:
            txt = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    out = []
    for blk in re.split(r"\r?\n\r?\n", (txt or "").strip()):
        ln = [x for x in blk.splitlines() if x.strip()]
        i = next((k for k, x in enumerate(ln) if "-->" in x), None)
        if i is None:
            continue
        m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", ln[i].strip())
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        a = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        b = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        t = " ".join(re.sub(r"<[^>]+>|\{[^}]*\}", "", x).strip() for x in ln[i + 1:])
        t = " ".join(t.split())
        if t:
            out.append((a, b, t))
    return out


def toks(s):
    return [w for w in re.findall(r"[a-z']+", s.lower().replace("I", "l")) if len(w) > 2]


def pairs(cues, shots):
    """(srt time, our time) for srt lines whose words clearly match one shot's dialogue"""
    idx = collections.defaultdict(set)
    sw = []
    for k, (t, d) in enumerate(shots):
        w = set(toks(d))
        sw.append(w)
        for x in w:
            idx[x].add(k)
    df = {x: len(v) for x, v in idx.items()}
    out = []
    for a, b, t in cues:
        w = set(toks(t))
        if len(w) < 3:
            continue
        rare = sorted(w, key=lambda x: df.get(x, 0))[:3]
        cand = set().union(*(idx.get(x, set()) for x in rare))
        best, bk = 0.0, None
        for k in cand:
            j = len(w & sw[k]) / len(w | sw[k])
            if j > best:
                best, bk = j, k
        if bk is not None and best >= 0.5:
            out.append((a, shots[bk][0]))
    return out


def fit(pr):
    """RANSAC line ours = a*srt + b with a in [0.94, 1.06]; -> (a, b, inliers, residual median)"""
    if len(pr) < 8:
        return None
    rnd = random.Random(7)
    best = None
    for _ in range(400):
        (x1, y1), (x2, y2) = rnd.sample(pr, 2)
        if abs(x2 - x1) < 60:
            continue
        a = (y2 - y1) / (x2 - x1)
        if not 0.94 <= a <= 1.06:
            continue
        b = y1 - a * x1
        inl = [(x, y) for x, y in pr if abs(a * x + b - y) < 2.5]
        if best is None or len(inl) > len(best[2]):
            best = (a, b, inl)
    if not best or len(best[2]) < 8:
        return None
    inl = best[2]                                      # least squares on the inliers
    n = len(inl)
    mx = sum(x for x, _ in inl) / n
    my = sum(y for _, y in inl) / n
    sxx = sum((x - mx) ** 2 for x, _ in inl) or 1.0
    a = sum((x - mx) * (y - my) for x, y in inl) / sxx
    b = my - a * mx
    res = sorted(abs(a * x + b - y) for x, y in inl)
    return a, b, n, res[n // 2]


srts = collections.defaultdict(list)
for f in glob.glob(os.path.join(root, "**", "*.srt"), recursive=True):
    m = re.search(r"(?:S(\d{1,2})E(\d{1,2})|\b(\d{1,2})x(\d{2})\b)", os.path.basename(f), re.I)
    if m and re.search(r"\.(en|eng)\.srt$", f, re.I):
        s, e = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        srts[f"S{int(s):02d}E{int(e):02d}"].append(f)
done = weak = 0
for cat in sorted(glob.glob(os.path.join(root, "**", "*.catalog.json"), recursive=True)):
    m = re.search(r"S(\d\d)E(\d\d)", os.path.basename(cat), re.I)
    if not m:
        continue
    code = f"S{m.group(1)}E{m.group(2)}".upper()
    d = json.load(open(lp(cat), encoding="utf-8"))
    if d.get("cues"):
        print(f"{code}: has cues already - left alone")
        continue
    shots = [(float(s["start"]), s.get("dialogue") or "") for s in d["shots"] if len(toks(s.get("dialogue") or "")) >= 3]
    res = []
    for f in srts.get(code, []):
        cues = srt_cues(f)
        r = fit(pairs(cues, shots))
        if r:
            res.append((r[2], -r[3], f, cues, r))
    if not res:
        print(f"{code}: no srt fits - skipped")
        weak += 1
        continue
    res.sort(reverse=True)
    # the srt that tools/export_subs_srt.py put in the ChatGPT zip (bluray > dvdrip > other): its wording is what the
    # refs quote, so it wins whenever it fits well enough
    zscore = lambda f: 2 if re.search(r"bluray|bdrip", os.path.basename(f), re.I) else 1 if re.search(r"dvdrip", os.path.basename(f), re.I) else 0
    zip_f = max(srts.get(code, []), key=zscore, default=None)
    zr = next((x for x in res if x[2] == zip_f and x[4][2] >= 40 and x[4][3] <= 1.2), None)
    n_in, _, f, cues, (a, b, n, med) = zr or res[0]
    ok = n >= 40 and med <= 1.2
    tag = "OK  " if ok else "WEAK"
    print(f"{code}: {tag} {os.path.basename(f)[:48]:48} matched {n:3} lines, t = {a:.4f}*srt {b:+.1f}s, median error {med:.2f}s")
    if not ok:
        weak += 1
        continue
    dur = float(d.get("duration") or d["shots"][-1]["end"])
    new = [[round(a * s + b, 3), round(a * e + b, 3), t] for s, e, t in cues if 0 <= a * s + b <= dur + 1]
    if DRY:
        done += 1
        continue
    bak = cat + ".pre_srt.bak"
    if not os.path.exists(lp(bak)):
        shutil.copy2(lp(cat), lp(bak))
    d["cues"] = new
    d["dialogue_source"] = f"srt-aligned:{os.path.basename(f)} a={a:.5f} b={b:+.2f} n={n} err={med:.2f}"
    tmp = cat + ".tmp"
    json.dump(d, open(lp(tmp), "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(lp(tmp), lp(cat))
    done += 1
print(f"\n{done} catalogue(s) {'would get' if DRY else 'got'} cues, {weak} left without")
