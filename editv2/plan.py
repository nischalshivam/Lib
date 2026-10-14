"""Line plan: clue_v2 + voice words -> verified EDL (plan.json).
align lines to spoken words -> scene grammar per line -> Gemini re-anchor (4x4 grid,
named person must be visible, no burned-in text) -> real cut ranges (ffmpeg) ->
sound-ups (replace for English scripts, insert after the line for other languages)."""
from __future__ import annotations

import concurrent.futures as cf, difflib, io, json, os, re, subprocess

from . import assets
from .common import lp, vision

FONT_NUM = r"C:\Windows\Fonts\arialbd.ttf"
# Gemini re-anchor calls at once per video (2026-10-14: 8 -> 16; one call ~16 s, the proxy took 128 at once while indexing)
VERIFY_THREADS = int(os.environ.get("EDITV2_VERIFY_THREADS", "16"))


def norm(s):
    return re.sub(r"[\W_]+", "", (s or "").lower())


# punch-ins / sound-ups play the show's ORIGINAL audio: strong profanity anywhere, and milder swearing in the first
# 3 minutes, costs ad suitability (YouTube weighs the opening hardest). Reacher 02 opened with 'fucked' at 0:55.
STRONG_SWEAR = re.compile(r"\b(fuck\w*|f\*+\w*|motherf\w*|cunt\w*|nigg\w*|fagg?ot\w*|retard\w*)", re.I)
MILD_SWEAR = re.compile(r"\b(\w*shit\b|\w*shits\b|\w*shitty\b|bitch\w*|bastard\w*|\w*asshole\w*|dick\b|dicks\b|pussy|cock\b|"
                        r"whore\w*|slut\w*|goddamn\w*)", re.I)


def unsafe_line(text, t):
    """True if a line of original dialogue must not be played at video time t (s)."""
    return bool(STRONG_SWEAR.search(text or "") or (t < 180 and MILD_SWEAR.search(text or "")))


