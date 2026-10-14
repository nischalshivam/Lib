"""plan.json -> output timeline (sound-ups shift time) -> cut every clip safely.
Safety: never past a real cut, never into a credits/text zone, split sides keep the
SAME (identity-checked) person for the whole line, final Gemini burned-text check.
Writes state.json + assets/c###*.mp4 (+ .wav for sound-ups)."""
from __future__ import annotations

import concurrent.futures as cf, glob, io, json, os, re, subprocess, threading

from .common import lp, vision, faces, is_person, frame_jpeg, mark_unusable
from .plan import Plan, norm
from . import textzones

# per video (2026-10-14): clip cutting is ffmpeg/CPU, the burned-text check is one Gemini call per clip
CUT_THREADS = int(os.environ.get("EDITV2_CUT_THREADS", "8"))
TEXT_THREADS = int(os.environ.get("EDITV2_TEXT_THREADS", "16"))


class Cutter:
    def __init__(self, ctx, log=print):
        self.ctx, self.log = ctx, log
        self.P = Plan(ctx)
        self.B = ctx["build"]
        self.A = os.path.join(self.B, "assets")
        os.makedirs(self.A, exist_ok=True)
        pl = json.load(open(os.path.join(self.B, "plan.json"), encoding="utf-8"))
        self.EDL, self.V, self.RR, self.SU, self.LT = pl["edl"], pl["verify"], pl["real"], pl["soundups"], pl["line_times"]
        self.REPL = []
        for s in self.SU:
            t0, t1 = self.LT[s["line"]]
            d = (s["src1"] - s["src0"]) + 0.30
            if s.get("mode") == "insert":
                self.REPL.append((t1, t1, d, s))           # narration kept, original moment after the line
            else:
                self.REPL.append((t0, t1, d, s))           # the original moment replaces the line
        self.REPL.sort(key=lambda r: r[0])
        self._cat, self._dark = {}, {}
        self._used, self._lock, self._still_hashes = {}, threading.Lock(), []     # every second of source is shown ONCE per video

    # ---------------------------------------------------------------- time map
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
            if rb <= ra:                                   # insert point: split, never stretch over it
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

    def timeline(self):
        items = []
        for e in self.EDL:
            for a, b in self.out_clip(e["t0"], e["t1"]):
                items.append(dict(e, o0=a, o1=b))
        for a, b, d, s in self.REPL:
            o0 = self.out(a - 1e-6) if b > a else self.out(a)
            if o0 is None:
                o0 = a
            if b <= a:                                     # insert: starts right after the line
                o0 = self.out(a - 1e-6) or a
            items.append(dict(kind="SU", o0=o0, o1=o0 + d, su=s, line=s["line"]))
        items.sort(key=lambda x: x["o0"])
        for k in range(len(items) - 1):
            items[k]["o1"] = items[k + 1]["o0"]
        merged = []
        for it in items:
            if merged and it["kind"] in ("V", "STILL") and it["o1"] - it["o0"] < 0.9 and merged[-1]["kind"] in ("V", "STILL", "SPLIT"):
                merged[-1]["o1"] = it["o1"]
                continue
            merged.append(it)
        items = merged
        for k, it in enumerate(items):                     # safety: a sound-up slot = its own length, never more
            if it["kind"] == "SU":
                d = (it["su"]["src1"] - it["su"]["src0"]) + 0.30
                if it["o1"] - it["o0"] > d + 0.05 and k + 1 < len(items):
                    items[k + 1]["o0"] = it["o0"] + d
                    it["o1"] = it["o0"] + d
        end_n = self.LT[-1][1]
        total = max(items[-1]["o1"], self.out(end_n) or end_n) + 0.6
        items[-1]["o1"] = total
        return items, total

    # ---------------------------------------------------------------- no repeats
    def claim(self, video, a, b, want, min_len=0.6, force=False):
        """First free stretch of [a, b] (>= min_len, <= want) not shown anywhere else in this video; registers it."""
        g = 0.35
        with self._lock:
            used = self._used.setdefault(video, [])
            if force:
                used.append((a, b))
                return a, b
            free = [(a, b)]
            for x, y in sorted(used):
                nxt = []
                for fa, fb in free:
                    if fb <= x - g or fa >= y + g:
                        nxt.append((fa, fb))
                    else:
                        if fa < x - g:
                            nxt.append((fa, x - g))
                        if fb > y + g:
                            nxt.append((y + g, fb))
                free = nxt
            for fa, fb in free:
                if fb - fa >= min_len:
                    take = (fa, fa + min(want, fb - fa))
                    used.append(take)
                    return take
        return None

    def dhash(self, video, t):
        from PIL import Image
        try:
            im = Image.open(io.BytesIO(frame_jpeg(video, t, 160))).convert("L").resize((9, 8))
            px = list(im.getdata())
            return sum(1 << i for i in range(64) if px[(i // 8) * 9 + i % 8] > px[(i // 8) * 9 + i % 8 + 1])
        except Exception:
            return 0

    def restructure(self, items):
        """No continuous video longer than ~7.5 s, and ~40 % of the picture time as STILL images (Ken Burns):
        long shots become video/still/video chunks; short ones alternate by the running ratio."""
        out = []
        for it in items:
            d = it["o1"] - it["o0"]
            if it["kind"] == "V" and d > 7.5:
                n = max(2, int(round(d / 4.2)))
                step = d / n
                for j in range(n):
                    out.append(dict(it, o0=it["o0"] + j * step, o1=it["o0"] + (j + 1) * step, chunk=j))
            else:
                out.append(it)
        target, vis, still, prev_still = 0.40, 0.0, 0.0, False
        for it in out:
            d = it["o1"] - it["o0"]
            if it["kind"] == "V" and 1.6 <= d <= 7.5 and it["o0"] > 6.0 and not prev_still \
                    and (still + d) / (vis + d) <= target + 0.08:
                if still / max(vis, 1e-6) < target or it.get("chunk", 0) % 2 == 1:
                    it["kind"] = "STILL"
            vis += d
            if it["kind"] == "STILL":
                still += d
            prev_still = it["kind"] == "STILL"
        self.log(f"  pictures: {sum(1 for i in out if i['kind'] == 'STILL')} stills = {100 * still / max(vis, 1):.0f}% of the time")
        return out

    # ---------------------------------------------------------------- catalogue helpers
    def cat(self, video):
        if video not in self._cat:
            c = video.rsplit(".", 1)[0] + ".catalog.json"
            try:
                self._cat[video] = mark_unusable(json.load(open(lp(c), encoding="utf-8"))["shots"])
            except Exception:
                self._cat[video] = []
        return self._cat[video]

    def zones(self, video):
        return textzones.zones(video, log=lambda *a: None)

    def dark(self, video, t):
        """Act-break fade / black frame (catalog shots don't say so)."""
        key = (video, round(t, 1))
        if key not in self._dark:
            try:
                from PIL import Image, ImageStat
                im = Image.open(io.BytesIO(frame_jpeg(video, t, 160))).convert("L")
                self._dark[key] = ImageStat.Stat(im).mean[0] < 30
            except Exception:
                self._dark[key] = False
        return self._dark[key]

    def clean_span(self, video, a, b):
        """Longest stretch of [a-0.3, b+0.3] with no real picture cut inside (0.08 s margins)."""
        a0, b0 = max(0.0, a - 0.3), b + 0.3
        r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{a0:.3f}", "-t", f"{b0 - a0:.3f}", "-i", lp(video),
                            "-vf", "scale=320:-2,select='gt(scene,0.25)',showinfo", "-an", "-f", "null", "-"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        cuts = [a0 + float(x) for x in re.findall(r"pts_time:([\d.]+)", r.stderr)]
        edges = [a0] + cuts + [b0]
        best = max(zip(edges, edges[1:]), key=lambda x: x[1] - x[0])
        return best[0] + (0.08 if best[0] > a0 else 0.0), best[1] - 0.08

    def next_safe(self, video, after):
        zones = self.zones(video)
        out = []
        for s in self.cat(video):
            if s["start"] >= after - 0.3:
                if s.get("safe") is False or any(s["start"] < b and s["end"] > a for a, b in zones):
                    continue
                if re.search(r"black (screen|frame)|fade[sd]? (to|out|in)|title card|credits", (s.get("description") or "").lower())                         or self.dark(video, (s["start"] + s["end"]) / 2):
                    continue
                out.append(s)
                if len(out) >= 16:
                    break
        return out

    def side_who(self, sid, line):
        """The person a split side stands for: named in the line, else the shot's first catalogued character."""
        key = self.P.SH[sid]["ep"][0]
        named = self.P.names_in(self.P.LINES[line]["text"], key)
        if named:
            return named[0]
        chars = self.P.SH[sid].get("characters") or []
        return chars[0].split()[0] if chars else None

    def char_shot(self, sid, line, need):
        """Long-enough shot of the person this split side stands for (same episode,
        then neighbours), identity checked on first+last frame by Gemini."""
        SH = self.P.SH[sid]
        key = SH["ep"][0]
        who = self.side_who(sid, line)
        if not who:
            return None
        show_name = self.P.show[key]["name"]
        video0 = SH["video"]
        folder = os.path.dirname(os.path.dirname(video0)) if not re.search(r"S\d{2}E\d{2}", os.path.basename(os.path.dirname(video0))) else os.path.dirname(video0)
        ep = SH["ep"][1]
        m = re.match(r"S(\d{2})E(\d{2})", ep)
        videos = [video0]
        if m:
            s_no, e_no = int(m.group(1)), int(m.group(2))
            root = self.P.show[key]["root"]
            for dlt in (1, -1, 2, -2, 3):
                code = f"S{s_no:02d}E{e_no + dlt:02d}"
                c = glob.glob(os.path.join(root, "**", f"*{code}*.catalog.json"), recursive=True)
                if c:
                    st = c[0][: -len(".catalog.json")]
                    v = next((st + e for e in (".mkv", ".mp4", ".avi", ".m4v") if os.path.exists(lp(st + e))), None)
                    if v:
                        videos.append(v)
        tried = 0
        for vid in videos:
            zones = self.zones(vid)
            pool = [s for s in self.cat(vid) if any(who in c for c in s.get("characters", []))
                    and len(s.get("characters", [])) <= 2 and s.get("safe", True) and s.get("shot_type") in ("close-up", "medium")
                    and s["end"] - s["start"] >= need + 0.4 and not any(s["start"] < b and s["end"] > a for a, b in zones)]
            pool.sort(key=lambda s: abs(s["start"] - SH["start"]) if vid == video0 else -(s["end"] - s["start"]))
            for s in pool[:6]:
                if tried >= 14:
                    return None
                tried += 1
                self.P.SH["_probe"] = dict(SH, video=vid, start=s["start"], end=s["end"])
                c0, c1 = self.P.real_range("_probe", s["start"] + 0.3)
                if c1 - c0 < need:
                    continue
                fs, (W, H) = faces(vid, (c0 + c1) / 2)
                if not fs or max(f[3] for f in fs) / H <= 0.18:
                    continue
                ok1, w1 = is_person(vid, c0 + 0.4, who, show_name)
                ok2, w2 = is_person(vid, min(c1, c0 + need) - 0.3, who, show_name)
                if ok1 and ok2:
                    self.log(f"  split side: using a verified {who} shot ({os.path.basename(vid)[:40]} {s['start']:.0f}s)")
                    return c0, c1, vid
        return None

    # ---------------------------------------------------------------- cutting
    def clip_job(self, k, it, offset):
        d = it["o1"] - it["o0"]
        outs = []
        if it["kind"] == "SU":
            s = it["su"]
            d = min(d, (s["src1"] - s["src0"]) + 0.30)        # an original moment is never stretched
            video = self.P.SH[s["sid"]]["video"]
            o = os.path.join(self.A, f"c{k:03d}.mp4")
            # picture: skip a cut that lands in the first 0.6 s (the previous shot's tail would flash)
            v0 = s["src0"] - 0.15
            a_, b_ = self.clean_span(video, v0 + 0.3, v0 + 0.9)
            vstart = a_ if a_ > v0 + 0.02 else v0
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{vstart:.3f}", "-t", f"{d + 0.3:.3f}", "-i", lp(video),
                            "-vf", f"tpad=start_mode=clone:start_duration={vstart - v0:.3f},"
                                   "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,fps=30,setsar=1,format=yuv420p",
                            "-an", "-c:v", "libx264", "-preset", "fast", "-g", "30", "-crf", "18", o], check=True)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{s['src0'] - 0.15:.3f}", "-t", f"{d:.3f}", "-i", lp(video),
                            "-vn", "-ac", "2", "-ar", "48000", "-af", "afade=t=in:d=0.08,afade=t=out:st=%.3f:d=0.15" % max(0, d - 0.15),
                            os.path.join(self.A, f"c{k:03d}.wav")], check=True)
            self.trim_edges(o, d + 0.3, head_too=False)      # a source cut just before the end flashed the next shot
            return k, [("su", o)]
        if it["kind"] == "STILL":
            sid = it["sid"]
            video = self.P.SH[sid]["video"]
            c0, c1 = self.RR[sid]
            start = max(c0, self.V.get(sid, {}).get("t", c0)) + offset
            spans = [(start, max(c1, start + 1.2))] + [(x["start"], x["end"]) for x in self.next_safe(video, max(c1, start))[:6]]
            best = None
            for a, b in spans:
                got = self.claim(video, a, b, 1.2, min_len=0.6)
                if not got:
                    continue
                t = (got[0] + got[1]) / 2
                try:
                    from PIL import Image, ImageStat
                    im = Image.open(io.BytesIO(frame_jpeg(video, t, 320))).convert("L")
                    bright = ImageStat.Stat(im).mean[0]
                    fs, (W, H) = faces(video, t)
                    score = (2 if fs and max(f[3] for f in fs) / H > 0.12 else 0) + (1 if bright > 45 else -3)
                except Exception:
                    score = -5
                if best is None or score > best[0]:
                    best = (score, t)
                if score >= 3:
                    break
            t = best[1] if best else start + 0.3
            for alt in [t] + [x["start"] + 0.5 for x in self.next_safe(video, t + 2)[:8]]:
                hsh = self.dhash(video, alt)
                with self._lock:
                    similar = any(bin(hsh ^ h2).count("1") <= 10 for h2 in self._still_hashes)
                    if not similar or alt == t and False:
                        self._still_hashes.append(hsh)
                if not similar:
                    t = alt
                    break
            o = os.path.join(self.A, f"c{k:03d}s.jpg")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", lp(video), "-frames:v", "1",
                            "-vf", "scale=2112:1188:force_original_aspect_ratio=increase:flags=lanczos,crop=2112:1188,"
                                   "unsharp=5:5:0.6,eq=contrast=1.04:saturation=1.04", "-q:v", "2", o], check=True)
            return k, [("s", o)]
        sids = [("v", it["sid"])] if it["kind"] == "V" else [("l", it["left"]), ("r", it["right"])]
        for tag, sid in sids:
            video = self.P.SH[sid]["video"]
            c0, c1 = self.RR[sid]
            start = max(c0, self.V.get(sid, {}).get("t", c0)) + (offset if tag == "v" else 0.0)
            need = d + 0.3
            zones = self.zones(video)
            unsafe = [s for s in self.cat(video) if s.get("safe") is False] + [dict(start=a, end=b) for a, b in zones]
            c1 = min([c1] + [s["start"] - 0.05 for s in unsafe if s["start"] > start])
            if any(s["start"] <= start < s["end"] for s in unsafe):
                c1 = start
            avail = max(0.0, c1 - start)
            override = None
            side_bad = False
            if tag in ("l", "r") and avail >= need * 0.8:
                who = self.side_who(sid, it["line"])
                if who:                                   # the catalog lies about who is on screen -> check
                    sn = self.P.show[self.P.SH[sid]["ep"][0]]["name"]
                    ok1, _ = is_person(video, start + 0.3, who, sn)
                    ok2, _ = is_person(video, min(c1, start + need) - 0.3, who, sn)
                    side_bad = not (ok1 and ok2)
            if tag in ("l", "r") and (avail < need * 0.8 or side_bad):
                alt = self.char_shot(sid, it["line"], need)
                if alt:
                    start, c1, override = alt
                    avail = c1 - start
            src = override or video
            pieces, have, last_end = [], 0.0, max(c1, start)
            got = self.claim(src, start, c1, need / 0.75, min_len=0.4) if avail > 0.4 else None
            if got:
                ga, gb = got
                avail = gb - ga
                if avail >= need * 0.75:
                    ln = min(need, avail)
                    pieces.append((ga, ln, max(0.75, min(1.0, avail / need))))
                    have = need
                else:
                    pieces.append((ga, avail, 1.0))
                    have = avail
                last_end = max(last_end, gb)
            for nx in ([] if (tag in ("l", "r") and override) else self.next_safe(video, last_end)):
                if have >= need - 0.05:
                    break
                a, b = self.clean_span(video, nx["start"], nx["end"])     # real cuts, not catalog guesses
                got = self.claim(video, a, b, need - have, min_len=0.8)
                if got and (self.dark(video, got[0] + 0.25) or self.dark(video, (got[0] + got[1]) / 2)):
                    continue                                  # night exterior / fade: the piece itself is too dark
                if got:
                    pieces.append((got[0], got[1] - got[0], 1.0))
                    have += got[1] - got[0]
            if not pieces:                                  # everything near here already shown: go further on
                for nx in self.next_safe(video, last_end + 60):
                    a, b = self.clean_span(video, nx["start"], nx["end"])
                    got = self.claim(video, a, b, need - have, min_len=0.8)
                    if got:
                        pieces.append((got[0], got[1] - got[0], 1.0))
                        have += got[1] - got[0]
                    if have >= need - 0.05:
                        break
            if not pieces:
                pieces = [(start, min(need, max(0.5, avail)), 1.0)]
                have = pieces[0][1]
            o = os.path.join(self.A, f"c{k:03d}{tag}.mp4")
            args, fc = [], []
            for j, (ss, ln, rate) in enumerate(pieces):
                args += ["-ss", f"{ss:.3f}", "-t", f"{ln + 0.2:.3f}", "-i", lp(override or video)]
                fc.append(f"[{j}:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,eq=contrast=1.04:saturation=1.03,"
                          f"setpts=(PTS-STARTPTS)/{rate:.4f},fps=30,setsar=1,format=yuv420p,trim=duration={ln / rate:.3f}[p{j}]")
            if have < need - 0.05:
                fc[-1] = fc[-1].replace(f"[p{len(pieces) - 1}]", f",tpad=stop_mode=clone:stop_duration={need - have + 0.2:.3f}[p{len(pieces) - 1}]")
            fc.append("".join(f"[p{j}]" for j in range(len(pieces))) + f"concat=n={len(pieces)}:v=1:a=0[out]")
            subprocess.run(["ffmpeg", "-v", "error", "-y", *args, "-filter_complex", ";".join(fc), "-map", "[out]",
                            "-an", "-c:v", "libx264", "-preset", "fast", "-g", "30", "-crf", "18", "-t", f"{need:.3f}", o], check=True)
            self.trim_edges(o, need)
            outs.append((tag, o))
        return k, outs

    def trim_edges(self, path, need, head_too=True):
        """A source cut in the first 0.45 s / last 0.5 s of a clip = a few frames of another shot (a flash).
        Cut them off; hold the last good frame for the few missing frames. head_too=False for sound-ups: their start
        is already cut-safe (vstart) and must stay in lip-sync - only the tail is held."""
        r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-vf", "scale=320:-2,select='gt(scene,0.3)',showinfo",
                            "-an", "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace")
        cuts = [float(x) for x in re.findall(r"pts_time:([0-9.]+)", r.stderr)]
        vis = need - 0.3                                    # the clip carries 0.3 s of spare tail nobody sees
        head = max([c for c in cuts if c <= 0.45], default=0.0) if head_too else 0.0
        # Reacher 02: 6 flashes of 0.23 s - a sound-up's source cut 7 frames before its end, then a STILL
        tail = min([c for c in cuts if vis - 0.5 <= c <= vis + 0.05], default=need)
        if head <= 0.0 and tail >= need:
            return False
        a, b = (head + 0.034 if head > 0 else 0.0), (tail - 0.034 if tail < need else need)
        if b - a < 0.5:
            return False
        tmp = path + ".edge.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-vf",
                        f"trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration={need - (b - a) + 0.1:.3f}",
                        "-an", "-c:v", "libx264", "-preset", "fast", "-g", "30", "-crf", "18", "-t", f"{need:.3f}", tmp], check=True)
        os.replace(tmp, path)
        return True

    def text_check(self, path):
        from PIL import Image, ImageDraw, ImageFont
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                                   capture_output=True, text=True).stdout or 0)
        ts = [0.05, dur / 3, 2 * dur / 3, max(0.05, dur - 0.08)]
        G = Image.new("RGB", (1280, 720))
        f = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", 34)
        for j, t in enumerate(ts):
            r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1", "-vf", "scale=640:360",
                                "-f", "image2", "-c:v", "mjpeg", "-"], capture_output=True)
            try:
                im = Image.open(io.BytesIO(r.stdout)).convert("RGB")
            except Exception:
                continue
            d = ImageDraw.Draw(im); d.rectangle([0, 0, 44, 40], fill="black"); d.text((8, 2), str(j + 1), font=f, fill="yellow")
            G.paste(im, ((j % 2) * 640, (j // 2) * 360))
        b = io.BytesIO(); G.save(b, "JPEG", quality=80)
        v = vision("Four numbered frames from a TV/film clip. Does any frame contain BURNED-IN TEXT laid over the picture: "
                   "credits (actor/writer/producer names), title cards, captions/subtitles, channel logos? Ignore text that is "
                   'part of the set. Reply ONLY JSON {"first_text_frame":0-4,"what":"<short>"} (0 = none).', b.getvalue())
        try:
            return int((v or {}).get("first_text_frame") or 0), ts, (v or {}).get("what", "")
        except (TypeError, ValueError):
            return 0, ts, ""

    def fix_text(self, path, first, ts):
        tmp = path + ".fix.mp4"
        dur = ts[-1] + 0.08
        if first == 1:
            vf = "gblur=sigma=28,eq=brightness=-0.22:saturation=0.7"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-vf", vf, "-an", "-c:v", "libx264", "-crf", "18", tmp], check=True)
        else:
            keep = (ts[first - 2] + ts[first - 1]) / 2 - 0.25
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-vf",
                            f"trim=duration={keep:.3f},tpad=stop_mode=clone:stop_duration={dur - keep + 0.1:.3f}",
                            "-an", "-c:v", "libx264", "-crf", "18", "-t", f"{dur:.3f}", tmp], check=True)
        os.replace(tmp, path)

    def run(self):
        items, total = self.timeline()
        items = self.restructure(items)
        for it in items:                                   # the original moments own their seconds
            if it["kind"] == "SU":
                s = it["su"]
                self.claim(self.P.SH[s["sid"]]["video"], s["src0"] - 0.6, s["src0"] + (it["o1"] - it["o0"]) + 0.4, 0, force=True)
        consumed, jobs = {}, []
        for k, it in enumerate(items):
            if it["kind"] in ("V", "STILL"):
                jobs.append((k, it, consumed.get(it["sid"], 0.0)))
                consumed[it["sid"]] = consumed.get(it["sid"], 0.0) + (it["o1"] - it["o0"])
            elif it["kind"] in ("SPLIT", "SU"):
                jobs.append((k, it, 0.0))
        self.log(f"  cutting {len(jobs)} clips...")
        with cf.ThreadPoolExecutor(CUT_THREADS) as ex:
            CLIPS = dict(ex.map(lambda j: self.clip_job(*j), jobs))
        allp = [p for v in CLIPS.values() for _, p in v if p.endswith(".mp4")]
        with cf.ThreadPoolExecutor(TEXT_THREADS) as ex:
            TC = dict(zip(allp, ex.map(self.text_check, allp)))
        fixed = 0
        for p, (first, ts, what) in TC.items():
            if first:
                fixed += 1
                self.fix_text(p, first, ts)
        self.log(f"  burned-in text found and fixed in {fixed} clips")
        st = dict(items=items, clips={str(k): v for k, v in CLIPS.items()}, total=total,
                  repl=[(a, b, d, s) for a, b, d, s in self.REPL], text_fixed=fixed)
        json.dump(st, open(os.path.join(self.B, "state.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=0, default=str)
        return st
