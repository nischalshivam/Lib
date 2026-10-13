"""Match the user's downloaded subtitles (Desktop\\Subtitles\\<show>\\...\\<show> - 1x01 - Title.<release>.en.srt)
to OUR video files, verify the sync against the real audio, and install the winner next to the video as
"<video name>.en.srt" (shifted/stretched when needed). Never installs a subtitle that does not verify.

Sync test: 4 x 40-s audio windows (15/38/62/85 % of the episode) -> whisper word times; every cue with >=3 words
is located in the heard words (first 3 normalised tokens); offsets -> linear fit t_video = a*t_sub + b.
Accept: >=10 matched cues, |a-1| <= 0.05, median residual <= 0.35 s. Candidates: same episode number and +-1
(catches 'unaired pilot' numbering shifts). Resumable; writes subs_report.json."""
import glob, json, os, re, subprocess, sys, tempfile, time, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from media_index import subtitles, dialogue_fill as DF

SUBS = r"C:\Users\Dell\Desktop\Subtitles"
ALIAS = {"house md": "House", "tahm": "Two and a Half Men", "brooklyn nine nine": "Brooklyn Nine-Nine"}
SKIP_ROOTS = ("Movies DATA 3\\Two and a Half Men", "[ENG-Lektor PL]", "Cartoons DATA")


def _n(x):
    return re.sub(r"[^a-z0-9]", "", x.lower())


def library_roots(sub_dir):
    """Library folders a subtitle folder belongs to (e.g. 'The Boys' -> DATA/The Boys + DATA 2/The Boys Season 1)."""
    want = _n(ALIAS.get(sub_dir.lower(), sub_dir))
    scan = json.load(open(os.path.join(C.HERE, "scan_all.json"), encoding="utf-8"))
    tops = [t for t in scan if not any(k in t for k in SKIP_ROOTS)]
    exact = [t for t in tops if _n(os.path.basename(t)) == want]
    if exact:
        extra = [t for t in tops if _n(os.path.basename(t)).startswith(want) and t not in exact
                 and re.search(r"season|s\d", os.path.basename(t).lower())]
        return exact + extra
    return [t for t in tops if _n(os.path.basename(t)).startswith(want)]


VID = (".mkv", ".mp4", ".avi", ".m4v")
REPORT = os.path.join(C.HERE, "subs_report.json")
LOG = open(os.path.join(C.LOGS, "subs_install.log"), "a", encoding="utf-8", buffering=1)


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, file=LOG, flush=True)


def ep_of(name):
    m = re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", name) or re.search(r"\b(\d{1,2})x(\d{2,3})\b", name)
    return (int(m.group(1)), int(m.group(2))) if m else None


def norm(w):
    return re.sub(r"[^a-z0-9']", "", w.lower().replace("â€™", "'"))


def heard_words(video, dur, fracs=(0.15, 0.38, 0.62, 0.85)):
    out = []
    for frac in fracs:
        a = max(0.0, dur * frac)
        wav = os.path.join(tempfile.gettempdir(), f"subs_{os.getpid()}.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{a:.1f}", "-t", "40", "-i", C.lp(video),
                        "-map", f"0:a:{DF.english_track(video)}", "-ac", "1", "-ar", "16000", wav], check=True)
        segs, _ = DF.model().transcribe(wav, language="en", vad_filter=True, word_timestamps=True,
                                        condition_on_previous_text=False)
        out += [(a + w.start, norm(w.word)) for s in segs for w in (s.words or []) if norm(w.word)]
    return out


def fit(cues, words):
    toks = [w for _, w in words]
    pairs = []
    for c in cues:
        ws = [norm(x) for x in c.text.split() if norm(x)]
        if len(ws) < 3:
            continue
        t0 = c.start_ms / 1000
        for j in range(len(toks) - 2):
            if toks[j:j + 3] == ws[:3] and abs(words[j][0] - t0) < 120:
                pairs.append((t0, words[j][0]))
                break
    if len(pairs) < 10:
        return None
    import numpy as np
    x, y = np.array([p[0] for p in pairs]), np.array([p[1] for p in pairs])
    a, b = np.polyfit(x, y, 1)
    res = np.abs(y - (a * x + b))
    keep = res < 1.5                                       # drop chance matches, refit
    if keep.sum() >= 10:
        a, b = np.polyfit(x[keep], y[keep], 1)
        res = np.abs(y[keep] - (a * x[keep] + b))
    return dict(n=int(keep.sum()), a=float(a), b=float(b), med=float(np.median(res)))