class Plan:
    def __init__(self, ctx):
        self.ctx = ctx
        self.B = ctx["build"]
        clue = json.load(open(os.path.join(self.B, "clue_v2.json"), encoding="utf-8"))
        self.LINES, self.SH = clue["lines"], clue["shots"]
        self._clue_sids = set(self.SH)
        # shots the cross-show guard took from the full catalogue live in plan.json, not in the clue - cut / look / QA
        # build their own Plan(ctx) and must know them (KeyError in cut, MITM vs Young Sheldon test 2026-10-13)
        try:
            self.SH.update(json.load(open(os.path.join(self.B, "plan.json"), encoding="utf-8")).get("extra_shots") or {})
        except (OSError, ValueError):
            pass
        self.W = [tuple(w) for w in json.load(open(os.path.join(self.B, "words.json"), encoding="utf-8"))]
        self.show = {s["key"]: s for s in ctx["shows"]}
        self.SPANS = self._align()
        W = self.W
        self.LT = [(W[a][0] if a < len(W) else W[-1][1], W[b][0] if b < len(W) else W[-1][1] + 0.4) for a, b in self.SPANS]

    # ---------------------------------------------------------------- timing
    def _align(self):
        st, owner = [], []
        for i, p in enumerate(self.LINES):
            for t in p["text"].split():
                if norm(t):
                    st.append(norm(t)); owner.append(i)
        sm = difflib.SequenceMatcher(None, st, [norm(w[2]) for w in self.W], autojunk=False)
        s2w = {}
        for a, b, n in sm.get_matching_blocks():
            for k in range(n):
                s2w[a + k] = b + k
        first = {}
        for k, i in enumerate(owner):
            if i not in first and k in s2w:
                first[i] = s2w[k]
        starts, last = [], 0
        for i in range(len(self.LINES)):
            s = first.get(i)
            s = last if s is None or s < last else s
            starts.append(s)
            last = s
        return [(starts[i], starts[i + 1] if i + 1 < len(starts) else len(self.W)) for i in range(len(starts))]

    def words_of(self, i):
        a, b = self.SPANS[i]
        return self.W[a:b]

    def snap(self, t, i):
        ws = self.words_of(i)
        return min((w[0] for w in ws), key=lambda x: abs(x - t)) if ws else t

    def video(self, sid):
        return self.SH[sid]["video"]

    def names_in(self, text, key):
        return [n for n in self.show[key]["first_names"] if re.search(rf"\b{re.escape(n)}\b", text)]

    # ---------------------------------------------------------------- cross-show guard
    # MITM vs Young Sheldon (2026-10) showed Sheldon while the narration talked about Malcolm: a line without its own
    # shot simply EXTENDED the previous shot, whatever show it came from. Single-show videos never notice this.
    def line_owner(self, i):
        """the ONE show whose characters a narration line names (None: no names, or several shows = a comparison)."""
        if len(self.show) < 2:
            return None
        own = [k for k in self.show if self.names_in(self.LINES[i]["text"], k)]
        return own[0] if len(own) == 1 else None

    def shows_of_item(self, e):
        sids = [e[k] for k in ("sid", "left", "right") if k in e and e[k] in self.SH]
        return {self.SH[s]["ep"][0] for s in sids}

    def owner_shot(self, key, i, used):
        """a catalogue shot of the line's show showing the named character (verified at indexing against the Cast
        photos), preferring episodes this video already uses; None if there is none."""
        names = self.names_in(self.LINES[i]["text"], key)
        eps = {tuple(self.SH[s]["ep"]) for s in used if s in self.SH and self.SH[s]["ep"][0] == key}

        def pick(pool):
            best = None
            for sid, s in pool:
                if tuple(s["ep"])[0] != key or sid in used or not s.get("safe", True) or s["end"] - s["start"] < 2.0:
                    continue
                chars = s.get("characters") or []
                if not chars or len(chars) > 2 or s.get("shot_type") not in ("close-up", "medium"):
                    continue
                if names and not any(c.split()[0] in names for c in chars):
                    continue
                rank = (tuple(s["ep"]) not in eps, len(chars), -(s["end"] - s["start"]), (hash(sid) + i) % 997)
                if best is None or rank < best[0]:
                    best = (rank, sid, s)
            return best

        # 1) the clue's own shot pool; 2) the WHOLE catalogue of that show (the clue pool is only the ~100-200 shots
        # the clue used, all already taken by other lines - the MITM vs Young Sheldon test found nothing there)
        b = pick(self.SH.items())
        if not b:
            lib = self.library()
            b = pick(((s["sid"], s) for e in lib.eps.values() if e["shots"] and e["shots"][0]["ep"][0] == key
                      for s in e["shots"]))
            if b:
                e = lib.eps[tuple(b[2]["ep"])]
                self.SH[b[1]] = dict(b[2], ep=list(b[2]["ep"]), video=e["video"], catalog=e["catalog"])
        if b:
            used.add(b[1])
        return b[1] if b else None

    def library(self):
        """every catalogued episode of the script's shows (loaded once, only when the cross-show guard needs it)."""
        if not hasattr(self, "_lib"):
            from .clue import Library
            self._lib = Library(self.ctx["shows"])
        return self._lib

    # ---------------------------------------------------------------- verify
    def _grid(self, sid, step=0.8, n=16):
        from PIL import Image, ImageDraw, ImageFont
        s = self.SH[sid]
        a = max(0.0, s["start"] - 5.0)
        ts = [a + k * step for k in range(n)]
        f = ImageFont.truetype(FONT_NUM, 34)
        G = Image.new("RGB", (4 * 384, 4 * 216))
        for k, t in enumerate(ts):
            r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", lp(self.video(sid)), "-frames:v", "1",
                                "-vf", "scale=384:216", "-f", "image2", "-c:v", "mjpeg", "-"], capture_output=True)
            try:
                im = Image.open(io.BytesIO(r.stdout)).convert("RGB")
            except Exception:
                im = Image.new("RGB", (384, 216))
            d = ImageDraw.Draw(im)
            d.rectangle([0, 0, 52, 42], fill="black")
            d.text((6, 2), str(k + 1), font=f, fill="yellow")
            G.paste(im, ((k % 4) * 384, (k // 4) * 216))
        b = io.BytesIO()
        G.save(b, "JPEG", quality=80)
        return b.getvalue(), ts

    def reanchor(self, sid, line):
        s = self.SH[sid]
        key = s["ep"][0]
        g, ts = self._grid(sid)
        must = self.names_in(line, key)
        q = (f'Narration line in a video essay: "{line}"\n'
             + (f"The chosen frame MUST clearly show {' or '.join(must)}'s face. " if must else "")
             + f'The editor wants a shot from {self.show[key]["name"]} {s["ep"][1]}: {s["description"]}\n'
             f'Dialogue spoken around here: "{(s.get("dialogue") or "")[:160]}"\n'
             "16 numbered frames, 0.8 s apart. Pick the frame where a clip under this line should START (right people / "
             "action; a person SAYING the quoted dialogue counts). Never pick a frame with burned-in text, credits, captions "
             "or logos on the picture. Pick 0 only if no frame fits. Reply ONLY JSON {\"pick\":0-16,\"score\":0-10,\"why\":\"<short>\"}")
        v = vision(q, g) or {"pick": 0, "score": 5, "why": "verify-error"}
        try:
            k = int(v.get("pick") or 0)
        except (TypeError, ValueError):
            k = 0
        v["t"] = ts[k - 1] if 1 <= k <= len(ts) else s["start"]
        return v

    def real_range(self, sid, anchor=None):
        s = dict(self.SH[sid])
        if anchor is not None:
            s["start"], s["end"] = anchor, anchor + max(s["end"] - s["start"], 3.0)
        a, b = max(0, s["start"] - 0.6), s["end"] + 2.5
        r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{a:.2f}", "-t", f"{b - a:.2f}", "-i", lp(self.video(sid)),
                            "-vf", "scale=320:-2,select='gt(scene,0.25)',showinfo", "-an", "-f", "null", "-"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        cuts = [a + float(x) for x in re.findall(r"pts_time:([0-9.]+)", r.stderr)]
        mid = s["start"] + 0.3
        c0 = max([c for c in cuts if c <= mid] or [s["start"]])
        c1 = min([c for c in cuts if c > mid + 0.3] or [s["end"]])
        return c0 + 0.06, c1 - 0.06

    # ---------------------------------------------------------------- grammar
    def grammar(self):
        edl, over, seen_names, cut_names = [], [], set(), set()
        kin_toggle, cutouts = 0, []
        used = {s for p in self.LINES for s in (p.get("shots") or []) + (p.get("split") or []) if s in self.SH}
        log_x = self.cross_log = []
        for i, p in enumerate(self.LINES):
            t0, t1 = self.LT[i]
            txt, typ = p["text"], p.get("type")
            split = [s for s in (p.get("split") or []) if s in self.SH]
            shots = [s for s in p.get("shots", []) if s in self.SH]
            good_split = len(split) == 2 and split[0] != split[1] and (
                self.SH[split[0]]["ep"][0] != self.SH[split[1]]["ep"][0]
                or set(self.SH[split[0]].get("characters", [])) != set(self.SH[split[1]].get("characters", [])))
            own = self.line_owner(i)
            if own and shots and all(self.SH[s]["ep"][0] != own for s in shots):
                # the clue put another show's footage under a line about THIS show's character
                alt = self.owner_shot(own, i, used)
                log_x.append(f"line {i + 1}: clue shot(s) from {self.SH[shots[0]]['ep'][0]} under a {own} line -> "
                             f"{alt or 'text card'}")
                shots = [alt] if alt else []
            if good_split:
                edl.append(dict(kind="SPLIT", t0=t0, t1=t1, left=split[0], right=split[1], line=i))
            elif shots:
                n = len(shots)
                cuts = [t0] + [self.snap(t0 + (t1 - t0) * k / n, i) for k in range(1, n)] + [t1]
                for k, sid in enumerate(shots):
                    if cuts[k + 1] - cuts[k] > 0.35:
                        edl.append(dict(kind="V", t0=cuts[k], t1=cuts[k + 1], sid=sid, line=i))
            elif typ == "commentary" and len(txt.split()) <= 6:
                edl.append(dict(kind="BREATH", t0=t0, t1=t1, text=txt, line=i))
            elif edl and edl[-1]["kind"] in ("V", "SPLIT") and own and own not in self.shows_of_item(edl[-1]):
                # no own shot and the previous shot is ANOTHER show: never let it run on under this line
                alt = self.owner_shot(own, i, used)
                log_x.append(f"line {i + 1}: would have continued {sorted(self.shows_of_item(edl[-1]))} footage under a "
                             f"{own} line -> {alt or 'text card'}")
                edl.append(dict(kind="V", t0=t0, t1=t1, sid=alt, line=i) if alt else
                           dict(kind="BREATH", t0=t0, t1=t1, text=txt, line=i))
            elif edl and edl[-1]["kind"] in ("V", "SPLIT"):
                edl[-1]["t1"] = t1
            else:
                edl.append(dict(kind="BREATH", t0=t0, t1=t1, text=txt, line=i))
            used.update(e.get("sid") for e in edl[-3:] if e.get("sid"))
            kin = p.get("kinetic")
            kin = " ".join(kin) if isinstance(kin, list) else kin
            if kin and edl and edl[-1]["kind"] in ("V", "SPLIT") and typ == "commentary":
                kin_toggle += 1
                over.append(dict(kind="KIN", t0=t0, t1=t1, text=kin, line=i, side=kin_toggle % 2))
            if p.get("emphasis") and edl and edl[-1]["kind"] == "V":
                em = norm(p["emphasis"])
                w = next((w for w in self.words_of(i) if norm(w[2]) == em), None)
                if w:
                    over.append(dict(kind="PUNCH", t=w[0], line=i))
            lab = p.get("label")
            if lab and typ == "intro":
                name = re.split(r"\s+[—–-]\s+", lab)[0].strip()
                if name and name not in seen_names:
                    seen_names.add(name)
                    role = re.split(r"\s+[—–-]\s+", lab, maxsplit=1)[1].strip() if re.search(r"\s+[—–-]\s+", lab) else ""
                    over.append(dict(kind="LABEL", t0=t0, t1=min(t1 + 1.6, t0 + 3.6), name=name.upper(), role=role, line=i))
                    # first introduction of a character with a Cast photo -> cutout spotlight
                    for sh in self.ctx["shows"]:
                        full = next((c for c in sh["cast"] if c.split()[0].lower() == name.split()[0].lower()), None)
                        if full and full not in cut_names and (not cutouts or t0 - cutouts[-1] >= 45) and len(cutouts) < 6                                 and sh.get("cast_dir") and assets.cutout(sh["key"], sh["cast_dir"], full):
                            dur = max(1.8, min(2.8, t1 - t0 + 0.8))
                            cutouts.append(t0)
                            cut_names.add(full)
                            edl.append(dict(kind="CUTOUT", t0=t0, t1=t0 + dur, name=full.upper() if sh["style"]["upper"] else full,
                                            show=sh["key"], photo=full, line=i))
                            break
        return edl, over

    @staticmethod
    def resolve_overlaps(edl):
        edl.sort(key=lambda e: (e["t0"], e["kind"] != "CUTOUT"))
        cut = [e for e in edl if e["kind"] == "CUTOUT"]
        out = []
        for e in [e for e in edl if e["kind"] != "CUTOUT"]:
            segs = [(e["t0"], e["t1"])]
            for c in cut:
                nxt = []
                for a, b in segs:
                    if b <= c["t0"] or a >= c["t1"]:
                        nxt.append((a, b))
                    else:
                        if a < c["t0"]:
                            nxt.append((a, c["t0"]))
                        if b > c["t1"]:
                            nxt.append((c["t1"], b))
                segs = nxt
            out += [dict(e, t0=a, t1=b) for a, b in segs if b - a > 0.3]
        return sorted(out + cut, key=lambda e: e["t0"])

    # ---------------------------------------------------------------- sound-ups
    @staticmethod
    def _sentences(ws):
        sents, cur = [], []
        for w in ws:
            cur.append(w)
            if w[2][-1:] in ".?!":
                sents.append(cur); cur = []
        if cur:
            sents.append(cur)
        return sents

    @staticmethod
    def _pi_score(o, pri):
        """lower = better punch-in without the LLM: a whole 2-5 s sentence of 5-16 words that does not open mid-thought."""
        dur, words = o["src1"] - o["src0"], o["said"].split()
        return (pri * 2 + (0 if 2.0 <= dur <= 5.0 else 1) + (0 if 5 <= len(words) <= 16 else 1)
                + (0 if o["said"].rstrip()[-1:] in ".?!" else 0.5)
                + (1 if words and words[0].lower().strip(",.") in ("and", "but", "so", "or", "because", "then") else 0))

    def punchins(self, edl, res, m, log=print):
        """Narration pauses, the show speaks: >=2 in the first 3 min, then one every ~2.5 min.
        Candidates are real whisper-heard sentences of shots already on screen for that line;
        agy Claude picks the most engaging one (or none)."""
        from .common import llm
        end = self.LT[-1][1]
        wins = [(8.0, 180.0, 2)] + [(a, min(a + 120.0, end - 15), 1) for a in range(180, int(end - 30), 120)]
        used_sids = {r["sid"] for r in res}
        for wa, wb, want in wins:
            have = [r for r in res if wa <= self.LT[r["line"]][1] < wb]
            if len(have) >= want:
                continue
            cands, seen = [], set()
            for e in edl:
                if e["kind"] != "V" or e["sid"] in used_sids or e["sid"] in seen:
                    continue
                i = e["line"]
                if not (wa <= self.LT[i][1] < wb) or any(r["line"] == i for r in res):
                    continue
                sh = self.SH[e["sid"]]
                if len((sh.get("dialogue") or "").split()) < 4:
                    continue
                seen.add(e["sid"])
                pri = {"quote": 0, "story": 1, "fact": 2}.get(self.LINES[i].get("type"), 3)
                cands.append((pri, i, e["sid"]))
            cands = sorted(cands)[:6]
            opts = []
            for _, i, sid in cands:
                sh = self.SH[sid]
                a = max(0, sh["start"] - 1.0)
                tmp = os.path.join(self.B, "assets", f"pi_{i}.wav")
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{a:.2f}", "-t", f"{sh['end'] - a + 4:.2f}",
                                "-i", lp(self.video(sid)), "-vn", "-ac", "1", "-ar", "16000", tmp])
                segs, _ = m.transcribe(tmp, language="en", word_timestamps=True)
                ws = [(a + w.start, a + w.end, w.word.strip()) for g in segs for w in g.words]
                sents = self._sentences(ws)
                for k in range(len(sents)):
                    for n in (1, 2):
                        win = [w for x in sents[k:k + n] for w in x]
                        if len(sents[k:k + n]) < n:
                            continue
                        dur = win[-1][1] - win[0][0]
                        if 1.0 <= dur <= 6.5 and len(win) >= 3 and sh["start"] - 1.0 <= win[0][0] <= sh["end"] + 1.5:
                            said = " ".join(w[2] for w in win)
                            if unsafe_line(said, self.LT[i][1]):      # profanity: never in a punch-in (ad-safe)
                                continue
                            nxt = next((w[0] for w in ws if w[0] > win[-1][1] + 0.01), win[-1][1] + 1.0)
                            opts.append(dict(id=len(opts), line=i, sid=sid, src0=win[0][0] - 0.2,
                                             src1=min(win[-1][1] + 0.15, nxt - 0.03), said=said,
                                             pri={"quote": 0, "story": 1, "fact": 2}.get(self.LINES[i].get("type"), 3)))
            if not opts:
                continue
            need = want - len(have)
            listing = "\n".join(f'[{o["id"]}] after narration line: "{self.LINES[o["line"]]["text"]}"  ->  original line: "{o["said"]}"'
                                 for o in opts)
            try:
                v, _ = llm("You are a YouTube video-essay editor. A PUNCH-IN = the narrator pauses and the ORIGINAL show "
                           "dialogue plays for a few seconds, to re-hook viewers. Pick lines that are self-contained, "
                           "punchy or funny, clearly connected to the narration line before them, and make sense out of "
                           "context. Never pick fragments, mumbles, or lines that need a missing setup. "
                           'Reply ONLY JSON {"picks":[<option ids>],"why":"<short>"} (empty list if none is good).',
                           f"Choose up to {need} option(s), at least 25 seconds apart in the video.\n{listing}")
                picks = [int(x) for x in (v.get("picks") or [])][:need]
                llm_ok = True
            except Exception as ex:
                log(f"  punch-in pick failed: {ex} - choosing by rules instead")
                picks, llm_ok = [], False
            # B99 (10-13) lost every punch-in when the LLM failed, and every video had a 3.5-min stretch without one:
            # rule-based pick when the LLM failed, or when it chose nothing and the last punch-in is 150+ s back
            prev = max([self.LT[r["line"]][1] for r in res if self.LT[r["line"]][1] < wa], default=0.0)
            if not picks and (not llm_ok or wa - prev >= 150):
                ranked = sorted(opts, key=lambda o: self._pi_score(o, o["pri"]))
                picks = [o["id"] for o in ranked[:need]]
                if picks:
                    log(f"  punch-in by rules ({'LLM failed' if not llm_ok else f'{wa - prev:.0f} s since the last one'})")
            for pid in picks:
                o = next((x for x in opts if x["id"] == pid), None)
                if not o or o["sid"] in used_sids:
                    continue
                if any(abs(self.LT[r["line"]][1] - self.LT[o["line"]][1]) < 25 for r in res):
                    continue
                res.append(dict(line=o["line"], sid=o["sid"], src0=o["src0"], src1=o["src1"], said=o["said"],
                                mode="insert", kind="punch"))
                used_sids.add(o["sid"])
                log(f"  punch-in L{o['line'] + 1} @{self.LT[o['line']][1]:.0f}s: '{o['said']}'")
        res.sort(key=lambda r: r["line"])
        return res

    def soundups(self, log=print, edl=None):
        from faster_whisper import WhisperModel
        m = WhisperModel("small.en", device="cpu", compute_type="int8", cpu_threads=16)
        english = self.ctx.get("language", "en") == "en"
        res, last_t = [], -999.0
        for i, p in enumerate(self.LINES):
            sid = p.get("soundup")
            if p.get("type") != "quote" or sid not in self.SH:
                continue
            # pacing: dense in the intro (hook), sparse later, never back-to-back
            t_line = self.LT[i][0]
            gap = 20 if t_line < 180 else 60
            if t_line - last_t < gap or len(res) >= (6 + int(self.LT[-1][1] // 180)):
                continue
            s = self.SH[sid]
            a = max(0, s["start"] - 4)
            tmp = os.path.join(self.B, "assets", f"su_{i}.wav")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{a:.2f}", "-t", f"{s['end'] - a + 12:.2f}",
                            "-i", lp(self.video(sid)), "-vn", "-ac", "1", "-ar", "16000", tmp])
            segs, _ = m.transcribe(tmp, language="en", word_timestamps=True)
            ws = [(a + w.start, a + w.end, w.word.strip()) for g in segs for w in g.words]
            # the clue's quote is often a paraphrase (or invented) -> find the REAL spoken sentence(s)
            # near the shot that carry it; play whole sentences only, never a fragment
            q = " ".join(r.get("quote") or "" for r in p.get("refs", [])).strip() or p["text"]
            target = {norm(x) for x in q.split() if norm(x)}
            if len(target) < 2 or not ws:
                continue
            sents, cur = [], []
            for w in ws:
                cur.append(w)
                if w[2][-1:] in ".?!":
                    sents.append(cur); cur = []
            if cur:
                sents.append(cur)
            best = None
            for k in range(len(sents)):
                for n in (1, 2):
                    win = [w for x in sents[k:k + n] for w in x]
                    if len(sents[k:k + n]) < n or win[-1][1] - win[0][0] > 7.5:
                        continue
                    toks = {norm(w[2]) for w in win}
                    cov = len(target & toks) / len(target)
                    prec = len(target & toks) / max(1, len(toks))
                    sc = cov + 0.25 * prec
                    if cov >= 0.6 and (best is None or sc > best[0]):
                        best = (sc, win)
            if not best or best[1][0][0] > s["end"] + 6 or best[1][-1][1] < s["start"] - 2:
                continue
            win = best[1]
            if unsafe_line(" ".join(w[2] for w in win), t_line):      # Reacher 02: 'fucked' at 0:55 - ad-unsafe
                log(f"  sound-up L{i + 1} skipped: profanity")
                continue
            nxt = next((w[0] for w in ws if w[0] > win[-1][1] + 0.01), win[-1][1] + 1.0)
            ln = {norm(x) for x in p["text"].split() if norm(x)}
            heard = {norm(w[2]) for w in win}
            import difflib
            hit = sum(1 for x in ln if x in heard or difflib.get_close_matches(x, heard, 1, 0.8))
            same = english and hit / max(1, len(ln)) >= 0.7          # narration says the quote -> replace it
            res.append(dict(line=i, sid=sid, src0=win[0][0] - 0.2, src1=min(win[-1][1] + 0.15, nxt - 0.03),
                            said=" ".join(w[2] for w in win), mode="replace" if same else "insert"))
            last_t = t_line
            log(f"  sound-up L{i + 1} ({res[-1]['mode']}): '{res[-1]['said']}'")
        if edl:
            res = self.punchins(edl, res, m, log)
        if res:                                             # whisper mishears names ("Quelblank") -> fix captions
            try:
                from .common import llm
                names = sorted({w for p in self.LINES for w in re.findall(r"\b[A-Z][a-z]{3,}\b", p["text"])})
                shows = ", ".join(s["name"] for s in self.ctx["shows"])
                v, _ = llm("You correct speech-to-text transcripts of TV dialogue. Fix ONLY misheard words (mostly names and "
                           "show-specific terms); never rephrase, never add or drop words. Reply ONLY JSON {\"fixed\":[...]} with "
                           "the same number of strings in the same order.",
                           f"Shows: {shows}\nNames/terms in the script: {', '.join(names)}\n" +
                           json.dumps([r["said"] for r in res], ensure_ascii=False))
                fx = (v.get("fixed") if isinstance(v, dict) else v) or []      # the model sometimes replies a bare list
                if len(fx) == len(res):
                    for r, f in zip(res, fx):
                        if f and f != r["said"]:
                            log(f"  caption fix: '{r['said']}' -> '{f}'")
                            r["said"] = f
            except Exception as ex:
                log(f"  caption fix skipped: {ex}")
        return res

    # ---------------------------------------------------------------- run
    def run(self, log=print):
        out_p = os.path.join(self.B, "plan.json")
        os.makedirs(os.path.join(self.B, "assets"), exist_ok=True)
        edl, over = self.grammar()
        for x in self.cross_log:
            log(f"  cross-show guard: {x}")
        vs = sorted({e["sid"] for e in edl if e["kind"] == "V"} | {e[k] for e in edl if e["kind"] == "SPLIT" for k in ("left", "right")})
        line_of = {}
        for e in edl:
            for k in ("sid", "left", "right"):
                if k in e:
                    line_of.setdefault(e[k], self.LINES[e["line"]]["text"])
        cache_p = os.path.join(self.B, "verify_cache.json")
        cache = json.load(open(cache_p, encoding="utf-8")) if os.path.exists(cache_p) else {}
        todo = [s for s in vs if f"{s}|{line_of[s]}" not in cache]
        log(f"  verify: {len(vs)} shots ({len(todo)} new) — Gemini 4x4 re-anchor")
        def save_cache():                             # atomic: a crash mid-write must not lose the earlier verdicts
            json.dump(cache, open(cache_p + ".tmp", "w", encoding="utf-8"), ensure_ascii=False, indent=0)
            os.replace(cache_p + ".tmp", cache_p)
        with cf.ThreadPoolExecutor(VERIFY_THREADS) as ex:
            for n, (s, v) in enumerate(zip(todo, ex.map(lambda s: self.reanchor(s, line_of[s]), todo)), 1):
                if "verify-error" not in str(v.get("why", "")):
                    cache[f"{s}|{line_of[s]}"] = v
                if n % 20 == 0:
                    # saved as it goes: PC off / internet / Stop during a 15 min - 2.5 h verify keeps what is done
                    save_cache()
        save_cache()
        V = {s: cache.get(f"{s}|{line_of[s]}", {"pick": 0, "score": 0, "why": "no verdict"}) for s in vs}
        rej = {s for s, v in V.items() if v.get("score", 0) < 5 or not v.get("pick")}
        log(f"  verify: rejected {len(rej)}/{len(vs)}")
        LINES = self.LINES

        def short_text(i):
            p = LINES[i]
            k = p.get("kinetic")
            k = " ".join(k) if isinstance(k, list) else k
            if len(p["text"].split()) <= 6:
                return p["text"]
            return k if k and len(k.split()) <= 6 else None

        clean = []
        used_now = {x.get("sid") for x in edl if x.get("sid")}

        def can_extend(line):
            """the previous shot may run on under this line only if it is the line's own show (cross-show guard)."""
            if not (clean and clean[-1]["kind"] in ("V", "SPLIT")):
                return False
            own = self.line_owner(line)
            return not own or own in self.shows_of_item(clean[-1])

        def owner_fill(e):
            own = self.line_owner(e["line"])
            alt = self.owner_shot(own, e["line"], used_now) if own else None
            if alt:
                used_now.add(alt)
                self.cross_log.append(f"line {e['line'] + 1}: rejected/no shot next to other-show footage -> {alt}")
                return dict(kind="V", t0=e["t0"], t1=e["t1"], sid=alt, line=e["line"])
            return None

        for e in edl:
            if e["kind"] == "V" and e["sid"] in rej:
                fill = None if can_extend(e["line"]) else owner_fill(e)
                if fill:
                    clean.append(fill)
                elif can_extend(e["line"]):
                    clean[-1]["t1"] = e["t1"]
                elif short_text(e["line"]):
                    clean.append(dict(kind="BREATH", t0=e["t0"], t1=e["t1"], text=short_text(e["line"]), line=e["line"]))
                else:
                    clean.append(dict(kind="GAP", t0=e["t0"], t1=e["t1"], line=e["line"]))
                continue
            if e["kind"] == "SPLIT" and (e["left"] in rej or e["right"] in rej):
                ok = [s for s in (e["left"], e["right"]) if s not in rej]
                if ok:
                    e = dict(kind="V", t0=e["t0"], t1=e["t1"], sid=ok[0], line=e["line"])
                elif can_extend(e["line"]):
                    clean[-1]["t1"] = e["t1"]
                    continue
                elif short_text(e["line"]):
                    e = dict(kind="BREATH", t0=e["t0"], t1=e["t1"], text=short_text(e["line"]), line=e["line"])
                else:
                    e = dict(kind="GAP", t0=e["t0"], t1=e["t1"], line=e["line"])
            if e["kind"] == "BREATH" and len(LINES[e["line"]]["text"].split()) > 6:
                if not can_extend(e["line"]) and clean and clean[-1]["kind"] in ("V", "SPLIT"):
                    fill = owner_fill(e)              # a long line about the OTHER show: its own character, not a card
                    if fill:
                        clean.append(fill)
                        continue
                if can_extend(e["line"]):
                    clean[-1]["t1"] = e["t1"]
                    st = short_text(e["line"])
                    if st:
                        over.append(dict(kind="KIN", t0=e["t0"], t1=e["t1"], text=st, line=e["line"], side=e["line"] % 2))
                    continue
                if short_text(e["line"]):
                    e["text"] = short_text(e["line"])
                else:
                    e = dict(kind="GAP", t0=e["t0"], t1=e["t1"], line=e["line"])
            clean.append(e)
        def fits(line, shows):
            """footage of `shows` may sit under this line (the line names no single show, or one of these)."""
            own = self.line_owner(line)
            return not own or own in shows

        seen, dedup = set(), []
        for e in clean:
            if e["kind"] == "SPLIT" and (e["left"] in seen or e["right"] in seen):
                ok = [x for x in (e["left"], e["right"]) if x not in seen]
                if ok:
                    e = dict(kind="V", t0=e["t0"], t1=e["t1"], sid=ok[0], line=e["line"])
                elif dedup and dedup[-1]["kind"] in ("V", "SPLIT") and fits(e["line"], self.shows_of_item(dedup[-1])):
                    dedup[-1]["t1"] = e["t1"]
                    continue
                elif not (dedup and dedup[-1]["kind"] in ("V", "SPLIT")):
                    continue
            if e["kind"] == "V" and e["sid"] in seen and dedup and dedup[-1]["kind"] in ("V", "SPLIT")                     and fits(e["line"], self.shows_of_item(dedup[-1])):
                dedup[-1]["t1"] = e["t1"]
                continue
            for k in ("sid", "left", "right"):
                if k in e:
                    seen.add(e[k])
            dedup.append(e)
        for k, e in enumerate(dedup):
            if e["kind"] == "BREATH" and e["t1"] - e["t0"] > 3.0 and k + 1 < len(dedup) and dedup[k + 1]["kind"] in ("V", "SPLIT"):
                dedup[k + 1]["t0"] = e["t0"] + 3.0
                e["t1"] = e["t0"] + 3.0
        # never a long static card: a breath is <= 2.5 s, the rest continues the previous shot's episode;
        # a breath right after a breath becomes footage (its words go on screen as kinetic text)
        fixed, prev_sid = [], None
        first_sid = next((e["sid"] for e in dedup if e["kind"] == "V"), None)
        cont_memo = {}

        def cont_sid(line, sid):
            """the shot a card / gap continues: the previous one if it is the line's show, else the line's own
            character from its show (cross-show guard, MITM vs Young Sheldon audit: 2 left after the first fix)."""
            if not sid or fits(line, {self.SH[sid]["ep"][0]} if sid in self.SH else set()):
                return sid
            if (line, sid) not in cont_memo:
                own = self.line_owner(line)
                cont_memo[(line, sid)] = self.owner_shot(own, line, used_now) if own else None
                if cont_memo[(line, sid)]:
                    self.cross_log.append(f"line {line + 1}: continuation of other-show footage -> {cont_memo[(line, sid)]}")
            return cont_memo[(line, sid)]
        for e in dedup:
            if e["kind"] == "V":
                prev_sid = e["sid"]
            if e["kind"] == "GAP":
                if fixed and fixed[-1]["kind"] in ("V", "SPLIT") and fixed[-1]["t1"] >= e["t0"] - 0.05                         and fits(e["line"], self.shows_of_item(fixed[-1])):
                    fixed[-1]["t1"] = e["t1"]
                elif cont_sid(e["line"], prev_sid or first_sid):
                    fixed.append(dict(kind="V", t0=e["t0"], t1=e["t1"], sid=cont_sid(e["line"], prev_sid or first_sid),
                                      line=e["line"], cont=True))
                continue
            if e["kind"] == "BREATH" and prev_sid and cont_sid(e["line"], prev_sid):
                csid = cont_sid(e["line"], prev_sid)
                if fixed and fixed[-1]["kind"] == "BREATH":
                    fixed.append(dict(kind="V", t0=e["t0"], t1=e["t1"], sid=csid, line=e["line"], cont=True))
                    if e.get("text"):
                        over.append(dict(kind="KIN", t0=e["t0"], t1=e["t1"], text=e["text"], line=e["line"], side=e["line"] % 2))
                    continue
                if e["t1"] - e["t0"] > 3.0:
                    fixed.append(dict(e, t1=e["t0"] + 2.5))
                    fixed.append(dict(kind="V", t0=e["t0"] + 2.5, t1=e["t1"], sid=csid, line=e["line"], cont=True))
                    continue
            fixed.append(e)
        edl = self.resolve_overlaps(fixed)
        need = sorted({e["sid"] for e in edl if e["kind"] == "V"} | {e[k] for e in edl if e["kind"] == "SPLIT" for k in ("left", "right")})
        with cf.ThreadPoolExecutor(8) as ex:
            RR = dict(zip(need, ex.map(lambda s: self.real_range(s, V.get(s, {}).get("t")), need)))
        SU = self.soundups(log, edl)
        extra = {s: v for s, v in self.SH.items() if s not in self._clue_sids}
        json.dump(dict(edl=edl, over=over, verify=V, real=RR, soundups=SU, line_times=self.LT,
                       stats=dict(verified=len(vs), rejected=len(rej), cross_show_fixes=len(self.cross_log)),
                       extra_shots=extra),
                  open(out_p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        log(f"  plan: {len(edl)} visual items, {len(over)} overlays, {len(SU)} sound-ups")
        return out_p
