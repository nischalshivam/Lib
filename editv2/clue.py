"""Grounded clue builder — written by agy Claude through the proxy, NEVER by the
interactive session. Per 12-line beat:
  RESOLVE  model: which show / episode / scene each line refers to + what is SAID there
  GROUND   code: find it in OUR catalog dialogue (IDF-weighted; a wrong episode guess
           still lands on the right scene anywhere in the show)
  PLAN     model: per line, real shot ids from the grounded windows only + edit intent
Output clue_v2.json in the build folder."""
from __future__ import annotations

import concurrent.futures as cf, glob, json, math, os, random, re, time

from .common import llm, lp, text_model, mark_unusable

SE = re.compile(r"S(\d{2})E(\d{2})|\b(\d{1,2})x(\d{2})\b", re.I)
VEXT = (".mkv", ".mp4", ".avi", ".m4v")


def _ep_code(fname):
    m = SE.search(fname)
    if not m:
        return None
    if m.group(1):
        return f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}"
    return f"S{int(m.group(3)):02d}E{int(m.group(4)):02d}"


class Library:
    """Every catalogued episode/film of the context's shows."""

    def __init__(self, shows):
        self.eps, self.show_name = {}, {}
        for sh in shows:
            self.show_name[sh["key"]] = sh["name"]
            films = 0
            for f in sorted(glob.glob(os.path.join(sh["root"], "**", "*.catalog.json"), recursive=True)):
                base = os.path.basename(f)
                ep = _ep_code(base)
                title = ""
                if not ep:
                    films += 1
                    ep = f"F{films:02d}"
                    title = re.sub(r"[._]+", " ", re.sub(r"\.catalog\.json$", "", base))[:60]
                else:
                    tm = re.search(r"(?:S\d{2}E\d{2}|\d{1,2}x\d{2})\s*-\s*(.+?)\s*[\(\[]", base)
                    title = tm.group(1) if tm else ""
                key = (sh["key"], ep)
                if key in self.eps:
                    continue
                try:
                    shots = mark_unusable(json.load(open(lp(f), encoding="utf-8"))["shots"])
                except Exception:
                    continue
                stem = f[: -len(".catalog.json")]
                video = next((stem + e for e in VEXT if os.path.exists(lp(stem + e))), None)
                if not video:
                    continue
                for i, s in enumerate(shots):
                    s["sid"] = f"{sh['key']}:{ep}_{i:03d}"
                    s["ep"] = key
                self.eps[key] = {"video": video, "catalog": f, "title": title, "shots": shots}
        self.by_sid = {s["sid"]: s for e in self.eps.values() for s in e["shots"]}

    def episode_list(self, key):
        ks = sorted(k for k in self.eps if k[0] == key)
        return ", ".join(k[1] + (f" {self.eps[k]['title']}" if self.eps[k]["title"] else "") for k in ks)


def _toks(s):
    return [w for w in re.findall(r"[a-z0-9']+", (s or "").lower()) if len(w) > 2]


class Grounder:
    def __init__(self, lib):
        self.lib, self.df = lib, {}

    def _df(self, key):
        if key not in self.df:
            df, n = {}, 0
            for k, e in self.lib.eps.items():
                if k[0] != key:
                    continue
                for x in e["shots"]:
                    n += 1
                    for w in set(_toks((x.get("dialogue") or "") + " " + x.get("description", ""))):
                        df[w] = df.get(w, 0) + 1
            self.df[key] = (df, n)
        return self.df[key]

    def ground(self, key, ep, quote, keywords):
        if key not in {k[0] for k in self.lib.eps}:
            return None
        q = set(_toks(quote)) | set(t for k in (keywords or []) for t in _toks(k))
        if not q:
            return None
        df, n = self._df(key)
        wt = {w: math.log((n + 1) / (df.get(w, 0) + 1)) for w in q}
        tot = sum(wt.values()) or 1
        keys = [k for k in self.lib.eps if k[0] == key]
        near = []
        if ep:
            ep = ep.upper()
            m = re.match(r"S(\d{2})E(\d{2})", ep)
            near = [k for k in keys if k[1] == ep]
            if m:
                s_, e_ = int(m.group(1)), int(m.group(2))
                near += [k for k in keys if re.match(r"S\d{2}E\d{2}", k[1]) and int(k[1][1:3]) == s_
                         and 0 < abs(int(k[1][4:6]) - e_) <= 2]
        best = None
        for tier, pool in ((0, near), (1, keys)):
            for k in pool:
                sh = self.lib.eps[k]["shots"]
                for i in range(len(sh)):
                    txt = set(_toks(" ".join((sh[j].get("dialogue") or "") + " " + sh[j].get("description", "")
                                             for j in range(i, min(i + 3, len(sh))))))
                    sc = sum(wt[w] for w in q & txt) / tot - (0.08 if tier else 0)
                    if best is None or sc > best[2]:
                        best = (k, sh[i]["start"], sc)
            if best and best[2] >= 0.5:
                break
        return best if best and best[2] >= 0.38 else None


