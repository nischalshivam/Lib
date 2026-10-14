"""Final report of the Movie Editor batch (editv2/cache/last_batch.json): per video the internet-phase, render, mix and
QA minutes, the video length, QA verdict + warnings, render retries and errors; then totals, averages, throughput.
    py tools\\batch_report.py              print
    py tools\\batch_report.py --md <file>  also write Markdown"""
import json, os, re, subprocess, sys, time

B = r"D:\Lib\editv2\cache\last_batch.json"


def lp(p):
    p = os.path.abspath(p)
    return "\\\\?\\" + p if len(p) >= 248 and not p.startswith("\\\\?\\") else p


def secs(hms):
    h, m, s = map(int, hms.split(":"))
    return h * 3600 + m * 60 + s


def parse(clean):
    bd = os.path.join(os.path.dirname(clean), os.path.splitext(os.path.basename(clean))[0] + "_v2build")
    try:
        lines = open(lp(os.path.join(bd, "build.log")), encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return None
    t, day, last = [], 0, -1
    for ln in lines:                                   # timeline of (abs seconds, stage) - midnight-safe
        m = re.match(r"^\[(\d\d:\d\d:\d\d)\] (\w+)", ln)
        if m:
            s = secs(m.group(1))
            if last >= 0 and s < last - 600:
                day += 86400
            last = s
            t.append((s + day, m.group(2)))
    r = dict(qa="", warns=[], retries=0, errors=[], video_s=None, stages={})
    for ln in lines:
        if re.search(r"QA (PASS|WARN|FAIL)", ln):
            r["qa"] = re.search(r"QA (PASS|WARN|FAIL)", ln).group(1)
        if ln.strip().startswith("WARN:"):
            r["warns"].append(ln.strip()[6:][:110])
        if "attempt" in ln and "failed" in ln:
            r["retries"] += 1
        m = re.search(r"render: \d+s, (\d+)s of video", ln)
        if m:
            r["video_s"] = int(m.group(1))
        if re.search(r"Traceback|RuntimeError|could not save|render stalled", ln):
            r["errors"].append(ln.strip()[:150])
    for (a, st), (b, _) in zip(t, t[1:] + [(t[-1][0], "end")] if t else []):
        r["stages"][st] = r["stages"].get(st, 0) + (b - a)
    if t:
        r["start"], r["end"] = t[0][0], t[-1][0]
    online = sum(r["stages"].get(k, 0) for k in ("preflight", "context", "clue", "voice", "words", "plan", "cut", "look"))
    r["online_min"] = round(online / 60, 1)
    for k in ("render", "mix", "QA"):
        r[k + "_min"] = round(r["stages"].get(k, 0) / 60, 1)
    r["done"] = bool(lines) and lines[-1].startswith("DONE") or any(x.startswith("DONE ") for x in lines[-3:])
    return r


def main():
    b = json.load(open(B, encoding="utf-8"))
    specs, state = b.get("specs", []), {str(k): v for k, v in (b.get("state") or {}).items()}
    rows = []
    for i, s in enumerate(specs):
        r = parse(s["clean"]) or {}
        rows.append((i, os.path.basename(os.path.dirname(s["clean"])), state.get(str(i), "pending"), r))
    out = []
    P = out.append
    P(f"# The Wire batch report - {time.strftime('%Y-%m-%d %H:%M')}\n")
    done = [x for x in rows if x[3].get("qa")]
    P(f"Videos: {len(rows)} queued, {len(done)} finished, states {dict((s, sum(1 for x in rows if x[2] == s)) for s in set(x[2] for x in rows))}\n")
    P("| # | Topic | Video | Online | Render | Mix+QA | QA | Retries | Warnings |")
    P("|---|---|---|---|---|---|---|---|---|")
    for i, name, st, r in rows:
        if not r:
            P(f"| {i + 1} | {name[:55]} | - | - | - | - | {st} | | |")
            continue
        v = f"{r['video_s'] / 60:.1f}m" if r.get("video_s") else "-"
        P(f"| {i + 1} | {name[:55]} | {v} | {r['online_min']}m | {r['render_min']}m | {round(r['mix_min'] + r['QA_min'], 1)}m |"
          f" {r['qa'] or st} | {r['retries'] or ''} | {'; '.join(r['warns'])[:140]} |")
    if done:
        starts = [x[3]["start"] for x in rows if x[3].get("start")]
        ends = [x[3]["end"] for x in done]
        span_h = (max(ends) - min(starts)) / 3600 if starts and ends else 0
        vid = [x[3]["video_s"] for x in done if x[3].get("video_s")]
        rend = [x[3]["render_min"] for x in done if x[3].get("render_min")]
        onl = [x[3]["online_min"] for x in done]
        P("\n## Totals")
        P(f"- wall clock: {span_h:.1f} h for {len(done)} videos -> {60 * span_h / len(done):.0f} min per video on average")
        if vid:
            P(f"- video length: avg {sum(vid) / len(vid) / 60:.1f} min, total {sum(vid) / 3600:.1f} h of video")
        if rend and vid:
            P(f"- render: avg {sum(rend) / len(rend):.0f} min = {sum(rend) * 60 / sum(vid):.2f}x the video length")
        P(f"- internet phase: avg {sum(onl) / len(onl):.0f} min (2 at a time, overlapping the renders)")
        qa = {}
        for x in done:
            qa[x[3]["qa"]] = qa.get(x[3]["qa"], 0) + 1
        P(f"- QA: {qa}; render retries: {sum(x[3].get('retries', 0) for x in done)}")
        errs = [(x[0] + 1, e) for x in rows for e in x[3].get("errors", [])] if rows else []
        if errs:
            P("\n## Errors seen")
            for i, e in errs[:40]:
                P(f"- V{i}: {e}")
    txt = "\n".join(out)
    print(txt)
    if "--md" in sys.argv:
        open(sys.argv[sys.argv.index("--md") + 1], "w", encoding="utf-8").write(txt + "\n")


if __name__ == "__main__":
    main()
