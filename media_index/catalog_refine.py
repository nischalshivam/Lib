"""Refine a finished catalogue so every shot is a REAL camera shot with VERIFIED fields  (VERSION 5, 2026-10-10).

v5 = v4 + dissolves must join two different pictures (corr < 0.45): House S1 got ~12 false dissolves per episode.

v3 -> v4 (adversarial check of v3): one linear subtitle fit could not follow subtitles whose offset changes during an
episode (B99: last act 1.5-2 s early); dark-scene cuts (score 0.05-0.08), fades and dissolves were missed; old
integer-second boundaries survived; Gemini could name characters from other seasons (whole-show cast list); a failed
12-shot grid left its shots unverified but 'safe'; transient Drive errors were taken for broken files.

ONE read of the source (the slow part on Drive) produces everything, so no field ever needs the video again:
  per-frame scene scores (kept as cut_scores), 2-fps 960x540 frames, a 1-fps 320x180 strip archived to D:\\frames,
  16 kHz mono audio of the chosen (English if tagged) track, the best English TEXT subtitle stream.
Then:
  1. boundaries = real cuts only: hard (score >= 0.27); soft (>= 0.04 and >= 8x the local median: dark scenes,
     shot/reverse-shot; each shot keeps `cut_in` = its starting boundary's score); dips to black; cross-dissolves
     (the middle frame is a blend of the frames 1 s before and after, and no camera move explains it). Each new shot
     inherits its text from the original shot it overlaps most; ghost shots past the end are dropped;
  2. dialogue: every candidate (all sidecar rips, embedded track, stored cues; ads / lyrics / mojibake removed) is
     aligned CUE BY CUE to the audio: English audio -> word timestamps of a fast whisper pass (frame-rate drift and
     offset jumps handled); other languages -> speech activity per 5-min block. Subtitle text, audio timing.
     Whisper text (in the audio's own language) only if nothing aligns;
  3. cast allowed in this episode = characters seen in >= 25 % of this season's episodes + cast members named in
     this episode's dialogue (films: their own cast list);
  4. Gemini 4x3 grids judge EVERY shot (failed grids retried): ok / new description, visible cast, burned-in text,
     logo, shot_type, nsfw; faces without enough names get a second, larger 2x2 look; a dense 1-fps text scan of
     the first 10 min and last 3 min gives text_zones; an unverified shot is never 'safe'.
Writes the catalogue in place (tmp + os.replace, .v1.bak kept) with "refined": {...}.
    py -m media_index.catalog_refine <list.json|video> ...
"""
from __future__ import annotations

import bisect, concurrent.futures as cf, contextlib, hashlib, io, json, os, re, shutil, subprocess, sys, tempfile, time

from . import catalog, subtitles

VERSION = 5
SCENE_HARD, SCENE_SOFT, SOFT_RATIO, MIN_SHOT = 0.27, 0.04, 8.0, 0.5
FW, FH = 960, 540                         # extracted frame size (2 fps)
TILE_W, TILE_H, COLS, ROWS = 480, 270, 4, 3
PER_GRID = COLS * ROWS
STRIP_DIR = r"D:\frames"                  # 1-fps 320x180 strips: future fields never need the source again
CASTFREQ_DIR = r"D:\Lib\libjobs\castfreq"
TEXT_HEAD, TEXT_TAIL = 600, 180           # dense burned-in-text scan (credits, titles)
SHOT_TYPES = ("wide", "medium", "close-up", "insert", "aerial")
NSFW = ("none", "suggestive", "nudity", "sex", "gore")
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
LIB_DIRS = ("Movies DATA", "Movies DATA 2", "Movies DATA 3", "Cartoons DATA")
EN_STOP = {"the", "you", "i", "to", "a", "and", "is", "it", "of", "that", "what", "me", "my", "this", "in", "don't", "we",
           "your", "not", "be", "have", "for", "on", "no", "just", "he", "she", "do", "know", "are", "was"}
DROP_KEYS = {"verified", "fixed", "inherited", "text", "logo", "nsfw", "explicit", "safe", "id", "luma", "faces", "face_h",
             "dialogue", "_frame", "_inherited", "transition", "blocked", "cut_in"}


class BadSource(RuntimeError):
    """The file itself is unusable (truncated, no video, broken container) -> needs a new download."""


def _nop(*a, **k):
    pass


def _lp(p: str) -> str:
    return "\\\\?\\" + os.path.abspath(p) if os.name == "nt" and len(p) >= 250 and not p.startswith("\\\\?\\") else p


def norm_shot_type(x) -> str:
    """Any spelling ('medium close-up', 'Medium shot', 'wide establishing') -> wide|medium|close-up|insert|aerial."""
    x = str(x or "").lower().strip()
    if not x:
        return "medium"
    if "aerial" in x or "drone" in x or "bird" in x:
        return "aerial"
    if "insert" in x or "detail" in x:
        return "insert"
    if "close" in x or x == "cu":
        return "close-up"
    if "wide" in x or "establish" in x or "long shot" in x or "full shot" in x:
        return "wide"
    return "medium"


# ---------------------------------------------------------------- probe
_BROKEN = re.compile(r"moov atom not found|Invalid data found|could not find codec parameters|invalid header|"
                     r"EBML header parsing failed|truncat|End of file", re.I)