def split_lines(text):
    out = []
    for para in [p.strip() for p in text.split("\n") if p.strip()]:
        out += [p.strip() for p in re.split(r'(?<=[.?!…])\s+(?=[A-ZÁÉÍÓÚÑÇÀÂÊÔÃÕÜÖÄ¿¡"“«])', para) if p.strip()]
    return out


RESOLVE_SYS = """You are a meticulous TV/film research assistant for a video-essay editor.
For each numbered script line, decide whether it refers to a SPECIFIC moment in one of the titles below.
The script may be in any language; the footage and its dialogue are in the ORIGINAL language of the show.
Titles (key = name): {shows}
Episode / film lists (use these codes only):
{eplists}
Reply ONLY JSON: {{"refs":[{{"line":<n>,"show":"<title key>","ep":"<code>","quote":"<what a character actually SAYS there, in the show's original language, as verbatim as you can>","keywords":["<3-6 distinctive words spoken or seen in that scene, original language>"],"what":"<what is on screen>","confidence":"high|medium|low"}}]}}
Rules: only lines that describe a concrete scene/event; lines continuing the same scene may repeat the ref; if unsure of the episode, lower the confidence instead of inventing."""

PLAN_SYS = """You are the senior editor of a top-tier TV/film video-essay channel. Plan, line by line, what the
viewer SEES, using ONLY the real candidate shot ids given. The script language is {lang}; any on-screen words you
write (kinetic, label role, fact text) MUST be in {lang} and taken from / faithful to the line.
Every line gets an entry. Types:
- "story": a concrete moment -> "shots": 1-3 ids from that exact scene, in order.
- "intro": a character/person/title is named for the first time -> "label": "NAME — 3-6 word role" and 1-2 shots where that character is clearly visible.
- "compare": the line contrasts two titles or two characters -> "split": [left_id, right_id] (left = the one named first).
- "commentary": opinion/analysis -> "kinetic": 2-5 punchy words from the line, AND 1-2 calm shots of the subject discussed.
- "fact": a real-world fact/statistic/source -> "fact": {{"text":"<short>","source":"<org>"}} plus optional shots.
- "quote": the line quotes/paraphrases dialogue that exists in a candidate shot -> "soundup": that shot id.
Optional: "emphasis": one word of the line, "annotate": {{"shot": id, "text": "2-4 words"}}.
Hard rules: each shot id at most once in the beat; never a shot from another title on a line about one title; if nothing fits, "shots": [] (a designed card is used) rather than a wrong shot.
Reply ONLY JSON: {{"lines":[{{"line":<n>,"type":"...","shots":[...], ...}}]}}"""

LANG_NAME = {"en": "English", "es": "Spanish", "pt": "Portuguese", "fr": "French", "de": "German"}


def _norm(s):
    return " ".join(re.findall(r"[a-z0-9']+", str(s or "").lower()))


def refs_path(ctx):
    """refs written OUTSIDE the tool (ChatGPT while writing the script): '<script>.refs.json' or 'refs.json' next to
    the script. 2026-10-17: agy Claude is going away; ChatGPT knows the episodes + exact quotes far better than Gemini
    Flash (A/B: episode right 92-99% vs ~45%), and only the PLAN step needs our local shot list."""
    if ctx.get("refs_file"):
        return ctx["refs_file"] if os.path.exists(ctx["refs_file"]) else None
    stem, topic = os.path.splitext(ctx["clean"])[0], os.path.dirname(ctx["clean"])
    return next((p for p in (stem + ".refs.json", os.path.join(topic, "refs.json")) if os.path.exists(p)), None)