def write_srt(cues, a, b, out):
    def ts(t):
        t = max(0.0, t)
        h, r = divmod(int(round(t * 1000)), 3600000)
        m, r = divmod(r, 60000)
        s, ms = divmod(r, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    with open(C.lp(out + ".tmp"), "w", encoding="utf-8") as f:
        for i, c in enumerate(cues, 1):
            f.write(f"{i}\n{ts(a * c.start_ms / 1000 + b)} --> {ts(a * c.end_ms / 1000 + b)}\n{c.text}\n\n")
    os.replace(C.lp(out + ".tmp"), C.lp(out))       # atomic: a worker never reads half a subtitle file


def save_report(rep):
    """atomic: the supervisor reads this file every 10 min (a half-written one broke its status)."""
    tmp = REPORT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    os.replace(tmp, REPORT)


def main():
    open(os.path.join(C.HERE, "subs.pid"), "w").write(str(os.getpid()))
    rep = json.load(open(REPORT, encoding="utf-8")) if os.path.exists(REPORT) else {}
    for sub_dir in sorted(os.listdir(SUBS)):
        if not os.path.isdir(os.path.join(SUBS, sub_dir)):
            continue
        roots = library_roots(sub_dir)
        if not roots:
            log(f"== {sub_dir}: no such show in the library - skipped")
            continue
        cands, films = {}, []
        for p in glob.glob(os.path.join(SUBS, sub_dir, "**", "*.srt"), recursive=True):
            e = ep_of(os.path.basename(p))
            if e:
                cands.setdefault(e, []).append(p)
            else:
                films.append(p)
        vids = sorted(v for r in roots for v in glob.glob(os.path.join(r, "**", "*"), recursive=True)
                      if v.lower().endswith(VID) and os.sep + "Featurettes" not in v and "sample" not in v.lower()
                      and not any(k in v for k in SKIP_ROOTS)
                      and (ep_of(os.path.basename(v)) or (films and os.path.getsize(C.lp(v)) > 700_000_000)))
        log(f"== {sub_dir} -> {[os.path.basename(r) for r in roots]}: {len(vids)} videos, {sum(len(x) for x in cands.values())} episode srt, {len(films)} film srt")
        for v in vids:
            try:
                one(v, rep, cands, films)
            except Exception as ex:                       # one odd file must not end the whole run (it did, every run,
                rep[v] = dict(status="error", why=f"{type(ex).__name__}: {str(ex)[:120]}")   # after Supernatural S10E05)
                log(f"   ERROR {os.path.basename(v)[:50]}: {traceback.format_exc().splitlines()[-1][:160]}")
            save_report(rep)
    save_report(rep)
    open(os.path.join(C.HERE, "subs_done.txt"), "w").write(time.strftime("%Y-%m-%d %H:%M"))
    log("== done")


def one(v, rep, cands, films):
    if v in rep and rep[v]["status"] in ("installed", "no-subtitle", "error"):
        return
    ep = ep_of(os.path.basename(v))
    if ep:
        s, e = ep
        pool = [(p, d) for d in (0, -1, 1) for p in cands.get((s, e + d), [])]
        tag = f"S{s:02d}E{e:02d}"
    else:                                         # a film: every subtitle without an episode code is a candidate
        pool = [(p, 0) for p in films]
        tag = os.path.basename(os.path.dirname(v))[:50]
    if not pool:
        rep[v] = dict(status="no-subtitle", ep=tag)
        return
    if v in rep and rep[v]["status"] == "rejected" and \
            sorted(rep[v].get("tried", [])) == sorted(os.path.basename(p) for p, _ in pool):
        return                                    # same candidates already failed the sync check: no new whisper pass
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                C.lp(v)], capture_output=True, text=True, timeout=180).stdout.strip())
    words = heard_words(v, dur)
    best = None
    for attempt in range(2):
        for p, d in pool:
            cues = subtitles.parse_file(p)
            f = fit(cues, words) if cues else None
            if f and abs(f["a"] - 1) <= 0.05 and f["med"] <= 0.35 and (not best or f["n"] > best[1]["n"]):
                best = (p, f, d, cues)
        if best or attempt:
            break
        # quiet episode (few lines in the 4 windows): listen to 8 more windows before rejecting
        words = words + heard_words(v, dur, (0.07, 0.25, 0.31, 0.45, 0.53, 0.70, 0.77, 0.93))
    if not best:
        rep[v] = dict(status="rejected", ep=tag, tried=[os.path.basename(p) for p, _ in pool])
        log(f"   REJECT {os.path.basename(v)[:50]} (no candidate in sync)")
        return
    p, f, d, cues = best
    out = os.path.splitext(v)[0] + ".en.srt"
    write_srt(cues, f["a"], f["b"], out)
    try:                                           # let the worker swap whisper text for the subtitle
        os.remove(os.path.join(C.DONE, C.key(v)))
    except OSError:
        pass
    rep[v] = dict(status="installed", ep=tag, srt=os.path.basename(p), shift=round(f["b"], 2),
                  stretch=round(f["a"], 4), matched=f["n"], residual=round(f["med"], 2), neighbour=d)
    log(f"   ok {os.path.basename(v)[:50]} <- {os.path.basename(p)[:50]} shift {f['b']:+.2f}s x{f['a']:.4f} "
        f"({f['n']} cues, {f['med']:.2f}s){'  NUMBER SHIFT ' + str(d) if d else ''}")


if __name__ == "__main__":
    main()