def _probe_once(video):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration:stream=index,codec_type,codec_name,channels,duration:stream_tags=language,title,DURATION"
                            ":stream_disposition=forced,default",
                            "-of", "json", _lp(video)], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=180)
    except subprocess.TimeoutExpired:
        raise OSError("cannot read: ffprobe timed out (Drive?)")
    try:
        j = json.loads(r.stdout or "{}")
    except ValueError:
        j = {}
    st = j.get("streams") or []
    try:
        dur = float((j.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    has_video = any(s.get("codec_type") == "video" and s.get("codec_name") not in ("mjpeg", "png") for s in st)
    return has_video and dur > 1, st, dur, (r.stderr or "").strip()


def probe(video: str) -> dict:
    """Streams + duration. A file that cannot be opened right now (Drive) raises OSError('cannot read ...'), which is
    temporary; only a file that opens but is broken - twice, 60 s apart - raises BadSource."""
    if not os.path.exists(_lp(video)):
        raise OSError("cannot read: file not found (Drive?)")
    ok, st, dur, err = _probe_once(video)
    if not ok:
        time.sleep(60)                        # Drive may have handed out a half-fetched file: ask once more
        ok, st, dur, err2 = _probe_once(video)
        err = err2 or err
    if not ok:
        if _BROKEN.search(err) or st:
            raise BadSource(f"bad source: {err[:160] or 'no video stream / zero duration'}")
        raise OSError(f"cannot read: ffprobe gave nothing ({err[:120]})")
    vdur = None                               # the video stream's own length (MKV keeps it in a DURATION tag)
    v0 = next(s for s in st if s.get("codec_type") == "video" and s.get("codec_name") not in ("mjpeg", "png"))
    try:
        vdur = float(v0.get("duration"))
    except (TypeError, ValueError):
        m = re.match(r"(\d+):(\d+):([\d.]+)", str((v0.get("tags") or {}).get("DURATION") or ""))
        if m:
            vdur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    aud = [dict(n=k, lang=((s.get("tags") or {}).get("language") or "und").lower(), codec=s.get("codec_name"),
                ch=int(s.get("channels") or 0), title=(s.get("tags") or {}).get("title") or "",
                default=bool((s.get("disposition") or {}).get("default")))
           for k, s in enumerate(x for x in st if x.get("codec_type") == "audio")]
    subs = [dict(n=k, lang=((s.get("tags") or {}).get("language") or "und").lower(), codec=(s.get("codec_name") or "").lower(),
                 title=(s.get("tags") or {}).get("title") or "", forced=bool((s.get("disposition") or {}).get("forced")))
            for k, s in enumerate(x for x in st if x.get("codec_type") == "subtitle")]
    return dict(duration=dur, vduration=vdur if vdur and vdur > 1 else None, audio=aud, subs=subs)


_COMMENTARY = re.compile(r"comment|director|descriptive|description|narrat|\bAD\b|isolated|score|music only", re.I)


def pick_audio(meta) -> int | None:
    """English track, never a commentary/description track; among several English tracks the one with the most
    channels = the real mix (House S05E19: default stereo track = untitled writers' commentary, real 5.1 mix 2nd)."""
    a = meta["audio"]
    if not a:
        return None
    ok = [x for x in a if not _COMMENTARY.search(x.get("title", ""))] or a
    en = [x for x in ok if x["lang"] in ("eng", "en", "english")]
    pool = en or ok
    best = max(pool, key=lambda x: (x.get("ch", 0), x.get("default", False), -x["n"]))
    return best["n"]


def pick_text_sub(meta) -> int | None:
    """Best embedded TEXT subtitle stream (English, else untagged); bitmap tracks cannot become text."""
    t = [x for x in meta["subs"] if x["codec"] in TEXT_SUB_CODECS and not x["forced"]
         and not re.search(r"forced|sign", x["title"], re.I)]
    for want in (("eng", "en", "english"), ("und",)):
        hit = [x for x in t if x["lang"] in want]
        if hit:
            return hit[0]["n"]
    return None


# ---------------------------------------------------------------- one read of the file
def read_once(video: str, work: str, meta: dict, progress=_nop):
    """One ffmpeg pass. Watches its progress: no new frame for 10 min = Drive stalled -> killed, 'incomplete read'."""
    frames = os.path.join(work, "f")
    fc = (f"[0:v]scale={FW}:-2,split=3[a][b][c];"
          f"[a]scale=480:-2,select='gte(scene,0)',metadata=print:key=lavfi.scene_score:file=scores.txt[s];"
          f"[b]fps=2,scale={FW}:{FH}:force_original_aspect_ratio=increase,crop={FW}:{FH}[f];"
          f"[c]fps=1,scale=320:180:force_original_aspect_ratio=increase,crop=320:180[k]")
    tr, sb = pick_audio(meta), pick_text_sub(meta)
    # several audio tracks and none tagged English (dual-audio rips): keep them all, choose_track() listens
    if len(meta["audio"]) > 1 and not any(x["lang"] in ("eng", "en", "english") for x in meta["audio"]):
        tracks = [x["n"] for x in meta["audio"]][:4]
    else:
        tracks = [tr] if tr is not None else []

    def build(with_sub):
        cmd = ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-y", "-i", _lp(video), "-filter_complex", fc,
               "-map", "[s]", "-f", "null", "-",
               "-map", "[f]", "-q:v", "5", "f/%06d.jpg",
               "-map", "[k]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-g", "10", "-an", "strip.mp4"]
        for n_ in tracks:
            cmd += ["-map", f"0:a:{n_}", "-af", "aresample=async=1:first_pts=0", "-ac", "1", "-ar", "16000", f"a{n_}.wav"]   # gaps of a damaged file -> silence, so audio time = video time
        if with_sub and sb is not None:
            cmd += ["-map", f"0:s:{sb}", "-c:s", "srt", "emb.srt"]
        return cmd

    def run(with_sub):
        shutil.rmtree(frames, ignore_errors=True)
        os.makedirs(frames, exist_ok=True)
        for f in ["scores.txt", "strip.mp4", "a.wav", "emb.srt"] + [f"a{n_}.wav" for n_ in tracks]:
            try:
                os.remove(os.path.join(work, f))
            except OSError:
                pass
        errp = os.path.join(work, "ffmpeg.err")
        with open(errp, "w", encoding="utf-8", errors="replace") as ef:
            p = subprocess.Popen(build(with_sub), cwd=work, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=ef)
            last_n, last_t = -1, time.time()
            while True:
                try:
                    rc = p.wait(timeout=30)
                    break
                except subprocess.TimeoutExpired:
                    pass
                n = sum(1 for _ in os.scandir(frames))
                if n != last_n:
                    last_n, last_t = n, time.time()
                    progress(f"read {n / 120:.0f}/{meta['duration'] / 60:.0f} min")
                elif time.time() - last_t > 600:
                    p.kill()
                    try:
                        p.wait(timeout=60)
                    except subprocess.TimeoutExpired:
                        pass
                    raise RuntimeError(f"incomplete read: ffmpeg stalled at {n / 2:.0f}s (Drive?)")
        with open(errp, encoding="utf-8", errors="replace") as f:
            return rc, f.read()[-2000:]
    rc, err = run(True)
    if rc != 0 and sb is not None:                       # a broken subtitle track must not kill the read
        rc, err = run(False)
    if rc != 0:
        raise RuntimeError(f"incomplete read: ffmpeg exit {rc}: {err.strip()[-160:]}")
    scores = []
    sp = os.path.join(work, "scores.txt")
    if os.path.exists(sp):
        t = None
        with open(sp, encoding="utf-8", errors="replace") as f:
            for ln in f:
                m = re.search(r"pts_time:([0-9.]+)", ln)
                if m:
                    t = float(m.group(1))
                    continue
                m = re.search(r"lavfi\.scene_score=([0-9.]+)", ln)
                if m and t is not None:
                    scores.append((t, float(m.group(1))))
                    t = None
    scores.sort()
    return scores, frames, len(os.listdir(frames)), tracks


def choose_track(work, tracks, dur, progress=_nop):
    """-> the audio track to use; its samples end up in work/a.wav. With several untagged tracks the one whose speech
    is English wins (3 x 30 s language id each), else the first."""
    from . import dialogue_fill as DF
    if not tracks:
        return None, {}
    pick, votes = tracks[0], {}
    if len(tracks) > 1:
        progress("audio tracks")
        pen = {}
        for n_ in tracks:
            p = os.path.join(work, f"a{n_}.wav")
            if not os.path.exists(p):
                continue
            x, langs, ps = _audio(p), [], []
            for frac in (0.3, 0.5, 0.7):
                seg = x[int(dur * frac * 16000): int((dur * frac + 30) * 16000)]
                if len(seg) < 16000 * 10:
                    continue
                segs, info = DF.fast_model().transcribe(seg, language=None, vad_filter=True, beam_size=1)
                if sum(s.end - s.start for s in segs) >= 6:
                    langs.append(info.language)
                    ps.append(_p_en(info))
            votes[n_] = langs
            pen[n_] = sum(ps) / len(ps) if ps else 0.0
        # English by its PROBABILITY, not the top label: the small model called The Best Offer's (mis-tagged 'ita')
        # English track Welsh twice, so the Russian dub was chosen
        best = max(pen, key=pen.get) if pen else None
        rest = [v for k, v in pen.items() if k != best]
        if best is not None and pen[best] >= 0.15 and pen[best] >= 2 * max(rest, default=0.0):
            pick = best
        votes = {k: dict(langs=v, p_en=round(pen.get(k, 0.0), 2)) for k, v in votes.items()}
        _AUDIO.clear()
    for n_ in tracks:
        p = os.path.join(work, f"a{n_}.wav")
        if n_ == pick and os.path.exists(p):
            os.replace(p, os.path.join(work, "a.wav"))
        else:
            try:
                os.remove(p)
            except OSError:
                pass
    return pick, votes


# ffmpeg's fps filter keeps the LAST source frame of each 0.5 s slot, so 2-fps frame i (0-based) shows the picture at
# i/2 + 0.25 - 1/fps = i/2 + 0.21..0.23 s (measured on B99/Vikings: a cut at 105.15 s is already in "105.0").
FRAME_LAG = 0.22


def frame_at(frames: str, n: int, t: float) -> str:
    k = min(n, max(1, int(round((t - FRAME_LAG) * 2)) + 1))     # file k (1-based) shows (k-1)/2 + FRAME_LAG
    return os.path.join(frames, f"{k:06d}.jpg")


# ---------------------------------------------------------------- cuts, dips to black, dissolves
def thumbs(frames, n):
    """64x36 grey thumbnails of all 2-fps frames (uint8, index i shows t = i/2)."""
    import numpy as np
    from PIL import Image
    th = np.zeros((n, 36, 64), np.uint8)
    for i in range(n):
        try:
            im = Image.open(os.path.join(frames, f"{i + 1:06d}.jpg"))
            im.draft("L", (120, 68))
            th[i] = np.asarray(im.convert("L").resize((64, 36)))
        except Exception:
            th[i] = th[i - 1] if i else 0
    return th


def _d(th, a, b):
    import numpy as np
    return float(np.abs(th[a].astype(np.int16) - th[b].astype(np.int16)).mean())


def adaptive_cuts(scores):
    """-> [(t, score)]: hard >= 0.27; soft >= 0.04 and >= 8x the median of the surrounding +-1 s; min 0.5 s apart."""
    import numpy as np
    if not scores:
        return []
    t = np.array([x[0] for x in scores])
    s = np.array([x[1] for x in scores])
    cand = []
    for i in np.where(s >= SCENE_SOFT)[0]:
        if s[i] >= SCENE_HARD:
            cand.append((float(t[i]), float(s[i])))
            continue
        lo, hi = np.searchsorted(t, t[i] - 1.0), np.searchsorted(t, t[i] + 1.0)
        local = np.delete(s[lo:hi], i - lo) if hi - lo > 1 else s[lo:hi]
        med = float(np.median(local)) if len(local) else 0.0
        if s[i] >= SOFT_RATIO * max(med, 0.004):
            cand.append((float(t[i]), float(s[i])))
    cuts = []
    for c in cand:                               # keep the stronger of two cuts closer than MIN_SHOT
        if cuts and c[0] - cuts[-1][0] < MIN_SHOT:
            if c[1] > cuts[-1][1]:
                cuts[-1] = c
            continue
        cuts.append(c)
    return cuts


# No second "is it really a cut" filter: tested 2026-10-10 on visually judged candidates (B99, Vikings), the adaptive
# candidates were ~80 % real even in the weak 0.04-0.12 band and ~100 % in 0.12-0.27, while every cheap picture test
# (grey difference vs neighbours, colour histogram, edge maps) rejected more real cuts than false ones. A missed cut
# (a clip that changes shot mid-way) is far worse for editing than an extra split, so every shot carries `cut_in` =
# the score of the boundary it starts on and the editor may join across weak ones.


def _shift_diff(A, B, mx=6, my=3):
    """smallest mean difference of A vs B over small global shifts (a camera move, not a picture change)."""
    import numpy as np
    A, B = A.astype(np.int16), B.astype(np.int16)
    h, w = A.shape
    best = 1e9
    for dy in range(-my, my + 1):
        for dx in range(-mx, mx + 1):
            a = A[max(0, dy):h + min(0, dy), max(0, dx):w + min(0, dx)]
            b = B[max(0, -dy):h + min(0, -dy), max(0, -dx):w + min(0, -dx)]
            best = min(best, float(np.abs(a - b).mean()))
    return best


def fade_cuts(scores, th, cuts):
    """Boundaries that have no single scene-score spike -> (boundaries, spans). A dip to black (fade-out, uniform
    near-black frames, fade-in) and a cross-dissolve each become their own short 'transition' piece, so the shots on
    either side start/end on clean, fully lit picture. spans = [(a, b, 'black'|'dissolve')]."""
    import numpy as np
    n = len(th)
    if n < 8:
        return [], []
    cut_arr = np.array(cuts) if cuts else np.array([-99.0])

    def near_cut(t, w):
        return float(np.min(np.abs(cut_arr - t))) < w
    flat = th.reshape(n, -1)
    luma, std, mx = flat.mean(axis=1), flat.std(axis=1), flat.max(axis=1)
    # truly black only: Vikings' dark grading is luma 10-20 and NOT a fade; no bright spot (torch, credits text)
    black = (luma < 6) & (std < 3) & (mx < 40)
    out, spans = [], []
    i = 0
    while i < n:
        if not black[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and black[j + 1]:
            j += 1
        before = float(np.median(luma[max(0, i - 6):i])) if i > 0 else 0.0
        after = float(np.median(luma[j + 1:j + 7])) if j < n - 1 else 0.0
        if j - i + 1 < (1 if (before >= 30 or after >= 30) else 3):
            i = j + 1                             # 0.5-1 s of black inside a dark scene = an occluded light, not a fade
            continue
        # every true-black stretch becomes its own piece, also next to dark-graded scenes and at the file's ends;
        # beside a lit scene the piece also takes the fade: walk (<= 3 samples, steadily brightening) to full light
        k0 = i - 1                                # last fully lit sample before the fade-out
        while before >= 30 and k0 > max(0, i - 4) and luma[k0] < 0.7 * before and luma[k0 - 1] >= luma[k0]:
            k0 -= 1
        k1 = j + 1                                # first fully lit sample after the fade-in
        while after >= 30 and k1 < min(n - 1, j + 4) and luma[k1] < 0.7 * after and luma[k1 + 1] >= luma[k1]:
            k1 += 1
        a = k0 / 2.0 + FRAME_LAG if i > 0 else 0.0
        b = k1 / 2.0 + FRAME_LAG - 0.04 if j < n - 1 else n / 2.0
        if b > a:
            if i > 0 and not near_cut(a, 0.6):
                out.append(a)
            if j < n - 1 and not near_cut(b, 0.6):
                out.append(b)
            spans.append((round(a, 2), round(b + 0.04, 2), "black"))
        i = j + 1
    # cross-dissolves: frames 1 s apart differ a lot, the middle one is their blend, no shift explains it
    A = th[:-4].astype(np.int16)
    B = th[4:].astype(np.int16)
    dab = np.zeros(n)
    for c in range(0, len(A), 2000):
        dab[c + 2:c + 2 + len(A[c:c + 2000])] = np.abs(A[c:c + 2000] - B[c:c + 2000]).mean(axis=(1, 2))
    st = np.array([x[0] for x in scores]) if scores else np.array([0.0])
    ss = np.cumsum([0.0] + [x[1] for x in scores])
    for k in range(2, n - 2):
        if dab[k] < 22 or dab[k] < dab[k - 2:k + 3].max() or black[k - 2] or black[k + 2]:
            continue
        t = k / 2.0 + FRAME_LAG
        if near_cut(t, 1.2) or any(abs(t - f) < 1.5 for f in out[-3:]):
            continue
        a, b = th[k - 2].astype(np.float32), th[k + 2].astype(np.float32)
        D = b - a
        den = float((D * D).sum()) or 1.0

        def mixw(x):                              # how far sample x is from a towards b, and how well a blend fits it
            f = th[x].astype(np.float32)
            w = float(((f - a) * D).sum()) / den
            return w, float(np.abs(f - (a + w * D)).mean()) / dab[k]
        ws = {x: mixw(x) for x in (k - 1, k, k + 1)}
        inner = [ws[x] for x in (k - 1, k, k + 1)]
        # a real blend somewhere in the middle (any sample phase), mixing steadily from a to b (TAHM's scene dissolves
        # were missed when only sample k was tested and when a scene-score sum >= 0.2 was required: theirs is 0.11)
        if not any(0.15 < w < 0.85 and r <= 0.35 for w, r in inner):
            continue
        if not (inner[0][0] <= inner[1][0] + 0.1 and inner[1][0] <= inner[2][0] + 0.1):
            continue
        lo, hi = np.searchsorted(st, t - 1.0), np.searchsorted(st, t + 1.0)
        if ss[hi] - ss[lo] < 0.05:                # the picture really is changing over these 2 s
            continue
        if _shift_diff(th[k - 2], th[k + 2]) < 0.5 * dab[k]:
            continue
        # a dissolve joins two DIFFERENT pictures; actors / a foreground shoulder moving inside one shot keep the
        # layout (House S1 over-the-shoulder scenes: corr 0.55-0.86 = 12 false dissolves per episode; real TAHM /
        # Vikings dissolves: -0.13..0.32)
        if float(np.corrcoef(a.ravel(), b.ravel())[0, 1]) >= 0.45:
            continue
        mix = [x for x, (w, r) in ws.items() if 0.08 < w < 0.92 and r <= 0.45] or [k]   # the blended samples only
        lo_t, hi_t = min(mix) / 2.0 + FRAME_LAG - 0.35, max(mix) / 2.0 + FRAME_LAG + 0.35
        out += [lo_t, hi_t]
        spans.append((round(lo_t, 2), round(hi_t, 2), "dissolve"))
    return sorted(out), spans


def segment(orig: list, bounds: list, dur: float) -> list:
    """New shots = the intervals between real boundaries; each inherits description/characters/... from the original
    shot it overlaps most (Gemini then verifies every one)."""
    pts = sorted(set(round(b + 0.04, 2) for b in bounds if MIN_SHOT <= b <= dur - MIN_SHOT))
    edges = [0.0]
    for e in pts:
        if e - edges[-1] >= MIN_SHOT:
            edges.append(e)
    if len(edges) > 1 and dur - edges[-1] < MIN_SHOT:
        edges.pop()
    edges.append(round(dur, 2))
    starts = [s["start"] for s in orig]
    out = []
    for a, b in zip(edges, edges[1:]):
        j = max(0, bisect.bisect_right(starts, a) - 1)
        best, bo = orig[j], -1e9
        for s in orig[max(0, j - 1):]:
            if s["start"] >= b:
                break
            ov = min(b, s["end"]) - max(a, s["start"])
            if ov > bo:
                best, bo = s, ov
        n = {k: v for k, v in best.items() if k not in DROP_KEYS}
        n.update(start=round(a, 2), end=round(b, 2))
        out.append(n)
    return out


# ---------------------------------------------------------------- local per-frame signals
_face_det = None


def frame_signals(path):
    """mean luma, number of faces and the largest face height ratio (YuNet) of one 960x540 frame."""
    global _face_det
    import cv2
    im = cv2.imread(path)
    if im is None:
        return 0.0, 0, 0.0
    luma = float(cv2.cvtColor(im, cv2.COLOR_BGR2GRAY).mean())
    if _face_det is None:
        from editv2.common import FACE_MODEL
        _face_det = cv2.FaceDetectorYN.create(FACE_MODEL, "", (im.shape[1], im.shape[0]), 0.7)
    _face_det.setInputSize((im.shape[1], im.shape[0]))
    _, f = _face_det.detect(im)
    if f is None:
        return luma, 0, 0.0
    big = [x for x in f if x[3] / im.shape[0] >= 0.05]
    return luma, len(big), round(float(max((x[3] for x in big), default=0) / im.shape[0]), 3)


# ---------------------------------------------------------------- Gemini grids
def _grid(paths, cols=COLS, rows=ROWS, tw=TILE_W, th=TILE_H, font=34):
    from PIL import Image, ImageDraw, ImageFont
    G = Image.new("RGB", (cols * tw, rows * th))
    f = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", font)
    for k, p in enumerate(paths):
        try:
            im = Image.open(p).convert("RGB").resize((tw, th))
        except Exception:
            im = Image.new("RGB", (tw, th))
        d = ImageDraw.Draw(im)
        d.rectangle([0, 0, int(font * 1.5), int(font * 1.25)], fill="black")
        d.text((6, 2), str(k + 1), font=f, fill="yellow")
        G.paste(im, ((k % cols) * tw, (k // cols) * th))
    b = io.BytesIO()
    G.save(b, "JPEG", quality=82)
    return b.getvalue()


def _frames_reply(v) -> dict:
    """{n: item} from a Gemini reply ({"frames":[...]} or a bare list), tolerant of junk; {} if unusable."""
    out = {}
    if isinstance(v, dict) and not isinstance(v.get("frames"), list):
        v = next((x for x in v.values() if isinstance(x, list)), None)
    elif isinstance(v, dict):
        v = v["frames"]
    if not isinstance(v, list):
        return out
    for x in v:
        if isinstance(x, dict):
            m = re.search(r"\d+", str(x.get("n", "")))
            if m:
                out[int(m.group())] = x
    return out


def vision_call(question, jpeg, timeout=240, tries=3):
    """-> (status, parsed JSON): 'ok'; 'blocked' = the proxy answered but Gemini returned no content (choices: [] - a
    silent content refusal); 'error' = transport / HTTP / quota / unparseable reply. editv2.common.vision() returns
    None for all of these alike, which made refusals look like outages and outages look like refusals."""
    import base64, urllib.request
    from editv2 import common as E
    import urllib.error
    uri = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    last = "error"
    k = 0
    while k < tries:
        # built per try: E.VISION_MODEL moves to the next chain model when the proxy reports one retired (2026-10-13)
        body = json.dumps({"model": E.VISION_MODEL, "temperature": 0, "messages": [{"role": "user", "content": [
            {"type": "text", "text": question}, {"type": "image_url", "image_url": {"url": uri}}]}]}).encode()
        k += 1
        try:
            req = urllib.request.Request(E.PROXY, body, {"Authorization": "Bearer " + E.KEY, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                j = json.load(r)
            ch = j.get("choices") or []
            content = ((ch[0].get("message") or {}).get("content") if ch else None) or ""
            if not content.strip():
                last = "blocked"
            else:
                try:
                    return "ok", E.parse_json(content)
                except ValueError:
                    last = "error"
        except urllib.error.HTTPError as e:
            last = "error"
            try:
                txt = e.read().decode("utf-8", "replace")
            except Exception:                                    # noqa: BLE001
                txt = ""
            if E.model_gone(e.code, txt) and E.drop_vision_model():
                k -= 1                                           # retired model -> next one, the try does not count
                continue
        except Exception:
            last = "error"
        time.sleep(2 + 3 * k)
    return last, None


def vision_alive():
    """one tiny known-good request: False = the proxy / Gemini quota is down for everyone (not this file's fault)."""
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (64, 36), (90, 120, 160)).save(b, "JPEG")
    st, v = vision_call('What colour is this image? Reply ONLY JSON {"colour":"..."}', b.getvalue(), timeout=120, tries=2)
    return st == "ok"


def check_grid(items, cast, show):
    """-> (status, {n: reply}). 12 shots -> 4x3 grid of 480x270; fewer (retries) -> 2x2 or 1x1 of 960x540 tiles. Gemini
    sometimes silently blocks a whole grid (empty reply, e.g. bed scenes + a family name in one prompt) while every
    frame alone is fine - so the caller re-sends unanswered shots in smaller grids."""
    lay = (COLS, ROWS, TILE_W, TILE_H, 34) if len(items) > 4 else (2, 2, FW, FH, 48) if len(items) > 1 else (1, 1, FW, FH, 48)
    st, v = vision_call(check_prompt(items, cast, show),
                        _grid([s["_frame"] for s in items], cols=lay[0], rows=lay[1], tw=lay[2], th=lay[3], font=lay[4]))
    return st, _frames_reply(v) if st == "ok" else {}


def _nsfw(x):
    """Gemini's nsfw word -> one of NSFW, None if unrecognisable ('Nudity', 'partial nudity', 'nudity, sex' handled)."""
    x = str(x or "").strip().lower()
    if not x:
        return None
    if "sex" in x:
        return "sex"
    if "nud" in x or "naked" in x or "topless" in x:
        return "nudity"
    if "gore" in x or "blood" in x or "graphic" in x:
        return "gore"
    if "suggest" in x:
        return "suggestive"
    if x in ("none", "no", "false", "safe", "clean", "n/a"):
        return "none"
    return None


def _bool(x):
    if isinstance(x, bool):
        return x
    if isinstance(x, str) and x.strip().lower() in ("true", "yes"):
        return True
    if isinstance(x, str) and x.strip().lower() in ("false", "no"):
        return False
    return None


def check_prompt(items, cast, show):
    lines = "\n".join(f'{k + 1}. desc: "{s.get("description", "")}" | characters: {", ".join(s.get("characters") or []) or "none"}'
                      for k, s in enumerate(items))
    q = (f"{len(items)} numbered frames from {show}, each with the catalogue line that should describe it.\n{lines}\n\n"
         f"Cast members who can appear in this episode (exact spellings): {', '.join(cast) or 'unknown'}\n"
         "For EVERY frame, judging only what is visible, return:\n"
         " ok: true if the description fits the frame (minor wording is fine), else false plus a corrected one-line "
         "\"description\" (who + what + setting);\n"
         " characters: the listed cast members clearly visible (exact spellings; leave out anyone you are not sure "
         "about - never give a name to someone who only looks similar);\n"
         " text: true if burned-in text is laid over the picture (credits, title card, caption/subtitle, big on-screen "
         "text) - NOT text that is part of the set (signs, papers, screens);\n"
         " logo: true if a channel/network logo or watermark is overlaid;\n"
         f" shot_type: one of {'|'.join(SHOT_TYPES)} (black or text-only frames are insert);\n"
         f" nsfw: one of {'|'.join(NSFW)} (nudity = visible nudity, sex = sexual act, gore = graphic injury/blood).\n"
         'Reply ONLY JSON {"frames":[{"n":1,"ok":true,"characters":["..."],"text":false,"logo":false,"shot_type":"medium",'
         '"nsfw":"none"},{"n":2,"ok":false,"description":"...","characters":[],"text":false,"logo":false,'
         '"shot_type":"wide","nsfw":"none"}]}')
    return q


def recheck_people(items, cast, show):
    """Faces without (enough) names: a 2x2 grid at 960x540 so faces are 4x larger than in the main grid."""
    from editv2.common import vision
    q = (f"{len(items)} numbered frames from {show}. Cast members who can appear in this episode (exact spellings): "
         f"{', '.join(cast)}\nFor each frame list the listed cast members whose face or body is clearly identifiable. "
         'Leave out anyone you are not sure about. Reply ONLY JSON {"frames":[{"n":1,"characters":["..."]}]}')
    try:
        return _frames_reply(vision(q, _grid([s["_frame"] for s in items], cols=2, rows=2, tw=FW, th=FH, font=48), timeout=240))
    except Exception:
        return {}


def text_scan(frames, n, dur, show, progress=_nop):
    """Dense 1-fps scan of the first 10 min + last 3 min for burned-in text -> merged zones [[a,b], ...]."""
    from editv2.common import vision
    tail = TEXT_TAIL if dur < 3600 else 480   # a film's end credits run far longer than an episode's
    ts = list(range(0, int(min(TEXT_HEAD, dur)))) + list(range(int(max(TEXT_HEAD, dur - tail)), int(dur)))
    groups = [ts[i:i + 16] for i in range(0, len(ts), 16)]

    def ask_once(g, tries):
        q = (f"{len(g)} numbered frames from {show}. List the numbers of frames with BURNED-IN text laid over the picture: "
             "opening/closing credits (names - even small, faint or fading in), title cards, captions or subtitles, big "
             "on-screen text. Ignore text that is part of the set (signs, papers, screens) and small channel logos. "
             'Reply ONLY JSON {"text_frames":[...]}')
        cols = 4 if len(g) > 4 else 2 if len(g) > 1 else 1
        for _ in range(tries):
            try:
                v = vision(q, _grid([frame_at(frames, n, t) for t in g], cols=cols, rows=-(-len(g) // cols), tw=480, th=270,
                                    font=34), timeout=240)
            except Exception:
                v = None
            if isinstance(v, dict):
                v = v.get("text_frames", v.get("frames"))
            if isinstance(v, list):
                out = []
                for k in v:
                    m = re.search(r"\d+", str(k))
                    if m and 1 <= int(m.group()) <= len(g):
                        out.append(g[int(m.group()) - 1])
                return out
        return None

    def ask(g):
        """16-frame grid; a blocked/failed grid is re-asked as 4-frame grids, then single frames. A frame that cannot
        be judged even alone counts as text (unsafe), unless that happens to more than a quarter of the group."""
        r = ask_once(g, 2)
        if r is not None:
            return r
        out, lost = [], 0
        for sub in (g[i:i + 4] for i in range(0, len(g), 4)):
            r = ask_once(sub, 2)
            if r is None:
                r = []
                for t in sub:
                    one = ask_once([t], 2)
                    if one is None:
                        lost += 1
                        r.append(t)
                    else:
                        r += one
            out += r
        return out if lost <= 0.25 * len(g) else None
    with cf.ThreadPoolExecutor(6) as ex:
        res = []
        for gi, r in enumerate(ex.map(ask, groups)):
            res.append(r)
            if gi % 10 == 9:
                progress(f"text scan {gi + 1}/{len(groups)}")
    bad = sum(1 for r in res if r is None)
    if bad > 0.05 * max(1, len(groups)):
        raise RuntimeError(f"text scan incomplete ({bad}/{len(groups)} grids) - shots verified")
    zones = []
    for t in sorted(t for r in res if r for t in r):
        a, b = t - 1.0, t + 1.6
        if zones and a - zones[-1][1] < 2.5:
            zones[-1][1] = b
        else:
            zones.append([a, b])
    return [[round(max(0, a), 2), round(min(dur, b), 2)] for a, b in zones]


# ---------------------------------------------------------------- dialogue: subtitle text, audio timing
def _norm(w):
    return re.sub(r"[^a-z0-9']", "", w.lower().replace("\u2019", "'"))


_AUDIO = {}


def _audio(wav):
    """decoded 16 kHz samples, cached per file."""
    if wav not in _AUDIO:
        from faster_whisper.audio import decode_audio
        _AUDIO.clear()
        _AUDIO[wav] = decode_audio(wav, sampling_rate=16000)
    return _AUDIO[wav]


_JUNK = re.compile(r"subtitles? (by|ripped|synced|corrected|downloaded|provided)|opensubtitles|addic7ed|subscene|yts|"
                   r"www\.|\.com\b|\.org\b|\.net\b|advertise your|support us|sync(ed)? (and|&) correct|captioning|"
                   r"transcript by|encoded by", re.I)
_MOJI = ("\u00c3", "\u00c2", "\u00e2\u20ac", "\u00e2\u2122", "\ufffd")


def clean_cues(cues):
    """drop ads, song lyrics, sound-only captions and mojibake lines (none of them is dialogue)."""
    out = []
    for c in cues:
        t = (c.text or "").strip()
        if not t or _JUNK.search(t):
            continue
        if "\u266a" in t or "\u266b" in t or t[:1] in "#*" or t[-1:] in "#*" or \
                re.fullmatch(r"[\[(][^\])]*[\])]", t, re.S):     # lyrics (*...*, #...#, notes), sound captions
            continue
        if sum(t.count(x) for x in _MOJI) >= 2:
            continue
        out.append(c)
    return [subtitles.Cue(i, c.start_ms, c.end_ms, c.text) for i, c in enumerate(out)]


def _p_en(info):
    """probability of English from a faster-whisper TranscriptionInfo (all_language_probs when available)."""
    probs = dict(getattr(info, "all_language_probs", None) or [])
    if probs:
        return float(probs.get("en", 0.0))
    return float(info.language_probability) if info.language == "en" else 0.0


def audio_language(wav, dur, tagged):
    """vote over 6 x 30 s windows (fast model); a stream tagged English stays English with >= 2 English windows;
    English is also taken when its mean probability is >= 0.3 (English heard as Welsh/Nynorsk by the small model)."""
    from . import dialogue_fill as DF
    x = _audio(wav)
    votes, wins, pens = {}, [], []
    for frac in (0.1, 0.25, 0.4, 0.55, 0.7, 0.85):
        seg = x[int(dur * frac * 16000): int((dur * frac + 30) * 16000)]
        if len(seg) < 16000 * 10:
            continue
        segs, info = DF.fast_model().transcribe(seg, language=None, vad_filter=True, beam_size=1)
        if sum(s.end - s.start for s in segs) < 6:       # (runs the transcription) too little speech to judge
            continue
        votes[info.language] = votes.get(info.language, 0) + info.language_probability
        wins.append(info.language)
        pens.append(_p_en(info))
    if tagged in ("eng", "en", "english") and wins.count("en") >= 2:
        return "en", 1.0, wins
    if pens and sum(pens) / len(pens) >= 0.3:
        return "en", round(sum(pens) / len(pens), 2), wins
    if not votes:
        return ("en" if tagged in ("eng", "en", "english") else "und"), 0.0, wins
    lang = max(votes, key=votes.get)
    return lang, round(votes[lang] / max(1, wins.count(lang)), 2), wins


def word_track(wav, lang, progress=_nop):
    """word timestamps of the whole audio (fast model, BATCHED: 10 min of audio 409 s -> 25 s under load with the same
    words, median timing difference 0.01 s - tested 2026-10-11) -> [(t, normalised word)]."""
    from . import dialogue_fill as DF
    from faster_whisper import BatchedInferencePipeline
    segs, _ = BatchedInferencePipeline(model=DF.fast_model()).transcribe(_audio(wav), language=lang, word_timestamps=True,
                                                                         batch_size=16, beam_size=1)
    out, last = [], time.time()
    for s in segs:
        out += [(w.start, _norm(w.word)) for w in (s.words or []) if _norm(w.word)]
        if time.time() - last > 60:
            progress(f"words {s.end / 60:.0f} min")
            last = time.time()
    return out


def _smooth(v, w):
    import numpy as np
    return np.array([np.median(v[max(0, i - w):i + w + 1]) for i in range(len(v))])


def align_cues(cues, words):
    """-> (retimed cues | None, stats). Anchors = cues whose 3 consecutive words were heard (rare trigrams only).
    The timing model is slope*t + offset (slope covers 23.976/25 fps rips) plus a smoothed per-anchor correction,
    so a subtitle whose offset changes during the episode still lands on its words."""
    import numpy as np
    st = dict(anchors=0)
    if len(words) < 50 or not cues:
        return None, st
    toks = [w for _, w in words]
    times = [t for t, _ in words]
    idx = {}
    for j in range(len(toks) - 2):
        idx.setdefault((toks[j], toks[j + 1], toks[j + 2]), []).append(j)
    cand, n3 = [], 0
    for ci, c in enumerate(cues):
        ws = [w for w in (_norm(x) for x in c.text.split()) if w]
        if len(ws) < 3:
            continue
        n3 += 1
        for o in range(len(ws) - 2):
            pos = idx.get(tuple(ws[o:o + 3]))
            if not pos or len(pos) > 6:
                continue
            heard = []
            for p in pos:
                q = p - o                         # where the cue's first word should have been heard
                ok = q >= 0 and 0 <= times[p] - times[q] <= 0.6 * o + 0.6
                heard.append(times[q] if ok else times[p] - 0.3 * o)
            cand.append((c.start_ms / 1000, heard))
            break
    st.update(cues3=n3, matched=len(cand))
    uniq = [(s, h[0]) for s, h in cand if len(h) == 1]
    if len(uniq) < 10:
        return None, st
    us, uh = np.array([u[0] for u in uniq]), np.array([u[1] for u in uniq])
    best = None
    for a in (1.0, 25 / 23.976, 23.976 / 25, 24 / 23.976, 23.976 / 24, 25 / 24, 24 / 25):
        b = np.sort(uh - a * us)
        cnt = np.searchsorted(b, b + 1.0, side="right") - np.arange(len(b))     # offsets within the next 1 s
        k = int(np.argmax(cnt))
        if best is None or cnt[k] > best[0]:
            best = (int(cnt[k]), a, float(np.median(b[k:k + cnt[k]])))
    _, a, b0 = best
    pts = []
    for s, heard in cand:
        h = min(heard, key=lambda x: abs(x - (a * s + b0)))
        if abs(h - (a * s + b0)) <= 15:
            pts.append((s, h - (a * s + b0)))
    pts.sort()
    if len(pts) < 15:
        return None, dict(st, slope=round(a, 4), anchors=len(pts))
    at = np.array([p[0] for p in pts])
    ar = np.array([p[1] for p in pts])
    keep = np.abs(ar - _smooth(ar, 4)) <= 0.5
    at, ar = at[keep], ar[keep]
    if len(at) < 15:
        return None, dict(st, slope=round(a, 4), anchors=int(len(at)))
    sm = _smooth(ar, 2)
    resid = float(np.median(np.abs(ar - sm)))
    blocks = {}
    for c in cues:
        blocks[int(c.start_ms / 120000)] = blocks.get(int(c.start_ms / 120000), 0) + 1
    full = [k for k, v in blocks.items() if v >= 3]
    have = {int(t // 120) for t in at}
    coverage = sum(1 for k in full if k in have) / max(1, len(full))
    st.update(anchors=int(len(at)), share=round(len(at) / max(1, n3), 3), resid=round(resid, 3), coverage=round(coverage, 2),
              slope=round(a, 4), offset=round(b0, 2), corr_min=round(float(sm.min()), 2), corr_max=round(float(sm.max()), 2))
    if st["share"] < 0.15 or resid > 0.3 or coverage < 0.75:
        return None, st

    def corr(t):
        i = int(np.searchsorted(at, t))
        if i <= 0:
            return float(sm[0])
        if i >= len(at):
            return float(sm[-1])
        l, r = sm[i - 1], sm[i]
        if abs(l - r) > 0.4:                  # an offset jump (ad break / different cut): nearest anchor, no ramp
            return float(l if t - at[i - 1] <= at[i] - t else r)
        return float(l + (r - l) * (t - at[i - 1]) / max(1e-6, at[i] - at[i - 1]))
    out = []
    for i, c in enumerate(cues):
        s0 = c.start_ms / 1000
        off = a * s0 + b0 + corr(s0) - s0
        out.append(subtitles.Cue(i, int(max(0.0, s0 + off) * 1000), int(max(0.0, a * c.end_ms / 1000 + b0 + corr(s0)) * 1000), c.text))
    return out, st


def speech_mask(wav, dur, step=0.1):
    import numpy as np
    from faster_whisper.vad import get_speech_timestamps, VadOptions
    x = _audio(wav)
    m = np.zeros(int(dur / step) + 1, bool)
    for seg in get_speech_timestamps(x, VadOptions()):
        m[int(seg["start"] / 16000 / step): int(seg["end"] / 16000 / step) + 1] = True
    return m


def vad_align(cues, speech, step=0.1, block_s=300):
    """Language-free piecewise sync per 5-min block. Score = speech rate while a subtitle is on MINUS speech rate while
    none is (plain overlap saturates in loud films where 'speech' covers 70 % of the track). 1) every block: global
    search +-150 s, keep blocks with one distinct peak; 2) consensus line through them (outliers dropped); 3) every
    block re-searched within +-3 s of the consensus and accepted if that local peak is >= 80 % of its global best.
    The subtitle is accepted if >= 75 % of the blocks are."""
    import numpy as np
    L = len(speech)
    S = np.zeros(L, bool)
    for c in cues:
        a, b = int(c.start_ms / 1000 / step), int(c.end_ms / 1000 / step) + 1
        S[max(0, a):min(L, b)] = True
    if S.sum() < 100:
        return None, {}
    bl = int(block_s / step)

    def score(lo, hi, d):
        s0, s1 = max(lo, -d), min(hi, L - d)
        if s1 - s0 < 0.7 * (hi - lo):
            return None
        part, sp = S[s0:s1], speech[s0 + d:s1 + d]
        on, off = int(part.sum()), int((~part).sum())
        if on < 30 or off < 30:
            return None
        return float((part & sp).sum() / on - (~part & sp).sum() / off)
    blocks = []
    for lo in range(0, L, bl):
        hi = min(L, lo + bl)
        if S[lo:hi].sum() < 50:
            continue
        res = [(d, c) for d in range(-1500, 1501, 2) for c in [score(lo, hi, d)] if c is not None]
        if not res:
            continue
        bd, bc = max(res, key=lambda x: x[1])
        far = max((c for d, c in res if abs(d - bd) * step > 2), default=-1.0)
        blocks.append(dict(lo=lo, hi=hi, centre=(lo + hi) / 2 * step, best=bd * step, c=bc, distinct=bc >= 0.2 and bc - far >= 0.06))
    n_blocks = len(blocks)
    seed = [b for b in blocks if b["distinct"]]
    stats = dict(blocks=n_blocks, distinct=len(seed))
    if n_blocks == 0 or len(seed) < max(2, 0.3 * n_blocks):
        return None, stats
    so = np.array([b["best"] for b in seed])
    seed = [b for b, ok in zip(seed, np.abs(so - _smooth(so, 2)) <= 2.0) if ok]
    if len(seed) < 2:
        return None, stats
    sc_, so_ = np.array([b["centre"] for b in seed]), np.array([b["best"] for b in seed])
    centres, offs = [], []
    for b in blocks:
        pred = float(np.interp(b["centre"], sc_, so_))
        loc = [(d, c) for d in range(int((pred - 3) / step), int((pred + 3) / step) + 1) for c in [score(b["lo"], b["hi"], d)]
               if c is not None]
        if not loc:
            continue
        ld, lc = max(loc, key=lambda x: x[1])
        if lc >= 0.15 and lc >= 0.8 * b["c"]:
            centres.append(b["centre"])
            offs.append(ld * step)
    stats.update(blocks_ok=len(offs))
    if len(offs) < max(2, 0.75 * n_blocks):
        return None, stats
    c_arr, o_arr = np.array(centres), np.array(offs)
    stats.update(offset_min=round(float(o_arr.min()), 1), offset_max=round(float(o_arr.max()), 1))
    out = []
    for i, c in enumerate(cues):
        off = float(np.interp(c.start_ms / 1000, c_arr, o_arr))
        out.append(subtitles.Cue(i, int(max(0, c.start_ms / 1000 + off) * 1000), int(max(0, c.end_ms / 1000 + off) * 1000), c.text))
    return out, stats


def whisper_cues(wav, lang, progress=_nop):
    from . import dialogue_fill as DF
    try:
        # NOT batched: the large model batched (beam 5) took 74 min for 10 min of audio on the shared 12 GB GPU
        segs, _ = DF.model().transcribe(_audio(wav), language=None if lang in ("und", None) else lang, vad_filter=True,
                                        beam_size=5, word_timestamps=True, condition_on_previous_text=False)
        out, cur, last = [], [], time.time()

        def flush():
            if cur:
                out.append(subtitles.Cue(len(out), int(cur[0].start * 1000), int(cur[-1].end * 1000),
                                         "".join(w.word for w in cur).strip()))
                cur.clear()
        for s in segs:
            if time.time() - last > 60:
                progress(f"whisper {s.end / 60:.0f} min")
                last = time.time()
            if s.no_speech_prob >= 0.6:
                continue
            for w in s.words or []:
                if w.probability < 0.15:
                    continue
                if cur and (w.start - cur[-1].end > 0.5 or w.end - cur[0].start > 5.0):
                    flush()
                cur.append(w)
                if w.word.strip()[-1:] in ".?!":
                    flush()
        flush()
        return out
    finally:
        DF.release_model()


def _is_english(cues):
    ws = [_norm(w) for c in cues[:400] for w in c.text.split()]
    return bool(ws) and sum(1 for w in ws if w in EN_STOP) / len(ws) >= 0.08


def dialogue_sources(video, data, work):
    """All candidate subtitle texts: every sidecar rip, the embedded text track, stored non-whisper cues."""
    c = []
    for p in subtitles.sidecar_candidates(video):
        try:
            cues = clean_cues(subtitles.parse_file(p))
        except Exception:
            continue
        if cues:
            c.append((os.path.basename(p), cues))
    emb = os.path.join(work, "emb.srt")
    if os.path.exists(emb):
        cues = clean_cues(subtitles.parse_file(emb))
        if cues:
            c.append(("embedded", cues))
    src = str(data.get("dialogue_source", ""))
    if data.get("cues") and not src.startswith("whisper"):
        c.append(("stored:" + src[:60], clean_cues([subtitles.Cue(i, int(a * 1000), int(b * 1000), t)
                                                     for i, (a, b, t) in enumerate(data["cues"])])))
    return c


def sync_dialogue(video, data, work, dur, tagged="und", progress=_nop):
    """-> (cues, source, info). Subtitle TEXT with audio-derived TIMING, else whisper in the audio's language."""
    from . import dialogue_fill as DF
    wav = os.path.join(work, "a.wav")
    checked = []
    for p in subtitles.sidecar_candidates(video):
        try:
            s = os.stat(_lp(p))
            checked.append([os.path.basename(p), int(s.st_mtime), s.st_size])
        except OSError:
            pass
    if not os.path.exists(wav):
        return [], "none (no audio)", dict(checked=checked)
    with gpu_slot(progress):
        return _sync_dialogue_gpu(video, data, work, dur, tagged, progress, wav, checked)


GPU_SLOTS = 3
GPU_SLOT_DIR = r"D:\Lib\libjobs\gpu"


@contextlib.contextmanager
def gpu_slot(progress=_nop):
    """At most GPU_SLOTS processes run whisper at once (OS byte locks, released if the holder dies). 10 workers on the
    GPU together filled its 12 GB and a 30-s language check took 7+ min (2026-10-10 22:40); 3 at a time finish faster."""
    import msvcrt
    os.makedirs(GPU_SLOT_DIR, exist_ok=True)
    fh, t0, last = None, time.time(), 0.0
    reserve = os.path.join(GPU_SLOT_DIR, "reserved")
    while fh is None:
        try:                                            # the Movie Editor's voiceover reserved the GPU (studio._GpuSlots);
            if time.time() - os.path.getmtime(reserve) < 300:    # the editor touches it every 60 s; older = editor died
                if time.time() - last > 60:
                    progress(f"waiting for GPU (voiceover) {time.time() - t0:.0f}s")
                    last = time.time()
                time.sleep(5)
                continue
        except OSError:
            pass
        for i in range(GPU_SLOTS):
            f = open(os.path.join(GPU_SLOT_DIR, f"slot{i}.lock"), "a+")
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                fh = f
                break
            except OSError:
                f.close()
        if fh is None:
            if time.time() - last > 60:                 # keeps the heartbeat fresh while queuing
                progress(f"waiting for GPU {time.time() - t0:.0f}s")
                last = time.time()
            time.sleep(2)
    try:
        yield
    finally:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        fh.close()


def _sync_dialogue_gpu(video, data, work, dur, tagged, progress, wav, checked):
    from . import dialogue_fill as DF
    try:
        progress("language")
        lang, p, wins = audio_language(wav, dur, tagged)
        info = dict(audio_lang=lang, lang_prob=p, lang_windows=wins, checked=checked)
        cands = dialogue_sources(video, data, work)
        tried, best = {}, None
        if lang == "en" and cands:
            progress("words")
            words = word_track(wav, "en", progress)
            for name, cues in cands:
                res, stt = align_cues(cues, words)
                tried[name] = stt
                rank = stt.get("anchors", 0) - (20 if name.startswith("stored:") else 0)   # a real file beats a stored copy
                if res and (best is None or rank > best[3]):
                    best = (name, res, stt, rank)
        elif cands:
            speech = speech_mask(wav, dur)
            for name, cues in sorted(cands, key=lambda c: not _is_english(c[1])):
                res, stt = vad_align(cues, speech)
                tried[name] = stt
                if res:
                    best = (name, res, stt, 0)
                    break
        info["tried"] = tried
    finally:
        DF.release_fast()
    if best:
        name, cues, stt, _ = best
        info.update(sync=dict(file=name, **stt),
                    dialogue_lang="en" if lang == "en" else ("en-subs" if _is_english(cues) else "subs"))
        return cues, ("aligned:" if lang == "en" else "vad-aligned:") + name, info
    progress("whisper")
    info["dialogue_lang"] = lang
    return whisper_cues(wav, lang, progress), f"whisper-{DF.MODEL}-{lang}", info


# ---------------------------------------------------------------- cast allowed in this episode
def episode_of(path):
    """(season, episode) of a video/catalogue path, None for a film. The file name first (S01E02, 1x02, 'Season 2
    Episode 1'), else an episode marker in the name ('E01 New Beginnings') + 'Season N' / 'S6' in a parent folder
    (Vikings S6, Breaking Bad S2/S3 are named like that)."""
    base = os.path.basename(path)
    # codec / channel tokens first: subtitles.episode_key reads 'DDP5.1.x265' as season 1 episode 265 (6 films hit)
    clean = re.sub(r"(?i)(?<![a-z0-9])(?:[xh]\.?26[456]|hevc|avc)(?![a-z0-9])", " ", base)
    clean = re.sub(r"(?<![0-9])[2-7]\.[01](?![0-9])", " ", clean)
    k = subtitles.episode_key(clean)
    if k:
        return k
    m = re.search(r"(?i)(?:^|[\s._\-\[(])(?:e|ep|episode)[\s._]?(\d{1,3})(?=$|[\s._\-\])])", os.path.splitext(base)[0])
    if m:
        for d in reversed(os.path.normpath(os.path.dirname(path)).split(os.sep)[-3:]):
            s = re.search(r"(?i)\bseason[\s._]*(\d{1,2})\b|(?:^|[\s._\-])s(\d{1,2})(?=$|[\s._\-])", d)
            if s:
                return int(s.group(1) or s.group(2)), int(m.group(1))
    return None


def season_of(path):
    k = episode_of(path)
    return k[0] if k else None


def _show_root(video):
    parts = os.path.normpath(video).split(os.sep)
    for i, p in enumerate(parts):
        if p in LIB_DIRS and i + 1 < len(parts) - 1:
            return os.sep.join(parts[:i + 2])
    return os.path.dirname(video)


def regular_cast(video, season, cast):
    """-> (names seen in >= 40 % of this season's catalogued episodes, scope) - scope 'season' (>= 4 episodes),
    'show' (>= 6 episodes of the whole show), or (None, None) when there is too little to go on."""
    root = _show_root(video)
    os.makedirs(CASTFREQ_DIR, exist_ok=True)
    cp = os.path.join(CASTFREQ_DIR, hashlib.md5(f"v3|{root.lower()}|{season}".encode()).hexdigest() + ".json")
    try:
        c = json.load(open(cp, encoding="utf-8"))
        if time.time() - c["t"] < 12 * 3600:
            return c["names"], c["scope"], c["strong"]
    except Exception:
        pass
    cats = [os.path.join(dp, f) for dp, dn, fn in os.walk(_lp(root)) for f in fn if f.endswith(".catalog.json")]
    same = [p for p in cats if season_of(p) == season]
    use, scope = (same, "season") if len(same) >= 4 else (cats, "show") if len(cats) >= 6 else ([], None)
    if not scope:
        return None, None, []
    pres, n = {}, 0
    for p in use:
        try:
            S = json.load(open(p, encoding="utf-8")).get("shots", [])
        except Exception:
            continue
        n += 1
        k = {}
        for s in S:
            for ch in s.get("characters") or []:
                if isinstance(ch, str):
                    k[ch.lower()] = k.get(ch.lower(), 0) + 1
        for ch, v in k.items():
            if v >= 2:
                pres[ch] = pres.get(ch, 0) + 1
    low = {x.lower(): x for x in cast}
    names = sorted({low[ch] for ch, v in pres.items() if ch in low and v >= max(2, 0.4 * n)})
    strong = sorted({low[ch] for ch, v in pres.items() if ch in low and v >= max(3, 0.6 * n)})
    try:
        json.dump(dict(t=time.time(), names=names, strong=strong, scope=scope, episodes=n), open(cp, "w", encoding="utf-8"),
                  ensure_ascii=False)
    except OSError:
        pass
    return names, scope, strong


_TITLES = {"the", "mrs", "miss", "mister", "dr", "doctor", "jr", "sr", "captain", "detective", "officer", "sergeant",
           "lieutenant", "agent", "chief", "sheriff", "deputy", "uncle", "aunt", "grandma", "grandpa", "mom", "dad", "mother",
           "father", "young", "old", "little", "big", "baby", "king", "queen", "lord", "lady", "sir", "prince", "princess",
           "professor", "judge", "nurse", "coach", "general", "major", "colonel", "commander", "director", "special"}


def season_cast_llm(video, show, season, cast):
    """Characters of THIS season, from agy Claude's knowledge of the series (one call per show+season, cached for good).
    The catalogues cannot tell: their names came from the old whole-show list (Reacher S2 catalogues name 'Oscar
    Finlay' in 43 shots of one episode). None when the model does not know the series well or the call fails."""
    root = _show_root(video)
    os.makedirs(CASTFREQ_DIR, exist_ok=True)
    # keyed by the cast list too: when the user adds a character folder later, the season is asked again
    cp = os.path.join(CASTFREQ_DIR, "llm_" + hashlib.md5(f"{root.lower()}|{season}|{'|'.join(sorted(cast))}".encode()).hexdigest()
                      + ".json")
    try:
        return json.load(open(cp, encoding="utf-8"))["names"]
    except Exception:
        pass
    from editv2.common import llm
    try:
        v, model = llm("You are an expert on TV series and their casts. Answer only from solid knowledge; never guess.",
                       f'TV series: "{show}". Season {season}.\nCharacters (exact spellings):\n' + "\n".join(cast) +
                       f"\n\nWhich of these characters appear ON SCREEN in at least one episode of season {season} "
                       "(flashbacks count)? Leave out characters who only appear in other seasons or are only talked about. "
                       f"If you do not know this series and its season {season} well, set known to false. "
                       'Reply ONLY JSON {"known": true, "names": ["exact spelling", ...]}',
                       max_tokens=6000, tries=2, temperature=0)       # <= ~20 min worst case (heartbeat is 60)
    except Exception:
        return None                           # not cached: asked again next time
    low = {c.lower(): c for c in cast}
    names = None
    if isinstance(v, dict) and v.get("known") is True and isinstance(v.get("names"), list):
        names = sorted({low[n.lower()] for n in v["names"] if isinstance(n, str) and n.lower() in low})
        if len(names) < 2:
            names = None
    try:
        json.dump(dict(names=names, model=model, t=time.time(), show=show, season=season), open(cp, "w", encoding="utf-8"),
                  ensure_ascii=False)
    except OSError:
        pass
    return names


def episode_cast_llm(video, show, ep, cast, cues):
    """Characters ON SCREEN in THIS episode, from agy Claude given the episode's file name (title) and its whole
    dialogue - the season list was not enough (Vikings S1E2 got 'King Horik', who only arrives in E8, on 26 shots
    of Erik). One call per episode, cached; None if unknown / failed."""
    k = hashlib.md5(f"{video.lower()}|{'|'.join(sorted(cast))}".encode()).hexdigest()
    os.makedirs(CASTFREQ_DIR, exist_ok=True)
    cp = os.path.join(CASTFREQ_DIR, "ep_" + k + ".json")
    try:
        return json.load(open(cp, encoding="utf-8"))["names"]
    except Exception:
        pass
    text = " / ".join(c.text.replace("\n", " ") for c in cues)
    if len(text) > 30000:
        text = text[:30000] + " ..."
    from editv2.common import llm
    try:
        v, model = llm("You are an expert on TV series, their episodes and casts. Answer only from solid knowledge and the "
                       "evidence given; never guess.",
                       f'TV series: "{show}", season {ep[0]} episode {ep[1]}. Video file: "{os.path.basename(video)}".\n'
                       "Characters of the series (exact spellings):\n" + "\n".join(cast) +
                       "\n\nThis episode's dialogue (subtitles, in order):\n" + (text or "(none)") +
                       "\n\nWhich of these characters appear ON SCREEN in THIS episode (flashbacks count)? Use your knowledge "
                       "of the episode and the dialogue (who speaks, who is spoken to). Leave out characters who are only "
                       "talked about or who appear only in other episodes. If you do not know this episode and the "
                       'dialogue does not settle it, set known to false. Reply ONLY JSON {"known": true, "names": ["exact spelling", ...]}',
                       max_tokens=6000, tries=2, temperature=0)
    except Exception:
        return None
    low = {c.lower(): c for c in cast}
    names = None
    if isinstance(v, dict) and v.get("known") is True and isinstance(v.get("names"), list):
        names = sorted({low[n.lower()] for n in v["names"] if isinstance(n, str) and n.lower() in low})
        if len(names) < 2:
            names = None
    try:
        json.dump(dict(names=names, model=model, t=time.time()), open(cp, "w", encoding="utf-8"), ensure_ascii=False)
    except OSError:
        pass
    return names


def episode_cast(video, cast, cues, show="", progress=_nop):
    """-> (allowed names, scope). Two independent signals must agree, because each alone was wrong in tests (agy Claude
    put John/Blythe House into House S4 and King Ecbert into Vikings S1; old catalogues put Finlay into Reacher S2):
    allowed = season cast per agy Claude AND (frequent in this season's catalogues OR named >= 2 times in this
    episode's dialogue by a part only they have). Films keep their own cast list."""
    if not cast:
        return [], "none"
    ep = episode_of(video)
    if ep is None:
        return list(cast), "film"
    season = ep[0]
    text = " " + " ".join(c.text for c in cues).lower() + " "
    # a name counts as mentioned only through a part that belongs to this one cast member: 'Santiago' is said all the
    # time for Amy, so it must not let in her parents Victor/Camila Santiago; titles never count
    parts_of = {n: {p.lower() for p in re.split(r"[\s.\-\"']+", n) if len(p) >= 3 and p.lower() not in _TITLES} for n in cast}
    freq = {}
    for ps in parts_of.values():
        for p in ps:
            freq[p] = freq.get(p, 0) + 1
    named = set()
    for name, ps in parts_of.items():
        if any(freq[p] == 1 and len(re.findall(r"\b" + re.escape(p) + r"\b", text)) >= 2 for p in ps):
            named.add(name)
    progress("episode cast")
    epn = episode_cast_llm(video, show or os.path.basename(_show_root(video)), ep, cast, cues)
    if epn:
        # + guests the user gave a Cast folder who are named in this episode: the LLM left out Fire Marshal Boone
        # (B99 S01E15, 15 shots of him) and Zeke (S01E07), and Gemini then put a main-cast name on them
        return sorted(set(epn) | named), "episode (llm + dialogue)"
    progress("season cast")
    known = season_cast_llm(video, show or os.path.basename(_show_root(video)), season, cast)
    progress("season cast done")
    regular, scope, strong = regular_cast(video, season, cast)
    if regular is None:
        if known:
            return known, "season (llm only - too few episodes catalogued)"
        return list(cast), "unscoped (too few episodes catalogued)"
    allowed = set(regular) | named
    if known:
        # a character in >= 60 % of this season's episodes who is also named here survives an LLM omission
        # (agy Claude left Gaitano Russo out of Reacher S2 although he is in 7 of its 8 episodes)
        allowed = (allowed & set(known)) | (set(strong) & named)
        scope = f"{scope} + llm"
    allowed = sorted(allowed)
    if len(allowed) < 2:
        return (known, "season (llm only - too few names)") if known else (list(cast), "unscoped (too few names)")
    return allowed, scope


# ---------------------------------------------------------------- main
SHORT_READS = r"D:\Lib\libjobs\shortreads.json"


def short_read(video, got, want):
    """A read that ends early is usually Drive dropping out (temporary). If the SAME unchanged file stops at the same
    second again, the file itself is truncated -> BadSource (needs a new download), not endless retries."""
    k = hashlib.md5(video.lower().encode("utf-8")).hexdigest()
    try:
        st = os.stat(_lp(video))
        sig = [st.st_size, int(st.st_mtime)]
    except OSError:
        return
    try:
        db = json.load(open(SHORT_READS, encoding="utf-8"))
    except Exception:
        db = {}
    prev = db.get(k)
    db[k] = dict(stop=round(got, 1), want=round(want, 1), sig=sig, t=time.time())
    try:
        tmp = SHORT_READS + f".{os.getpid()}.tmp"
        json.dump(db, open(tmp, "w", encoding="utf-8"))
        os.replace(tmp, SHORT_READS)
    except OSError:
        pass
    if prev and prev.get("sig") == sig and abs(prev.get("stop", -99) - got) <= 3:
        raise BadSource(f"bad source: truncated - every read stops at {got:.0f}s of {want:.0f}s")


def refine(video: str, cast: list, show: str = "", log=print, keep_backup=True, progress=None, out=None) -> dict:
    """out: write the refined catalogue there instead of in place (tests; the library copy is not touched)."""
    progress = progress or _nop
    cat = os.path.splitext(video)[0] + ".catalog.json"
    data = json.load(open(_lp(cat), encoding="utf-8"))
    if (data.get("refined") or {}).get("version", 0) >= VERSION and not out:
        return {"skip": "already refined"}
    shots = sorted((dict(s) for s in data.get("shots") or [] if "start" in s and "end" in s), key=lambda s: s["start"])
    if not shots:
        raise RuntimeError("catalogue has no shots")
    meta = probe(video)
    real = meta["duration"]
    work = tempfile.mkdtemp(prefix="refine_")
    t0 = time.time()
    try:
        progress("read")
        scores, frames, nfr, tracks = read_once(video, work, meta, progress)
        if nfr < 10:
            raise RuntimeError(f"incomplete read: could not read frames ({nfr})")
        dur = nfr / 2.0
        want = min(meta.get("vduration") or real, real)
        if dur + 0.5 < want - max(2.5, 0.005 * want):   # the read stopped early -> do not write anything
            short_read(video, dur, want)                 # same stop point twice = a truncated file (BadSource)
            raise RuntimeError(f"incomplete read: {dur:.0f}s of {want:.0f}s")
        dur = min(dur, real)
        if not scores or scores[-1][0] < 0.97 * dur - 5:
            raise RuntimeError(f"incomplete read: scene scores stop at {scores[-1][0] if scores else 0:.0f}s of {dur:.0f}s")
        track, track_votes = choose_track(work, tracks, dur, progress)
        wav = os.path.join(work, "a.wav")
        if track is not None and (not os.path.exists(wav) or len(_audio(wav)) / 16000 < 0.97 * dur - 5):
            raise RuntimeError("incomplete read: audio track short or missing")
        ghosts = sum(1 for s in shots if s["start"] >= real - 0.3)
        shots = [s for s in shots if s["start"] < real - 0.3]
        if not shots:
            raise RuntimeError("catalogue has no shot inside the video")
        shots[-1]["end"] = min(shots[-1]["end"], real)
        t_read = time.time() - t0
        # --- real boundaries only
        progress("cuts")
        th = thumbs(frames, nfr)
        cand = adaptive_cuts(scores)
        cuts = [t for t, sc in cand]
        fades, spans = fade_cuts(scores, th, cuts)
        del th
        new = segment(shots, cuts + fades, dur)
        bt = sorted([(t + 0.04, round(sc, 3)) for t, sc in cand] + [(t + 0.04, "transition") for t in fades])
        bts = [b[0] for b in bt]
        for i, s in enumerate(new):
            j = bisect.bisect_left(bts, s["start"] - 0.06)
            near = [b for b in bt[j:j + 3] if abs(b[0] - s["start"]) <= 0.06]
            s["cut_in"] = None if i == 0 else (near[0][1] if near else None)   # strength of the boundary this shot starts on
            mid = (s["start"] + s["end"]) / 2
            s["_frame"] = frame_at(frames, nfr, mid)
            s["luma"], s["faces"], s["face_h"] = frame_signals(s["_frame"])
            kind = next((k for a, b, k in spans if a <= mid <= b), None)
            if kind:
                s["transition"] = kind            # fade / dissolve piece: not usable footage
        # --- dialogue first: it also tells which characters belong to this episode
        tagged = next((a["lang"] for a in meta["audio"] if a["n"] == track), "und") if track is not None else "und"
        cues, src, dinfo = sync_dialogue(video, data, work, dur, tagged, progress)
        if track_votes:
            dinfo["track_votes"] = {str(k): v for k, v in track_votes.items()}
        allowed, scope = episode_cast(video, cast, cues, show, progress)
        okn = {c.lower(): c for c in allowed}
        # --- verify every shot (description, cast, text, logo, shot_type, nsfw); every shot Gemini skipped or whose
        #     grid failed goes into fresh grids again (3 rounds)
        fixed = checked = 0
        todo = list(new)
        refused = set()                       # ids of shots Gemini refused as a SINGLE frame (empty reply)
        for attempt, size in enumerate((PER_GRID, PER_GRID, 4, 1, 1)):
            groups = [todo[i:i + size] for i in range(0, len(todo), size)]
            progress(f"verify {len(todo)} shots (round {attempt + 1}, {size}/grid)")
            results = []
            with cf.ThreadPoolExecutor(8) as ex:
                for gi, r in enumerate(ex.map(lambda g: check_grid(g, allowed, show), groups)):
                    results.append(r)
                    if gi % 10 == 9:
                        progress(f"verify round {attempt + 1}: {gi + 1}/{len(groups)} grids")
            if attempt == 0 and groups and sum(1 for st, _ in results if st != "ok") >= 0.9 * len(groups) and not vision_alive():
                raise RuntimeError("vision unavailable (proxy / Gemini quota down) - not this file's fault")
            left = []
            for g, (st, res) in zip(groups, results):
                if st == "blocked" and len(g) == 1:
                    refused.add(id(g[0]))
                if len(res) < 0.5 * len(g):           # blocked / mostly missing = reply not trusted (numbering may be off)
                    left += g
                    continue
                for k, s in enumerate(g):
                    r = res.get(k + 1)
                    ok, txt, nsfw = (_bool(r.get("ok")), _bool(r.get("text")), _nsfw(r.get("nsfw"))) if r else (None, None, None)
                    if ok is None or txt is None or nsfw is None or (ok is False and not str(r.get("description") or "").strip()):
                        left.append(s)                # missing / incomplete answer: ask again, never fill in 'safe' defaults
                        continue
                    checked += 1
                    s["verified"] = True
                    refused.discard(id(s))
                    if ok is False:
                        s["description"] = str(r["description"]).strip()
                        s["action"], s["tags"], s["fixed"] = "", [], True
                        fixed += 1
                    if isinstance(r.get("characters"), list):    # only names from the allowed list - none without a Cast
                        s["characters"] = [okn[c.lower()] for c in r["characters"] if isinstance(c, str) and c.lower() in okn]
                    else:                             # no usable answer about people: keep only allowed inherited names
                        s["characters"] = [c for c in (s.get("characters") or []) if isinstance(c, str) and c.lower() in okn]
                    s["text"] = txt
                    s["logo"] = bool(_bool(r.get("logo")))
                    s["shot_type"] = norm_shot_type(r.get("shot_type") or s.get("shot_type"))
                    s["nsfw"] = nsfw
                    s["explicit"] = nsfw in ("nudity", "sex")
            todo = left
            if not todo:
                break
        blocked = [s for s in todo if id(s) in refused]
        if len(todo) - len(blocked) > 0.05 * len(new):   # unanswered for other reasons -> not verified -> do not write
            raise RuntimeError(f"only {checked}/{len(new)} shots verified")
        for s in todo:                        # refused even alone, or never answered (<= 5 %): never used as safe footage
            s.update(blocked=id(s) in refused, verified=False, nsfw="blocked" if id(s) in refused else None,
                     explicit=True if id(s) in refused else None, text=None, logo=None, fixed=False,
                     shot_type=norm_shot_type(s.get("shot_type")))
        # --- faces without (enough) names: a second, larger look
        if okn:
            miss = [s for s in new if s.get("verified") and s["face_h"] >= 0.08 and s["end"] - s["start"] >= 1.0
                    and s["faces"] > len(s.get("characters") or [])]
            quads = [miss[i:i + 4] for i in range(0, len(miss), 4)]
            progress(f"people {len(quads)}")
            with cf.ThreadPoolExecutor(6) as ex:
                for q, res in zip(quads, ex.map(lambda q: recheck_people(q, allowed, show), quads)):
                    for k, s in enumerate(q):
                        r = res.get(k + 1) or {}
                        names = [okn[c.lower()] for c in (r.get("characters") or []) if isinstance(c, str) and c.lower() in okn]
                        if names:
                            s["characters"] = sorted(set(s.get("characters") or []) | set(names))
        # --- burned-in text zones (credits/titles) -> safe flag; an unverified shot is never safe
        progress("text scan")
        zones = text_scan(frames, nfr, dur, show, progress)
        for s in new:
            if not s.get("verified") and not s.get("blocked"):
                s.update(verified=False, text=None, logo=None, nsfw=None, explicit=None, fixed=False,
                         shot_type=norm_shot_type(s.get("shot_type")))
            in_zone = any(s["start"] < b and s["end"] > a for a, b in zones)
            s["safe"] = bool(s.get("verified")) and not (s.get("text") or in_zone or s.get("transition"))
            if s.get("transition") or s.get("blocked") or not s.get("verified"):
                s["quality"] = "low"              # black / dissolve / unjudged pieces are never picked as footage
            elif s.get("fixed") and s.get("quality") == "low" and s["luma"] >= 25:
                s["quality"] = "mid"              # 'low' was inherited from an old (e.g. black-frame) window
        # --- dialogue onto the final shots
        for s in new:
            s["dialogue"] = catalog.dialogue_for(cues, s["start"], s["end"]) if cues else ""
        if cues:
            data["cues"] = [[round(c.start_ms / 1000, 2), round(c.end_ms / 1000, 2), c.text] for c in cues]
        else:
            data.pop("cues", None)
        data["dialogue_source"], data["dialogue_info"] = src, dinfo
        # --- archive the 1-fps strip so no future field needs the source file again
        key = hashlib.md5(video.lower().encode("utf-8")).hexdigest()
        try:
            os.makedirs(STRIP_DIR, exist_ok=True)
            shutil.copy(os.path.join(work, "strip.mp4"), os.path.join(STRIP_DIR, key + ".mp4"))
            data["strip"] = dict(path=os.path.join(STRIP_DIR, key + ".mp4"), fps=1, w=320, h=180,
                                 lag=0.46)       # strip frame i shows i + 0.5 - 1/fps s (fps filter keeps a slot's last frame)
        except OSError:
            pass
        ep = episode_of(video)
        slug = key[:8] + (f"s{ep[0]:02d}e{ep[1]:02d}" if ep else "")
        for i, s in enumerate(new):
            s.pop("_frame", None)
            s.setdefault("fixed", False)
            s["id"] = f"{slug}__{i:05d}"
        stats = dict(version=VERSION, before=len(shots), after=len(new), ghost_shots_removed=ghosts, cuts=len(cuts),
                     soft_cuts=sum(1 for t, sc in cand if sc < SCENE_HARD), transitions=len(spans), checked=checked, blocked=len(blocked),
                     unverified=len(todo) - len(blocked), fixed=fixed, text_zones=len(zones),
                     unsafe=sum(1 for s in new if not s["safe"]), explicit=sum(1 for s in new if s.get("explicit")),
                     no_names=sum(1 for s in new if not s.get("characters")), cast_scope=scope, allowed_cast=len(allowed),
                     dialogue=round(sum(1 for s in new if s.get("dialogue")) / max(1, len(new)), 3), dialogue_source=src,
                     read_s=round(t_read), total_s=round(time.time() - t0),
                     params=dict(scene_hard=SCENE_HARD, scene_soft=SCENE_SOFT, soft_ratio=SOFT_RATIO, min_shot=MIN_SHOT,
                                 frames=f"{FW}x{FH}@2", text_scan=[TEXT_HEAD, TEXT_TAIL]))
        # sanity before anything is replaced: whole video covered, no collapse, real descriptions
        lit = [s for s in new if s["luma"] >= 25]
        # compared with the VIDEO's end: some files carry audio past the last picture (Young Sheldon S05E03: video
        # 1190 s, container 1230 s) and the old catalogue ran to the container end
        if new[-1]["end"] < 0.97 * min(shots[-1]["end"], dur) - 3 or len(new) < 0.35 * len(shots) or \
                sum(1 for s in lit if len((s.get("description") or "").split()) < 3) > 0.03 * max(1, len(lit)):
            raise RuntimeError(f"sanity check failed (shots {len(shots)}->{len(new)}, end {shots[-1]['end']:.0f}->{new[-1]['end']:.0f})"
                               " - original kept")
        if keep_backup and not out and not os.path.exists(_lp(cat + ".v1.bak")):
            shutil.copy(_lp(cat), _lp(cat + ".v1.bak"))
        cat = out or cat
        data.update(shots=new, windows=len(new), complete=True, refined=stats, duration=round(real, 3),
                    audio_track=track, cuts=[round(c, 3) for c in cuts], transitions=[list(x) for x in spans],
                    text_zones=zones, allowed_cast=allowed,
                    cut_scores=[[round(t, 2), round(s, 3)] for t, s in scores if s >= 0.03])
        tmp = cat + ".tmp"
        with open(_lp(tmp), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(_lp(tmp), _lp(cat))
        return stats
    finally:
        _AUDIO.clear()
        shutil.rmtree(work, ignore_errors=True)


def _cast_of(video):
    """Strict: only this show's own Cast (same rule as libjobs.common.cast_of)."""
    sys.path.insert(0, r"D:\Lib\libjobs")
    import common
    return common.cast_of(video)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    vids = json.load(open(args[0], encoding="utf-8")) if args and args[0].lower().endswith(".json") else args
    for n, v in enumerate(vids, 1):
        t = time.time()
        try:
            sys.path.insert(0, r"D:\Lib\libjobs")
            import common
            r = refine(v, _cast_of(v), common.show_name(v),
                       progress=lambda st: print(f"   {time.strftime('%H:%M:%S')} {st}", flush=True))
        except Exception as e:
            r = f"FAILED {type(e).__name__}: {str(e)[:160]}"
        print(f"{time.strftime('%H:%M:%S')} [{n}/{len(vids)}] {os.path.basename(v)[:58]}  {r}  ({time.time() - t:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