def external_refs(path, ctx, lines, lib, log=print):
    """{"refs":[{"line":"<verbatim start of the script sentence>","show":"<title>","ep":"S02E06" or film title,
    "quote":"...","keywords":[...],"what":"...","confidence":"high|medium|low"}]} -> {line index: [refs]}"""
    data = json.load(open(path, encoding="utf-8-sig"))
    refs = data.get("refs", []) if isinstance(data, dict) else data
    names = {}
    for s in ctx["shows"]:
        names[_norm(s["key"])] = s["key"]
        names[_norm(s["name"])] = s["key"]
    nl = [_norm(x) for x in lines]
    out, cur, miss = {}, 0, []

    def show_key(v):
        v = _norm(v)
        if v in names:
            return names[v]
        hit = [k for n, k in names.items() if n and (n in v or v and v in n)]
        if hit:
            return hit[0]
        return ctx["shows"][0]["key"] if len(ctx["shows"]) == 1 else None

    def ep_code(key, v):
        m = re.search(r"S(\d{1,2})\s*E(\d{1,3})|\b(\d{1,2})x(\d{1,3})\b", str(v or ""), re.I)
        if m:
            return f"S{int(m.group(1) or m.group(3)):02d}E{int(m.group(2) or m.group(4)):02d}"
        films = [(k[1], set(_toks(e["title"]))) for k, e in lib.eps.items() if k[0] == key and k[1].startswith("F")]
        q = set(_toks(v))
        best = max(films, key=lambda f: len(q & f[1]), default=None)
        return best[0] if best and q & best[1] else None

    def line_of(v):
        nonlocal cur
        if isinstance(v, int) or str(v).strip().isdigit():
            return int(v) - 1 if 0 < int(v) <= len(lines) else None
        t = _norm(v)
        if not t:
            return None
        for rng in (range(cur, len(nl)), range(0, cur)):
            for j in rng:
                two = nl[j] + " " + nl[j + 1] if j + 1 < len(nl) else nl[j]      # "Mr. Mayor" splits our line
                if nl[j].startswith(t[:60]) or t in nl[j] or (len(nl[j]) > 20 and nl[j] in t) or two.startswith(t[:60]):
                    cur = j
                    return j
        tt = set(t.split())
        sc = [(len(tt & set(x.split())) / max(1, len(tt)), j) for j, x in enumerate(nl)]
        best = max(sc, default=(0, None))
        return best[1] if best[0] >= 0.6 else None

    for r in refs if isinstance(refs, list) else []:
        if not isinstance(r, dict):
            continue
        j = line_of(r.get("line"))
        if j is None:
            miss.append(str(r.get("line"))[:50])
            continue
        key = show_key(r.get("show"))
        out.setdefault(j, []).append(dict(r, line=j + 1, show=key, ep=ep_code(key, r.get("ep")) if key else None,
                                          src_ep=r.get("ep")))
    log(f"  refs from {os.path.basename(path)}: {sum(len(v) for v in out.values())} refs on {len(out)} lines"
        + (f", {len(miss)} not matched to a script line (e.g. {miss[0]!r})" if miss else ""))
    return out


