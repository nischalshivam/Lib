"""state.json -> HyperFrames project (comp/index.html) in one LOOK:
  A pop        full-screen, depth-text x3/3min, colour isolation x1, annotations, cutouts
  B framed     ~60% of shots in a framed card on a textured backdrop (the channel's classic look)
  C cinematic  full-screen + letterbox, sparse text
  auto         picks A/B/C per video (seeded by the script name) — no fixed rotation
Every effect only where it is SAFE: one big face, the named person verified, one effect per shot."""
from __future__ import annotations

import colorsys, glob, hashlib, json, os, random, re, shutil, subprocess

from .common import faces as _faces, is_person, lp, FPS
from .plan import Plan, norm
from . import assets, fx

FONTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
BGDIR = r"C:\Users\Dell\Desktop\ProStudio\backgrounds"
LOOKS = ("A", "B", "C")
WEAK = {"whatever", "notices", "paying", "entitles", "central", "match", "judging", "really", "because", "always",
        "something", "nothing", "everything", "another", "himself", "herself", "itself", "without", "between", "though"}
# per 3 minutes of video
BUDGET = {"A": dict(depth=2, iso=1, ann=2, framed=0.0, kin_every=1, punch=True, letterbox=False),
          "B": dict(depth=2, iso=0, ann=1, framed=0.62, kin_every=1, punch=True, letterbox=False),
          "C": dict(depth=1, iso=0, ann=0, framed=0.0, kin_every=2, punch=False, letterbox=True)}


def pick_look(ctx):
    lk = (ctx.get("look") or "auto").upper()
    if lk in LOOKS:
        return lk
    from .pipeline import topic_name                  # 'Script.txt' everywhere -> hash the topic, not the file name
    h = int(hashlib.md5(topic_name(ctx["clean"]).encode()).hexdigest(), 16)
    return LOOKS[h % 3]


