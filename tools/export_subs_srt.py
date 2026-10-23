"""Subtitle zip for ChatGPT from the .srt files next to the footage (for shows whose catalogues carry no subtitle cues,
e.g. Prison Break). Same layout as tools/export_subs.py: '<Show> SxxEyy <Title>.txt', one '[mm:ss] text' line per cue.
usage: py tools/export_subs_srt.py "<show name>" "<show root folder>" """
import glob, os, re, sys, zipfile
name, root = sys.argv[1], sys.argv[2]
OUT = os.path.join(os.path.expanduser("~"), "Desktop", "Subtitles for ChatGPT")
os.makedirs(OUT, exist_ok=True)
best = {}
for f in glob.glob(os.path.join(root, "**", "*.srt"), recursive=True):
    b = os.path.basename(f)
    m = re.search(r"(?:S(\d{1,2})E(\d{1,2})|\b(\d{1,2})x(\d{2})\b)", b, re.I)
    if not m or not re.search(r"\.(en|eng)\.srt$", b, re.I):
        continue
    s, e = (int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(3)), int(m.group(4)))
    t = re.search(r"\d+x\d+\s*-\s*(.+?)\.(?:DVDRip|HDTV|BluRay|WEB|720p|1080p|en)", b, re.I)
    title = t.group(1).strip() if t else ""
    score = 2 if re.search(r"bluray|bdrip", b, re.I) else 1 if re.search(r"dvdrip", b, re.I) else 0
    if (s, e) not in best or score > best[(s, e)][0]:
        best[(s, e)] = (score, f, title)


def cues(path):
    raw = open(path, "rb").read()
    for enc in (("utf-16",) if raw[:2] in (bytes([255, 254]), bytes([254, 255])) else ()) + ("utf-8-sig", "cp1252", "latin-1"):
        try:
            txt = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    out = []
    for blk in re.split(r"\r?\n\r?\n", txt.strip()):
        ln = [x for x in blk.splitlines() if x.strip()]
        i = next((k for k, x in enumerate(ln) if "-->" in x), None)
        if i is None:
            continue
        m = re.match(r"(\d+):(\d+):(\d+)", ln[i].strip())
        if not m:
            continue
        sec = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        text = " ".join(re.sub(r"<[^>]+>|\{[^}]*\}", "", x).strip() for x in ln[i + 1:])
        if text:
            out.append((sec, " ".join(text.split())))
    return out


zp = os.path.join(OUT, re.sub(r'[\/:*?"<>|]', "", name) + ".zip")
n = 0
with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
    for (s, e), (_, f, title) in sorted(best.items()):
        c = cues(f)
        if not c:
            continue
        code = f"S{s:02d}E{e:02d}"
        body = "\n".join(f"[{t // 60:02d}:{t % 60:02d}] {x}" for t, x in c)
        z.writestr(f"{name} {code} {title}".strip() + ".txt",
                   f"{name} {code} {title}\n(English subtitles; [mm:ss] = time in the episode)\n\n{body}\n")
        n += 1
print(f"{name}: {n} episodes -> {zp} ({os.path.getsize(zp) // 1024} KB)")