def build(ctx: dict, log=print) -> str:
    out_json = os.path.join(ctx["build"], "clue_v2.json")
    if os.path.exists(out_json):
        log("  clue_v2.json exists — reused")
        return out_json
    lib = Library([s for s in ctx["shows"] if s.get("indexed")])
    G = Grounder(lib)
    log(f"  library: {len(lib.eps)} episodes/films, {len(lib.by_sid)} shots")
    lines = split_lines(open(ctx["clean"], encoding="utf-8-sig").read())
    B = [list(range(i, min(i + 12, len(lines)))) for i in range(0, len(lines), 12)]
    rp = refs_path(ctx)
    ext = external_refs(rp, ctx, lines, lib, log) if rp else None
    log(f"  script: {len(lines)} lines in {len(B)} beats; model {text_model()} (agy)"
        + (" - refs from the script writer, model only plans shots" if ext is not None else ""))
    shows_txt = ", ".join(f"{s['key']} = {s['name']}" for s in ctx["shows"])
    eplists = "\n".join(f"- {s['key']}: {lib.episode_list(s['key'])}" for s in ctx["shows"])
    sys_resolve = RESOLVE_SYS.format(shows=shows_txt, eplists=eplists[:60000])
    sys_plan = PLAN_SYS.format(lang=LANG_NAME.get(ctx.get("language", "en"), "English"))
    names_all = {n for s in ctx["shows"] for n in s.get("first_names", [])}
    text_all = " ".join(lines)
    common_names = sorted(names_all, key=lambda n: -text_all.count(n))[:3]
    results = [None] * len(B)
    models_used = []                    # 2026-10-16: the chain may move to Gemini (agy drops Claude 11-02)

    def do_beat(bi):
        idx = B[bi]
        before = "\n".join(f"(context) {lines[j]}" for j in range(max(0, idx[0] - 6), idx[0]))
        body = "\n".join(f"{j + 1}. {lines[j]}" for j in idx)
        if ext is not None:
            refs, m1 = {"refs": [r for j in idx for r in ext.get(j, [])]}, "refs.json"
        else:
            refs, m1 = llm(sys_resolve, f"{before}\n\nLINES:\n{body}")
        anchors = []
        for r in refs.get("refs", []):
            g = G.ground(r.get("show"), r.get("ep"), r.get("quote"), r.get("keywords"))
            r["grounded"] = None if not g else {"show": g[0][0], "ep": g[0][1], "t": round(g[1], 1), "score": round(g[2], 2)}
            if g:
                anchors.append((g[0], g[1]))
        cands = {}
        for key, t in anchors:
            for s in lib.eps[key]["shots"]:
                if t - 45 <= s["start"] <= t + 45:
                    cands[s["sid"]] = s
        want = {w for w in re.findall(r"\b[A-ZÁÉÍÓÚÑ][\w'’-]+", body + " " + before) if w in names_all} or set(common_names)
        for key in {a[0] for a in anchors}:
            for s in lib.eps[key]["shots"]:
                if len(cands) > 220:
                    break
                if {c.split()[0] for c in s.get("characters", []) if c} & want and s.get("shot_type") in ("close-up", "medium"):
                    cands.setdefault(s["sid"], s)
        if len(cands) < 60:
            rnd = random.Random(bi)
            keys = list(lib.eps)
            rnd.shuffle(keys)
            for key in keys:
                if len(cands) >= 140:
                    break
                for s in lib.eps[key]["shots"]:
                    chars = " ".join(s.get("characters", []))
                    if any(w in chars for w in want) and s.get("shot_type") in ("close-up", "medium") \
                            and s.get("safe", True) and 1 <= len(s.get("characters", [])) <= 2 and rnd.random() < 0.08:
                        cands.setdefault(s["sid"], s)
        cl = "\n".join(f"{s['sid']} | {lib.show_name[s['ep'][0]]} {s['ep'][1]} {s['start']:.0f}s | {','.join(s.get('characters', []))} | "
                       f"{s['description'][:90]} | \"{(s.get('dialogue') or '')[:70]}\""
                       for s in sorted(cands.values(), key=lambda s: (s['ep'], s['start'])))
        plan, m2 = llm(sys_plan, f"SCRIPT LINES:\n{body}\n\nGROUNDED REFERENCES:\n{json.dumps(refs.get('refs', []), ensure_ascii=False)[:6000]}"
                                 f"\n\nCANDIDATE SHOTS ({len(cands)}):\n{cl or '(none)'}")
        models_used.extend([m1, m2])
        return bi, refs, plan, len(cands)

    t0 = time.time()
    with cf.ThreadPoolExecutor(4) as ex:
        for bi, refs, plan, nc in ex.map(do_beat, range(len(B))):
            results[bi] = (refs, plan)
            g = sum(1 for r in refs.get("refs", []) if r.get("grounded"))
            log(f"  beat {bi + 1}/{len(B)}: refs {len(refs.get('refs', []))} grounded {g}, candidates {nc} ({time.time() - t0:.0f}s)")

    def _id(x):
        if isinstance(x, dict):
            x = x.get("id") or x.get("shot") or next((v for v in x.values() if isinstance(v, str)), "")
        return str(x).strip() if x else ""

    out, used = [], set()
    for bi, idx in enumerate(B):
        refs, plan = results[bi]
        byline = {}
        for p in plan.get("lines", []):
            try:
                byline[int(p.get("line", 0)) - 1] = p
            except (TypeError, ValueError):
                pass
        for j in idx:
            p = byline.get(j, {"type": "commentary", "shots": []})
            p["shots"] = [_id(x) for x in (p.get("shots") or [])]
            if p.get("split"):
                p["split"] = [_id(x) for x in p["split"]][:2]
            if p.get("soundup"):
                p["soundup"] = _id(p["soundup"])
            if isinstance(p.get("annotate"), dict) and p["annotate"].get("shot"):
                p["annotate"]["shot"] = _id(p["annotate"]["shot"])
            shots = [s for s in p["shots"] if s in lib.by_sid and s not in used]
            used.update(shots)
            if not shots and p.get("soundup") in lib.by_sid:
                shots = [p["soundup"]]
            p["shots"] = shots
            p["text"] = lines[j]
            p["refs"] = [r for r in refs.get("refs", []) if str(r.get("line", "")).isdigit() and int(r["line"]) - 1 == j]
            out.append(p)
    need = {sid for p in out for sid in p["shots"] + (p.get("split") or []) + ([p["soundup"]] if p.get("soundup") else [])
            if sid in lib.by_sid}
    shots = {}
    for sid in need:
        s = lib.by_sid[sid]
        shots[sid] = {k: s.get(k) for k in ("start", "end", "description", "characters", "dialogue", "safe", "shot_type")}
        shots[sid]["ep"] = list(s["ep"])
        shots[sid]["video"] = lib.eps[s["ep"]]["video"]
        shots[sid]["catalog"] = lib.eps[s["ep"]]["catalog"]
    refs_all = [r for p in out for r in p["refs"]]
    stats = dict(lines=len(out), refs=len(refs_all), grounded=sum(1 for r in refs_all if r.get("grounded")),
                 no_visual=sum(1 for p in out if not p["shots"] and not p.get("split")))
    json.dump({"model": ", ".join(sorted(set(models_used))) or text_model(), "stats": stats, "lines": out, "shots": shots},
              open(out_json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(f"  clue done in {time.time() - t0:.0f}s: {stats}")
    return out_json