def _shade(hexcol, f):
    r, g, b = (int(hexcol[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    l = max(0, min(1, l * f))
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))


def fix_spelling(said, script_line):
    import difflib
    vocab = re.findall(r"[\w']+", script_line)
    out = []
    for w in said.split():
        core = re.sub(r"[^\w']", "", w)
        m = difflib.get_close_matches(core, vocab, n=1, cutoff=0.72) if len(core) >= 5 and "'" not in core else []
        out.append(w.replace(core, m[0]) if m and m[0].lower() != core.lower() else w)
    return " ".join(out)


# Name cards (lower-third NAME + role) are OFF - user 2026-10-13: across 4 test videos they landed on the wrong face
# (TAHM: Lyndsey's card on Alan, an episode title as a card; Reacher: ALAN RITCHSON on a woman, Roscoe's card on Reacher)
# and checking them cost Gemini calls in every build. The cast-photo CUTOUT intro (photo from the Cast folder) stays.
# Set EDITV2_NAME_CARDS=1 to bring them back.
NAME_CARDS = os.environ.get("EDITV2_NAME_CARDS", "0") == "1"
# words that never appear in a person's name: a label holding them is an episode title / phrase, not a person
LABEL_STOP = {"the", "a", "an", "and", "of", "that", "this", "in", "on", "with", "for", "to", "at", "from", "by", "is",
              "was", "episode", "season", "finale", "pilot", "part", "my", "your", "his", "her"}


class Look:
    def __init__(self, ctx, log=print):
        self.ctx, self.log = ctx, log
        self.P = Plan(ctx)
        self.B = ctx["build"]
        self.SRC = os.path.join(self.B, "assets")
        st = json.load(open(os.path.join(self.B, "state.json"), encoding="utf-8"))
        self.ITEMS, self.TOTAL, self.REPL = st["items"], st["total"], st["repl"]
        self.CLIPS = {int(k): v for k, v in st["clips"].items()}
        pl = json.load(open(os.path.join(self.B, "plan.json"), encoding="utf-8"))
        self.OVER = pl["over"]
        self.shows = {s["key"]: s for s in ctx["shows"]}
        self.default_show = ctx["shows"][0]["key"]
        for s in self.shows.values():               # a dark logo colour (BB green) is unreadable on footage
            acc = s["style"]["accent"]
            r, g, b_ = (int(acc[i:i + 2], 16) / 255 for i in (1, 3, 5))
            l = colorsys.rgb_to_hls(r, g, b_)[1]
            if l < 0.45:
                s["style"] = dict(s["style"], accent=_shade(acc, 0.55 / max(l, 0.05)))
        self.look = pick_look(ctx)
        minutes = max(1.0, self.TOTAL / 60)
        b = BUDGET[self.look]
        self.n_depth = min(8, max(1, round(b["depth"] * minutes / 3)))     # effects only where they land hard
        self.n_iso = min(4, round(b["iso"] * minutes / 3))
        # annotations (arrow + a person's NAME on a frozen face) are OFF like name cards (user 2026-10-13): same
        # wrong-face risk, a Gemini identity check per use. EDITV2_ANNOTATIONS=1 brings them back.
        self.n_ann = round(b["ann"] * minutes / 3) if os.environ.get("EDITV2_ANNOTATIONS", "0") == "1" else 0

    # ---------------------------------------------------------------- helpers
    def out(self, t):
        o = t
        for a, b, d, _ in self.REPL:
            if t >= b:
                o += d - (b - a)
            elif t > a:
                return None
        return o

    def out_clip(self, a, b):
        segs = [(a, b)]
        for ra, rb, _, _ in self.REPL:
            if rb <= ra:
                segs = [x for x, y in segs for x in ([(x, ra), (ra, y)] if x < ra < y else [(x, y)])]
                continue
            nxt = []
            for x, y in segs:
                if y <= ra or x >= rb:
                    nxt.append((x, y))
                else:
                    if x < ra:
                        nxt.append((x, ra))
                    if y > rb:
                        nxt.append((rb, y))
            segs = nxt
        return [(self.out(x), self.out(y - 1e-6) + 1e-6) for x, y in segs if y - x > 0.6]

    def sid_of(self, it):
        return it.get("sid") or (it.get("su") or {}).get("sid") or ""

    def show_of(self, it):
        sid = self.sid_of(it)
        if ":" in sid:
            return sid.split(":")[0]
        return self.line_show(self.line_of(it))

    def line_show(self, i):
        txt = self.P.LINES[i]["text"] if 0 <= i < len(self.P.LINES) else ""
        hits = [k for k, s in self.shows.items() if any(re.search(rf"\b{re.escape(n)}\b", txt) for n in s["first_names"][:40])]
        return hits[0] if len(hits) == 1 else self.default_show

    def line_of(self, it):
        return it["line"] if "line" in it else (it.get("su") or {}).get("line", 0)

    def clip_of(self, k):
        return os.path.join(self.SRC, os.path.basename(self.CLIPS[k][0][1]))

    def label_on_person(self, o, a, bb):
        """-> (start, end) of the name card on a clip that shows the labelled person, or None."""
        dur = bb - a
        role = o.get("role") or ""
        m = re.match(r"\s*([A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*)?)'s?\b", role)
        key = self.line_show(o["line"])
        show = self.shows.get(key, {}).get("name", key)
        # a name card is for a PERSON: TAHM 01 got 'THAT DARN PRIEST' (an episode title) - agy sometimes labels episodes
        tok = [w.lower() for w in re.findall(r"[A-Za-z]+", o["name"])]
        if not tok or len(tok) > 4 or re.search(r"\d", o["name"]) or set(tok) & LABEL_STOP:
            self.log(f"  label {o['name']!r} dropped: not a person's name")
            return None
        # who the card names, as catalogue character-name tokens: 2+ words -> first name must match (BEN MERRICK must
        # not match 'Anna Merrick'); one word ('FINLAY') -> any token of the character name. The role's possessive
        # ("Roscoe's ...") names the CHARACTER only when the card shows an ACTOR (WILLA FITZGERALD); for a character
        # card it names someone else ("LYNDSEY - Alan's girlfriend" was put on Alan, TAHM 01).
        cast = self.shows.get(key, {}).get("cast") or []
        names = [o["name"]]

        def named(chars, ns=None):
            for n in (ns or names):
                t = [w.lower() for w in re.findall(r"[A-Za-z]+", n)]
                for c in chars:
                    ct = [w.lower() for w in re.findall(r"[A-Za-z]+", c)]
                    if t and ct and ((len(t) >= 2 and t[0] == ct[0]) or (len(t) == 1 and t[0] in ct)):
                        return True
            return False

        if m and not named(cast):                     # the card names an actor -> the possessive is their character
            names.append(m.group(1))
        who = o["name"].title() + (f" (the character {names[1]})" if len(names) > 1 else "")

        lo, hi = a - 0.5, a + 12.0                     # the card may move a little later within the same moment
        for k, it in enumerate(self.ITEMS):
            if it["kind"] not in ("V", "SU") or k not in self.CLIPS or it["o1"] <= lo or it["o0"] >= hi:
                continue
            start = max(a, it["o0"])
            rel = start - it["o0"]
            # the card stays inside ONE camera shot: a clip continues into the next catalogue shots (Roscoe ->
            # Reacher in S01E02 shot-reverse-shot put Roscoe's card on Reacher)
            cuts = self.clip_cuts(k)
            nxt = min([c for c in cuts if c > rel + 0.2], default=it["o1"] - it["o0"])
            end = min(start + dur, it["o1"], it["o0"] + nxt - 0.1)
            if end - start < 1.5:
                continue
            # 1) catalogue: the shot's characters were verified against the Cast photos at indexing time - works for
            #    new seasons Gemini has never seen (Reacher S4: every card was dropped by the Gemini-only check)
            first_shot = not any(c <= rel + 0.05 for c in cuts)
            chars = (self.P.SH.get(it.get("sid"), {}) or {}).get("characters") or [] if first_shot else []
            if chars:
                if not named(chars):
                    # the catalogue says someone else is in this shot: never ask Gemini to overrule it (it said "yes,
                    # Jacob Merrick" on a catalogued Russell Plum shot - it does not know new-season faces)
                    continue
                if len(chars) == 1:                   # the person alone in the shot: the catalogue is enough
                    try:
                        fs, _ = _faces(self.clip_of(k), rel + (end - start) / 2)
                    except Exception:                  # noqa: BLE001
                        fs = []
                    if fs:
                        return start, end
                    continue
                # two+ people (Lyndsey + Alan in one bed shot): the card's person must be the one Gemini names as
                # the MAIN face - falls through to the Gemini check below

            # 2) shot not in the catalogue: Gemini, and the person it NAMES must be the card's person (a bare "yes"
            #    was a guess for faces it does not know)
            def check(t):
                try:
                    yes, seen = is_person(self.clip_of(k), t - it["o0"], who, show)
                    return bool(yes) and named([str(seen)]), seen
                except Exception:                      # noqa: BLE001
                    return False, "check-error"
            ok, seen = check(start + 0.4)
            if ok:
                ok, seen = check(end - 0.3)
                if ok:
                    return start, end
            self.log(f"  label {o['name']!r}: clip {k} shows {seen!r} (catalogue {chars}), trying the next clip")
        return None

    def clip_cuts(self, k):
        """scene-cut times (s, relative) inside clip k - ffmpeg scene score, cached."""
        if not hasattr(self, "_cuts"):
            self._cuts = {}
        if k not in self._cuts:
            r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", self.clip_of(k), "-an", "-vf",
                                "scale=320:-2,select='gt(scene,0.32)',showinfo", "-f", "null", "-"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
            self._cuts[k] = [float(x) for x in re.findall(r"pts_time:([\d.]+)", r.stderr)]
        return self._cuts[k]

    def script_caps(self):
        """Capitalised words of the whole script (names, 'Krelboyne') to fix whisper's spelling in captions."""
        if not hasattr(self, "_caps"):
            ws = {w for p in self.P.LINES for w in re.findall(r"\b[A-Z][a-z]{3,}\b", p["text"])}
            self._caps = " ".join(sorted(ws))
        return self._caps

    def style(self, key):
        return self.shows.get(key, self.shows[self.default_show])["style"]

    def fam(self, key):
        return f"F_{re.sub(r'[^a-z0-9]', '', key)}"

    def all_names(self):
        return {n for s in self.ctx["shows"] for n in s["first_names"]}

    # ---------------------------------------------------------------- key words
    def key_word(self, i):
        p = self.P.LINES[i]
        m = re.search(r"\b(IQ|\$|€|£)?\s?(of\s|de\s|von\s)?(\d[\d,.]*)", p["text"])
        if m and m.group(3) and len(m.group(3).strip(".,")) >= 2:
            return ((m.group(1) + " ") if m.group(1) else "") + m.group(3).strip(".,")
        names = self.all_names() | {"Young", "The"}
        # every TOKEN of every name ('Reacher' of 'Jack Reacher' was picked twice), and capitalised words the script
        # repeats 3+ times = a recurring name / place / brand ('NutriBoom' x5 in B99, 'Margrave' x2) - not a depth word
        name_toks = {t.lower() for n in list(names) + [c for s in self.ctx["shows"] for c in (s.get("cast") or [])]
                     for t in re.findall(r"[A-Za-z]+", n) if len(t) >= 3}
        if not hasattr(self, "_script_counts"):
            from collections import Counter
            self._script_counts = Counter(w.lower() for p_ in self.P.LINES for w in re.findall(r"[A-Za-z]+", p_["text"]))
        words = [w.split("'")[0] for w in re.findall(r"[\w']+", p["text"])]
        caps = [w for w in words[1:] if w[:1].isupper() and len(w) >= 7 and w not in names
                and w.lower() not in name_toks and self._script_counts[w.lower()] < 3]
        if caps:
            return caps[0]
        em = p.get("emphasis")
        if em and 6 <= len(em) <= 11 and em.lower() not in WEAK and em.capitalize() not in names and not em.lower().endswith(("ly", "ing", "es")):
            return em
        return None

    # ---------------------------------------------------------------- effect moments
    def depth_moments(self):
        D = []
        for i in range(len(self.P.LINES)):
            w = self.key_word(i)
            if not w:
                continue
            toks = [norm(x) for x in w.split()]
            hit = next((x for x in self.P.words_of(i) if norm(x[2]) in toks), None)
            su = next((it for it in self.ITEMS if it["kind"] == "SU" and it["su"]["line"] == i), None)
            if su is not None and su["su"].get("mode") == "replace":
                said = su["su"]["said"].lower()
                pos = said.find(w.lower())
                if pos < 0:
                    continue
                t = su["o0"] + 0.15 + (su["o1"] - su["o0"] - 0.3) * pos / max(1, len(said))
            elif hit is not None:
                t = self.out(hit[0])
                if t is None:
                    continue
            else:
                continue
            k = next((k for k, it in enumerate(self.ITEMS) if it["kind"] in ("V", "SU") and it["o0"] <= t < it["o1"] and k in self.CLIPS), None)
            if k is None:
                continue
            host = self.ITEMS[k]
            key = self.show_of(host)
            txt = self.P.LINES[i]["text"]
            named = self.P.names_in(txt, key)
            line_shows = [s for s in self.shows if self.P.names_in(txt, s)]
            if len(line_shows) == 1 and line_shows[0] != key:
                continue
            appear = t - host["o0"] - 0.12
            if appear < 0 or host["o1"] - t < 1.0:
                continue
            fs, (W, H) = _faces(self.clip_of(k), appear + 0.3)
            if len([f for f in fs if f[3] / H >= 0.20]) != 1:
                continue
            if len(named) == 1:
                ok, who = is_person(self.clip_of(k), appear + 0.3, named[0], self.shows[key]["name"])
                if not ok:
                    continue
            D.append(dict(k=k, t=t, appear=appear, word=w, line=i, show=key))
        return D

    def single_face_items(self):
        C = []
        for k, it in enumerate(self.ITEMS):
            if it["kind"] not in ("V", "SU") or k not in self.CLIPS or it["o1"] - it["o0"] < 1.6:
                continue
            fs, (W, H) = _faces(self.clip_of(k), 0.45)
            big = [f for f in fs if f[3] / H >= 0.20]
            C.append(dict(k=k, t=it["o0"], d=it["o1"] - it["o0"], kind=it["kind"], nfaces=len(fs), big=len(big),
                          chars=self.P.SH.get(self.sid_of(it), {}).get("characters", []), W=W, H=H, face=fs[0] if len(fs) == 1 else None))
        return C

    # ---------------------------------------------------------------- build
    def build(self):
        L, b = self.look, BUDGET[self.look]
        OUT = os.path.join(self.B, "comp")
        A = os.path.join(OUT, "assets")
        os.makedirs(os.path.join(A, "fonts"), exist_ok=True)
        rnd = random.Random(int(hashlib.md5(self.B.encode()).hexdigest(), 16))
        # project files
        tpl = r"D:\Lib\hyperframes_lab\demo"
        for f in ("hyperframes.json", "package.json"):
            shutil.copy(os.path.join(tpl, f), OUT)
        json.dump({"id": "editv2", "name": "editv2"}, open(os.path.join(OUT, "meta.json"), "w"))
        # GSAP from our own copy: the CDN <script> made every render need the internet (it dropped for 6 h on 10-12)
        shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "gsap.min.js"), os.path.join(A, "gsap.min.js"))
        fonts_needed = {s["style"]["font"] for s in self.ctx["shows"]} | {"Kalam-Bold.ttf"}
        for f in fonts_needed:
            shutil.copy(os.path.join(FONTS, f), os.path.join(A, "fonts", f))
        for f in glob.glob(os.path.join(self.SRC, "c*.mp4")):
            dst = os.path.join(A, os.path.basename(f))
            if not os.path.exists(dst):
                try:
                    os.link(f, dst)
                except OSError:
                    shutil.copy(f, dst)
        for s in self.ctx["shows"]:
            lg = s["style"].get("logo")
            if lg and os.path.exists(lg):
                shutil.copy(lg, os.path.join(A, f"logo_{s['key']}.png"))
        # effect moments
        D = self.depth_moments()
        depth, used_words = [], set()
        for c in D:                                   # each depth word once per video (B99 had 'NutriBoom' x5)
            if len(depth) < self.n_depth and all(abs(c["t"] - x["t"]) >= 12 for x in depth) \
                    and c["word"].lower() not in used_words:
                depth.append(c)
                used_words.add(c["word"].lower())
        C = self.single_face_items()
        used = {c["k"] for c in depth}
        iso = []
        for c in sorted(C, key=lambda c: (c["kind"] != "SU", -c["d"])):
            if len(iso) >= self.n_iso:
                break
            if c["big"] == 1 and c["k"] not in used and all(abs(c["t"] - x["t"]) >= 30 for x in iso + depth):
                iso.append(c); used.add(c["k"])
        ann, ann_chars = [], set()
        for c in sorted(C, key=lambda c: -c["d"]):
            if len(ann) >= self.n_ann:
                break
            if c["nfaces"] == 1 and len(c["chars"]) == 1 and c["k"] not in used and c["chars"][0] not in ann_chars \
                    and all(abs(c["t"] - x["t"]) >= 3.5 for x in ann + depth + iso):
                key = self.show_of(self.ITEMS[c["k"]])
                ok, who = is_person(self.clip_of(c["k"]), 0.45, c["chars"][0].split()[0], self.shows[key]["name"])
                if ok:
                    ann.append(c); used.add(c["k"]); ann_chars.add(c["chars"][0])
        fxsrc = {}
        for c in depth:
            st = self.style(c["show"])
            o = os.path.join(A, f"fx_depth_{c['k']}.mp4")
            word = c["word"].upper() if st["upper"] else c["word"][:1].upper() + c["word"][1:]
            if not os.path.exists(o):
                fx.depth_text(self.clip_of(c["k"]), word, os.path.join(FONTS, st["font"]), o,
                              color=tuple(int(st["accent"][i:i + 2], 16) for i in (1, 3, 5)) if not st.get("stroke") else (255, 255, 255),
                              stroke=(20, 20, 20) if st.get("stroke") else None, y_frac=0.34, appear=c["appear"])
            fxsrc[c["k"]] = os.path.basename(o)
        for c in iso:
            o = os.path.join(A, f"fx_iso_{c['k']}.mp4")
            if not os.path.exists(o):
                fx.color_isolate(self.clip_of(c["k"]), o, tint=(150, 110, 210))
            fxsrc[c["k"]] = os.path.basename(o)
        self.log(f"  look {L}: depth {len(depth)} ({[c['word'] for c in depth]}), isolation {len(iso)}, annotations {len(ann)}")
        ann_k = {c["k"]: c for c in ann}
        bgs = sorted(glob.glob(os.path.join(BGDIR, "*.jpg")) + glob.glob(os.path.join(BGDIR, "*.png")))
        clips, tw, framed_left, bgn, still_n = [], [], 0, None, 0
        framed_items = set()
        for k, it in enumerate(self.ITEMS):
            o0, o1 = it["o0"], it["o1"]
            d = o1 - o0
            key = self.show_of(it)
            fam = self.fam(key)
            st = self.style(key)
            if it["kind"] in ("V", "SU") and k in self.CLIPS:
                src = fxsrc.get(k, os.path.basename(self.CLIPS[k][0][1]))
                if k in ann_k:
                    hold0 = min(1.6, d - 0.3)
                    fz = f"fz_{k}.mp4"
                    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "0.45", "-i", self.clip_of(k), "-vf",
                                    f"tpad=start_mode=clone:start_duration={hold0:.3f}", "-t", f"{d + 0.3:.3f}", "-an",
                                    "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-g", str(FPS), os.path.join(A, fz)], check=True)
                    src = fz
                framed = b["framed"] > 0 and bgs and it["kind"] == "V" and k not in fxsrc and k not in ann_k and rnd.random() < b["framed"]
                if framed:
                    if framed_left <= 0:
                        bg = rnd.choice(bgs); framed_left = 3
                        bgn = "bg_" + re.sub(r"\W", "", os.path.basename(bg).split(".")[0]) + ".jpg"
                        if not os.path.exists(os.path.join(A, bgn)):
                            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", bg, "-vf",
                                            "scale=2000:1160:force_original_aspect_ratio=increase,crop=2000:1160", os.path.join(A, bgn)])
                    framed_left -= 1
                    framed_items.add(k)
                    clips.append(f'<img class="clip fbg" id="bg{k}" src="assets/{bgn}" data-start="{o0:.3f}" data-duration="{d:.3f}" data-track-index="0">')
                    clips.append(f'<video id="cd{k}" class="clip fvid" src="assets/{src}" data-start="{o0:.3f}" data-duration="{d:.3f}" data-track-index="1" muted playsinline></video>')
                    tw.append(f'tl.fromTo("#bg{k}",{{scale:1.0,x:0}},{{scale:1.04,x:-20,duration:{d:.3f},ease:"none"}},{o0:.3f});')
                    tw.append(f'tl.fromTo("#cd{k}",{{scale:0.985}},{{scale:1.0,duration:{d:.3f},ease:"none"}},{o0:.3f});')
                else:
                    clips.append(f'<div class="shot" id="s{k}"><div class="punch" id="p{k}"><video id="v{k}" class="clip" src="assets/{src}" '
                                 f'data-start="{o0:.3f}" data-duration="{d:.3f}" data-track-index="0" muted playsinline></video></div></div>')
                    tw.append(f'tl.fromTo("#s{k}",{{scale:1}},{{scale:1.03,duration:{d:.3f},ease:"none"}},{o0:.3f});')
                if it["kind"] == "SU":
                    said = fix_spelling(it["su"]["said"], self.P.LINES[it["su"]["line"]]["text"] + " " + self.script_caps()).replace('"', "&quot;").replace("<", "")
                    clips.append(f'<div id="cap{k}" class="clip cap" data-start="{o0 + 0.1:.3f}" data-duration="{d - 0.1:.3f}" data-track-index="5">'
                                 f'<div id="capt{k}" class="captx {fam}">&ldquo;{said}&rdquo;</div></div>')
                    tw.append(f'tl.fromTo("#capt{k}",{{opacity:0,y:18}},{{opacity:1,y:0,duration:0.25,ease:"power3.out"}},{o0 + 0.1:.3f});')
                if k in ann_k:
                    c = ann_k[k]
                    fs0, (W0, H0) = _faces(self.clip_of(k), 0.45)
                    if len(fs0) == 1:
                        x, y, w, h = fs0[0]
                        sx, sy = 1920 / W0, 1080 / H0
                        cx, cy = (x + w / 2) * sx, (y + h / 2) * sy
                        img = f"ann_{k}.jpg"
                        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "0.45", "-i", self.clip_of(k), "-frames:v", "1",
                                        "-vf", "scale=1920:1080", os.path.join(A, img)])
                        parts = [x for x in c["chars"][0].split() if x.rstrip(".").lower() not in
                                 ("dr", "mr", "mrs", "ms", "miss", "principal", "coach", "pastor", "officer", "detective", "agent")]
                        name = (parts or c["chars"][0].split())[0]
                        if c["chars"][0].split()[0].rstrip(".").lower() in ("dr", "mr", "mrs", "ms") and len(parts) >= 1:
                            name = c["chars"][0].split()[0] + " " + parts[-1]      # "Dr. Sturgis"
                        name = name.upper() if st["upper"] else name
                        twid = len(name) * 44 + 20                    # name width estimate (74 px hand font)
                        left = cx > 960                              # face on the right -> name on its left
                        fw, fh = w * sx, h * sy
                        ty = max(110, cy - fh * 1.15)
                        if left:
                            tx = max(60, cx - fw * 0.6 - 340 - twid)
                            ax0, ay0 = tx + twid + 15, ty + 50
                            ax1, ay1 = cx - fw * 0.55, cy - fh * 0.1
                        else:
                            tx = min(1860 - twid, cx + fw * 0.6 + 340)
                            ax0, ay0 = tx - 15, ty + 50
                            ax1, ay1 = cx + fw * 0.55, cy - fh * 0.1
                        an = self.P.LINES[self.line_of(it)].get("annotate")
                        sub = ""                                   # agreed: arrow + name only
                        hold = min(1.6, d - 0.3)
                        clips.append(f'<div id="an{k}" class="clip ann" style="--acc:{st["accent"]}" data-start="{o0:.3f}" data-duration="{hold:.3f}" data-track-index="8">'
                                     f'<img src="assets/{img}"><svg viewBox="0 0 1920 1080">'
                                     f'<path id="ap{k}" d="M{ax0:.0f},{ay0:.0f} Q{(ax0 + ax1) / 2:.0f},{min(ay0, ay1) - 70:.0f} {ax1:.0f},{ay1:.0f}" fill="none" stroke="#fff" stroke-width="9" stroke-linecap="round" pathLength="1" stroke-dasharray="1" stroke-dashoffset="1"/>'
                                     f'</svg><div class="anntx" id="at{k}" style="left:{tx:.0f}px;top:{ty:.0f}px">{name}<span class="annsub">{sub}</span></div>'
                                     f'<div class="flash" id="af{k}"></div></div>')
                        tw += [f'tl.fromTo("#af{k}",{{opacity:0.8}},{{opacity:0,duration:0.15}},{o0:.3f});',
                               f'tl.to("#ap{k}",{{attr:{{"stroke-dashoffset":0}},duration:0.3,ease:"power2.out"}},{o0 + 0.15:.3f});',
                               f'tl.fromTo("#at{k}",{{opacity:0,scale:0.8}},{{opacity:1,scale:1,duration:0.25,ease:"back.out(2)"}},{o0 + 0.3:.3f});']
            elif it["kind"] == "SPLIT" and k in self.CLIPS:
                Lc, Rc = self.CLIPS[k]

                def objpos(path):
                    fs1, (W1, H1) = _faces(path, 1.0)
                    if not fs1:
                        return "50% 40%"
                    f1 = max(fs1, key=lambda f: f[2] * f[3])
                    return f"{min(100, max(0, (f1[0] + f1[2] / 2) / W1 * 100)):.0f}% 40%"
                lp_, rp_ = os.path.join(self.SRC, os.path.basename(Lc[1])), os.path.join(self.SRC, os.path.basename(Rc[1]))
                kl, kr = self.P.SH[it["left"]]["ep"][0], self.P.SH[it["right"]]["ep"][0]
                logos = ""
                if kl != kr:
                    logos = (f'<img class="slogo l" id="lgl{k}" src="assets/logo_{kl}.png">' if os.path.exists(os.path.join(A, f"logo_{kl}.png")) else "") + \
                            (f'<img class="slogo r" id="lgr{k}" src="assets/logo_{kr}.png">' if os.path.exists(os.path.join(A, f"logo_{kr}.png")) else "")
                clips.append(f'<div class="half left" id="hl{k}"><video id="vl{k}" class="clip" style="object-position:{objpos(lp_)}" src="assets/{os.path.basename(Lc[1])}" data-start="{o0:.3f}" data-duration="{d:.3f}" data-track-index="0" muted playsinline></video></div>')
                clips.append(f'<div class="half right" id="hr{k}"><video id="vr{k}" class="clip" style="object-position:{objpos(rp_)}" src="assets/{os.path.basename(Rc[1])}" data-start="{o0:.3f}" data-duration="{d:.3f}" data-track-index="1" muted playsinline></video></div>')
                clips.append(f'<div id="sp{k}" class="clip splitui" data-start="{o0:.3f}" data-duration="{d:.3f}" data-track-index="2"><div class="divider" id="dv{k}"></div>{logos}</div>')
                tw += [f'tl.fromTo("#hl{k}",{{xPercent:-100}},{{xPercent:0,duration:0.45,ease:"power3.out"}},{o0:.3f});',
                       f'tl.fromTo("#hr{k}",{{xPercent:100}},{{xPercent:0,duration:0.45,ease:"power3.out"}},{o0:.3f});',
                       f'tl.fromTo("#dv{k}",{{scaleY:0}},{{scaleY:1,duration:0.4,ease:"power3.out"}},{o0 + 0.15:.3f});']
                if logos:
                    tw.append(f'tl.fromTo("#sp{k} .slogo",{{opacity:0,y:-30,scale:0.8}},{{opacity:1,y:0,scale:1,duration:0.5,ease:"back.out(1.8)",stagger:0.12}},{o0 + 0.25:.3f});')
            elif it["kind"] == "CUTOUT":
                s2 = it["show"]
                st2 = self.style(s2)
                sh = self.shows[s2]
                cut = assets.cutout(s2, sh["cast_dir"], it["photo"]) if sh.get("cast_dir") else None
                if cut:
                    shutil.copy(cut, os.path.join(A, os.path.basename(cut)))
                lgfile = f"logo_{s2}.png"
                acc = st2["accent"]
                bgimg = ""
                if not cut:
                    prev = next((j for j in range(k - 1, -1, -1) if j in self.CLIPS and self.ITEMS[j]["kind"] in ("V", "SU")), None)
                    if prev is not None:
                        bgimg = f"blur_{k}.jpg"
                        subprocess.run(["ffmpeg", "-v", "error", "-y", "-sseof", "-0.4", "-i", self.clip_of(prev), "-frames:v", "1",
                                        "-vf", "scale=1920:1080", os.path.join(A, bgimg)])
                clips.append(f'<div id="co{k}" class="clip scene co" data-start="{o0:.3f}" data-duration="{d:.3f}" data-track-index="3">'
                             f'<div class="cobg" style="background:radial-gradient(circle at 60% 55%,{_shade(acc, 1.5)} 0%,{acc} 55%,{_shade(acc, 0.45)} 100%)"></div>'
                             + (f'<div class="blurcard"><img src="assets/{bgimg}"></div>' if bgimg else "")
                             + f'<div class="rays" id="ry{k}"></div>'
                             + (f'<img class="colog" id="cl{k}" src="assets/{lgfile}">' if os.path.exists(os.path.join(A, lgfile)) else "")
                             + (f'<img class="cocut" id="cc{k}" src="assets/{os.path.basename(cut)}">' if cut else "")
                             + f'<div class="coname {self.fam(s2)}" id="cn{k}">{it["name"]}</div></div>')
                tw.append(f'tl.fromTo("#ry{k}",{{rotation:0,scale:1}},{{rotation:25,scale:1.1,duration:{d:.3f},ease:"none"}},{o0:.3f});')
                if cut:
                    tw.append(f'tl.fromTo("#cc{k}",{{y:220,opacity:0,scale:0.92}},{{y:0,opacity:1,scale:1,duration:0.55,ease:"back.out(1.5)"}},{o0 + 0.05:.3f});')
                if os.path.exists(os.path.join(A, lgfile)):
                    tw.append(f'tl.fromTo("#cl{k}",{{opacity:0,scale:0.6,rotation:-6}},{{opacity:1,scale:1,rotation:-3,duration:0.5,ease:"back.out(2)"}},{o0 + 0.3:.3f});')
                tw.append(f'tl.fromTo("#cn{k}",{{opacity:0,y:30}},{{opacity:1,y:0,duration:0.4,ease:"power3.out"}},{o0 + 0.55:.3f});')
            elif it["kind"] == "STILL" and k in self.CLIPS:
                img = os.path.basename(self.CLIPS[k][0][1])
                dst = os.path.join(A, img)
                if not os.path.exists(dst):
                    shutil.copy(self.CLIPS[k][0][1] if os.path.isabs(self.CLIPS[k][0][1]) else os.path.join(self.SRC, img), dst)
                mode = rnd.choice(["framed", "framed", "kb"] if L == "B" and bgs else ["kb", "kb", "kb", "photo", "photo"])
                still_n += 1
                if mode == "framed":
                    if framed_left <= 0 or not bgn:
                        bg = rnd.choice(bgs); framed_left = 3
                        bgn = "bg_" + re.sub(r"\W", "", os.path.basename(bg).split(".")[0]) + ".jpg"
                        if not os.path.exists(os.path.join(A, bgn)):
                            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", bg, "-vf",
                                            "scale=2000:1160:force_original_aspect_ratio=increase,crop=2000:1160", os.path.join(A, bgn)])
                    framed_left -= 1
                    clips.append(f'<img class="clip fbg" id="bg{k}" src="assets/{bgn}" data-start="{o0:.3f}" data-duration="{d:.3f}" data-track-index="0">')
                    clips.append(f'<img class="clip fvid" id="cd{k}" src="assets/{img}" data-start="{o0:.3f}" data-duration="{d:.3f}" data-track-index="1">')
                    tw.append(f'tl.fromTo("#bg{k}",{{scale:1.0,x:0}},{{scale:1.04,x:-20,duration:{d:.3f},ease:"none"}},{o0:.3f});')
                    tw.append(f'tl.fromTo("#cd{k}",{{scale:1.0}},{{scale:1.06,duration:{d:.3f},ease:"none"}},{o0:.3f});')
                elif mode == "photo":
                    rot = rnd.choice([-2.2, -1.4, 1.4, 2.2])
                    clips.append(f'<div id="ph{k}" class="clip photo" data-start="{o0:.3f}" data-duration="{d:.3f}" data-track-index="0">'
                                 f'<img class="phbg" src="assets/{img}"><img class="phpic" id="pp{k}" src="assets/{img}"></div>')
                    tw.append(f'tl.fromTo("#pp{k}",{{scale:0.93,rotation:{rot},y:30,opacity:0.2}},{{scale:1.0,rotation:{rot / 3:.2f},y:0,opacity:1,duration:0.45,ease:"power3.out"}},{o0:.3f});')
                    tw.append(f'tl.to("#pp{k}",{{scale:1.04,duration:{max(0.2, d - 0.45):.3f},ease:"none"}},{o0 + 0.45:.3f});')
                else:
                    kb = still_n % 4
                    a0, a1 = [("scale:1.0,x:0,y:0", "scale:1.09,x:0,y:-10"), ("scale:1.10,x:0,y:0", "scale:1.0,x:0,y:0"),
                              ("scale:1.08,x:50,y:0", "scale:1.08,x:-50,y:0"), ("scale:1.08,x:-50,y:0", "scale:1.08,x:50,y:0")][kb]
                    clips.append(f'<img class="clip kbimg" id="st{k}" src="assets/{img}" data-start="{o0:.3f}" data-duration="{d:.3f}" data-track-index="0">')
                    tw.append(f'tl.fromTo("#st{k}",{{{a0}}},{{{a1},duration:{d:.3f},ease:"none"}},{o0:.3f});')
            elif it["kind"] == "BREATH":
                prev = next((j for j in range(k - 1, -1, -1) if j in self.CLIPS and self.ITEMS[j]["kind"] in ("V", "SU")), None)
                img = f"blur_{k}.jpg"
                if prev is not None:
                    subprocess.run(["ffmpeg", "-v", "error", "-y", "-sseof", "-0.4", "-i", self.clip_of(prev), "-frames:v", "1",
                                    "-vf", "scale=1920:1080", os.path.join(A, img)])
                words = it["text"].upper() if st["upper"] else it["text"]
                clips.append(f'<div id="br{k}" class="clip blurcard" data-start="{o0:.3f}" data-duration="{d:.3f}" data-track-index="3">'
                             + (f'<img id="bi{k}" src="assets/{img}">' if prev is not None else '<div class="blackbg"></div>')
                             + f'<div class="brtx {fam}" id="bt{k}">{words}</div></div>')
                if prev is not None:
                    tw.append(f'tl.fromTo("#bi{k}",{{scale:1.0}},{{scale:1.06,duration:{d:.3f},ease:"none"}},{o0:.3f});')
                tw.append(f'tl.fromTo("#bt{k}",{{opacity:0,y:30}},{{opacity:1,y:0,duration:0.45,ease:"power3.out"}},{o0 + 0.05:.3f});')
        # overlays
        last_punch, ki, kin_n = -9, 0, 0
        fx_spans = [(self.ITEMS[k]["o0"], self.ITEMS[k]["o1"]) for k in list(fxsrc) + list(ann_k)]
        for o in self.OVER:
            if o["kind"] == "PUNCH" and b["punch"]:
                t = self.out(o["t"])
                if t is None or t - last_punch < 6:
                    continue
                host = next((k for k, it in enumerate(self.ITEMS) if it["kind"] == "V" and k not in framed_items and k not in fxsrc
                             and k in self.CLIPS and it["o0"] <= t < it["o1"] - 0.3), None)
                if host is None:
                    continue
                last_punch = t
                tw.append(f'tl.to("#p{host}",{{scale:1.10,duration:0.14,ease:"power3.out"}},{t:.3f});')
            elif o["kind"] == "KIN":
                segs = self.out_clip(o["t0"], o["t1"])
                if not segs:
                    continue
                a, bb = segs[0]
                if any(it["kind"] in ("CUTOUT", "BREATH") and it["o0"] < bb and it["o1"] > a for it in self.ITEMS):
                    continue
                if any(x < bb and y > a for x, y in fx_spans):
                    continue
                kin_n += 1
                if kin_n % b["kin_every"]:
                    continue
                ki += 1
                key = self.line_show(o["line"])
                st = self.style(key)
                host = next((k for k, it in enumerate(self.ITEMS) if it["o0"] <= a + 0.05 < it["o1"]), None)
                side = "kb" if host in framed_items else ("kl" if o["side"] else "kr")
                ws = self.P.words_of(o["line"])
                toks = o["text"].split()
                spans, last_t = [], a + 0.1
                for w in toks:
                    hit = next((x for x in ws if norm(x[2]) == norm(w) and self.out(x[0]) and self.out(x[0]) >= last_t - 0.05), None)
                    t = self.out(hit[0]) if hit else last_t + 0.12
                    last_t = t
                    spans.append((w.upper() if st["upper"] else w, t))
                hi = max(range(len(toks)), key=lambda j: len(toks[j]))
                inner = " ".join(f'<span id="kw{ki}_{j}" class="kw{" hi" if j == hi else ""}">{w}</span>' for j, (w, _) in enumerate(spans))
                clips.append(f'<div id="kn{ki}" class="clip kin {side}" style="--acc:{st["accent"]}" data-start="{a:.3f}" data-duration="{bb - a:.3f}" data-track-index="6">'
                             f'<div class="kshade"></div><div class="ktx {self.fam(key)}">{inner}</div></div>')
                for j, (w, t) in enumerate(spans):
                    tw.append(f'tl.fromTo("#kw{ki}_{j}",{{opacity:0,y:40,scale:1.15}},{{opacity:1,y:0,scale:1,duration:0.18,ease:"power4.out"}},{max(a, t - 0.03):.3f});')
            elif o["kind"] == "LABEL":
                if not NAME_CARDS:
                    continue
                segs = self.out_clip(o["t0"], o["t1"])
                if not segs:
                    continue
                a, bb = segs[0]
                if any(it["kind"] == "CUTOUT" and it["o0"] - 1.0 < a < it["o1"] for it in self.ITEMS):
                    continue
                # the name card must sit on THAT person: Reacher 01 showed "WILLA FITZGERALD - Roscoe's ..." over a
                # shot of Reacher (labels were placed by script line only). Check the clips under the card's span
                # with Gemini, move the card onto the first one that shows the person, else drop it.
                placed = self.label_on_person(o, a, bb)
                if placed is None:
                    self.log(f"  label {o['name']!r} dropped: the person is not on screen at line {o['line']}")
                    continue
                a, bb = placed
                key = self.line_show(o["line"])
                st = self.style(key)
                i = o["line"]
                clips.append(f'<div id="lb{i}" class="clip lbl" style="--acc:{st["accent"]}" data-start="{a:.3f}" data-duration="{bb - a:.3f}" data-track-index="7">'
                             f'<div class="lname {self.fam(key)}" id="ln{i}">{o["name"]}</div><div class="lrole" id="lr{i}">{o["role"]}</div></div>')
                tw += [f'tl.fromTo("#ln{i}",{{opacity:0,x:-30}},{{opacity:1,x:0,duration:0.4,ease:"back.out(1.6)"}},{a:.3f});',
                       f'tl.fromTo("#lr{i}",{{opacity:0}},{{opacity:1,duration:0.3}},{a + 0.2:.3f});']
        if b["letterbox"]:
            clips = [c.replace('class="captx', 'style="bottom:160px" class="captx').replace('class="slogo', 'style="top:150px" class="slogo') for c in clips]
            clips.append(f'<div id="lbx" class="clip" data-start="0" data-duration="{self.TOTAL:.3f}" data-track-index="20"><div class="lbox t"></div><div class="lbox b"></div></div>')
        html = HTML.format(css=self.css(), total=f"{self.TOTAL:.3f}", clips="\n".join(clips), tweens="\n".join(tw))
        open(os.path.join(OUT, "index.html"), "w", encoding="utf-8").write(html)
        info = dict(look=L, depth=[(round(c["t"], 1), c["word"]) for c in depth], iso=[round(c["t"], 1) for c in iso],
                    annotations=[round(c["t"], 1) for c in ann], framed=len(framed_items), kinetic=ki,
                    stills=still_n, still_share=round(sum(it["o1"] - it["o0"] for it in self.ITEMS if it["kind"] == "STILL") / self.TOTAL, 3),
                    designed=[(it["kind"], it["o0"], it["o1"]) for it in self.ITEMS if it["kind"] in ("CUTOUT", "SPLIT")])
        json.dump(info, open(os.path.join(self.B, "look.json"), "w", encoding="utf-8"), indent=1)
        return OUT, info

    def css(self):
        fonts = "".join(f'@font-face{{font-family:"{self.fam(s["key"])}";src:url("assets/fonts/{s["style"]["font"]}");}}'
                        f'.{self.fam(s["key"])}{{font-family:"{self.fam(s["key"])}";font-weight:700;'
                        + ("text-transform:uppercase;" if s["style"]["upper"] else "")
                        + ("-webkit-text-stroke:2px #111;paint-order:stroke fill;" if s["style"].get("stroke") else "") + "}"
                        for s in self.ctx["shows"])
        return fonts + CSS


CSS = r'''
@font-face{font-family:"KAL";src:url("assets/fonts/Kalam-Bold.ttf");}
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:1920px;height:1080px;overflow:hidden;background:#000}
#root{position:relative;width:100%;height:100%;overflow:hidden;background:#000}
.shot{position:absolute;inset:0;transform-origin:50% 50%}
.punch{position:absolute;inset:0;transform-origin:50% 42%}
.shot video{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.fbg{position:absolute;left:-40px;top:-40px;width:2000px;height:1160px;object-fit:cover;filter:brightness(.92)}
.fvid{position:absolute;left:210px;top:96px;width:1500px;height:844px;object-fit:cover;border-radius:30px;
  box-shadow:0 30px 70px rgba(0,0,0,.55),0 0 0 6px rgba(255,255,255,.85)}
.kbimg{position:absolute;left:-96px;top:-54px;width:2112px;height:1188px;object-fit:cover;transform-origin:50% 45%}
.photo{position:absolute;inset:0;overflow:hidden;background:#111}
.photo .phbg{position:absolute;left:-80px;top:-60px;width:2080px;height:1200px;object-fit:cover;filter:blur(30px) brightness(.45) saturate(.8)}
.photo .phpic{position:absolute;left:250px;top:125px;width:1420px;height:799px;object-fit:cover;border:16px solid #f4f1ea;
  box-shadow:0 30px 80px rgba(0,0,0,.6);transform-origin:50% 50%}
img.fvid{object-fit:cover}
.half{position:absolute;top:0;width:960px;height:1080px;overflow:hidden}
.half.left{left:0}.half.right{left:960px}
.half video{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.splitui{position:absolute;inset:0}
.divider{position:absolute;left:954px;top:0;width:12px;height:1080px;background:#fff;box-shadow:0 0 30px rgba(0,0,0,.6);transform-origin:50% 0}
.slogo{position:absolute;top:44px;height:120px;max-width:820px;object-fit:contain;filter:drop-shadow(0 6px 14px rgba(0,0,0,.7))}
.slogo.l{left:60px}.slogo.r{right:60px}
.scene{position:absolute;inset:0;overflow:hidden}
.co .cobg{position:absolute;inset:0}
.rays{position:absolute;left:-50%;top:-50%;width:200%;height:200%;
  background:repeating-conic-gradient(from 0deg at 50% 50%,rgba(255,255,255,.13) 0deg 8deg,rgba(255,255,255,0) 8deg 20deg);transform-origin:50% 50%}
.cocut{position:absolute;right:120px;bottom:0;height:960px;max-width:980px;object-fit:contain;object-position:bottom;transform-origin:50% 100%;
  filter:drop-shadow(0 0 0 #fff) drop-shadow(5px 0 0 #fff) drop-shadow(-5px 0 0 #fff) drop-shadow(0 -5px 0 #fff) drop-shadow(0 40px 50px rgba(0,0,0,.45))}
.colog{position:absolute;left:100px;top:200px;width:620px;max-height:360px;object-fit:contain;object-position:left top;filter:drop-shadow(0 8px 20px rgba(0,0,0,.45))}
.coname{position:absolute;left:110px;top:640px;width:900px;font-size:104px;line-height:1.02;color:#fff;text-shadow:0 6px 0 rgba(0,0,0,.25),0 10px 30px rgba(0,0,0,.35)}
.blurcard{position:absolute;inset:0}
.blurcard img{position:absolute;inset:-60px;width:2040px;height:1200px;object-fit:cover;filter:blur(26px) brightness(.42) saturate(.8)}
.blackbg{position:absolute;inset:0;background:#0b0b0b}
.brtx{position:absolute;left:160px;right:160px;top:420px;text-align:center;font-size:104px;line-height:1.05;color:#fff;text-shadow:0 8px 30px rgba(0,0,0,.6)}
.kin{position:absolute;inset:0}
.kshade{position:absolute;inset:0;background:linear-gradient(90deg,rgba(0,0,0,.5),rgba(0,0,0,0) 60%)}
.kin.kr .kshade{background:linear-gradient(270deg,rgba(0,0,0,.5),rgba(0,0,0,0) 60%)}
.ktx{position:absolute;top:280px;width:880px;font-size:108px;line-height:1.02;color:#fff;text-shadow:0 6px 24px rgba(0,0,0,.65)}
.kin.kl .ktx{left:110px}.kin.kr .ktx{right:110px;text-align:right}
.kin.kb .kshade{background:none}.kin.kb .ktx{left:210px;right:210px;width:auto;top:948px;font-size:74px;text-align:center}
.kw{display:inline-block}
.kw.hi{color:var(--acc)}
.cap{position:absolute;inset:0}
.captx{position:absolute;left:120px;right:120px;bottom:90px;text-align:center;font-size:62px;color:#fff;text-shadow:0 4px 0 #000,0 0 18px rgba(0,0,0,.8)}
.lbl{position:absolute;inset:0}
.lname{position:absolute;left:110px;top:800px;font-size:86px;color:#fff;text-shadow:0 5px 0 rgba(0,0,0,.35),0 0 24px rgba(0,0,0,.5)}
.lrole{position:absolute;left:116px;top:905px;font-family:"KAL";font-size:44px;color:var(--acc);text-shadow:0 2px 8px rgba(0,0,0,.8)}
.ann{position:absolute;inset:0}
.ann img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;filter:saturate(.55) brightness(.8)}
.ann svg{position:absolute;left:0;top:0;width:1920px;height:1080px}
.anntx{position:absolute;font-family:"KAL";font-size:74px;color:#fff;text-shadow:0 4px 0 #000,0 0 16px rgba(0,0,0,.7);white-space:nowrap}
.annsub{display:block;font-size:52px;color:var(--acc)}
.flash{position:absolute;inset:0;background:#fff;opacity:0}
.lbox{position:absolute;left:0;right:0;height:132px;background:#000;z-index:50}
.lbox.t{top:0}.lbox.b{bottom:0}
'''

HTML = '''<!doctype html>
<html lang="en" data-resolution="landscape"><head><meta charset="UTF-8"><meta name="viewport" content="width=1920, height=1080">
<script src="assets/gsap.min.js"></script><style>{css}</style></head>
<body><div id="root" data-composition-id="main" data-start="0" data-duration="{total}" data-width="1920" data-height="1080">
{clips}
</div><script>
const tl = gsap.timeline({{ paused: true }});
{tweens}
window.__timelines["main"] = tl;
tl.seek(0);
</script></body></html>'''
