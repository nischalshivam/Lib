"""Stage 3: turn a matched shot-list into a finished video.

`plan.py` decided which catalogued moment plays under each beat. This cuts
those moments out of the source episodes and hands them to the tool's existing
timeline + render pipeline — the same one that paces a beat's cuts, alternates
clips and stills, varies durations so it doesn't tick like a metronome, and
lays the narration audio over the whole thing.

The bridge is a **manifest**: one scene per beat, each carrying the assets
(cut clips and stills) that beat may show. Producing that manifest from the
plan is the whole job here; everything after it — pacing, rendering, audio —
is machinery that already existed and is reused unchanged.

Cutting reads the real episode files, so the cut step runs where the footage
lives. The manifest-building is pure and injectable, so it is tested with
neither ffmpeg nor a video.
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict

from . import plan as plan_mod

# A clip is cut a little longer than the script's target so the timeline,
# which decides the real on-screen duration, always has footage to show and
# never has to freeze a too-short clip.
CLIP_PAD_S = 3.0
MIN_CLIP_S = 5.0
STILL_WIDTH = 1920


_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")


def _thumb(data: bytes, px: int = 256) -> bytes:
    """Shrink a reference photo to a small JPEG. This matters enormously with a
    large cast: 43 full-size cast photos (~300KB each) is a ~13MB upload on
    EVERY shot's call — it made each call take ~40s and the reply came back
    truncated (so the shot was stored blank). A 256px face is more than enough
    to recognise someone, and 43 of them is ~1MB. No PIL / a bad image just
    keeps the original."""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.thumbnail((px, px))
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=80)
        return out.getvalue()
    except Exception:
        return data


def load_refs(cast_dir: str, per: int = 4, only=None) -> dict:
    """{character_name_lower: [photo_bytes, ...]} from a cast folder.

    Layout is one subfolder per character — `cast/Victor/1.jpg`,
    `cast/Hank/1.jpg` — the same shape the tool's cast feature already uses.
    These reference photos are what let the verifier tell one character from
    another instead of guessing. Each is shrunk to a thumbnail once here (see
    `_thumb`) so a big cast does not balloon every model call.

    `only` (a set/iterable of names) restricts loading to those characters — the
    lever for a big-cast show: The Wire has 58 folders, but a season only needs
    ~20, so passing that season's names cuts the reference images sent on EVERY
    shot (and thus the build cost) to a third. Names match case-insensitively.
    """
    refs = {}
    if not cast_dir or not os.path.isdir(cast_dir):
        return refs
    only_set = {str(n).strip().lower() for n in only} if only else None
    for name in sorted(os.listdir(cast_dir)):
        d = os.path.join(cast_dir, name)
        if not os.path.isdir(d):
            continue
        if only_set is not None and name.strip().lower() not in only_set:
            continue
        photos = []
        for f in sorted(os.listdir(d)):
            if f.lower().endswith(_IMG_EXT):
                try:
                    with open(os.path.join(d, f), "rb") as fh:
                        photos.append(_thumb(fh.read()))
                except OSError:
                    pass
                if len(photos) >= per:
                    break
        if photos:
            refs[name.strip().lower()] = photos
    return refs


def _refs_for(characters: list, refs: dict) -> dict:
    """The reference photos for the people a shot requires, by loose name
    match ('Gus' finds the 'Gus Fring' folder and vice-versa)."""
    if not refs or not characters:
        return {}
    out = {}
    for c in characters:
        key = c.strip().lower()
        for name, photos in refs.items():
            if key and (key in name or name in key):
                out[c] = photos
                break
    return out


def _still_sharpness(path: str) -> float:
    """Variance of the Laplacian on a 512-px grey copy — higher = crisper.
    Only ever compared between frames of the SAME shot, so a noisy source
    and a clean one are never ranked against each other."""
    try:
        import numpy as np                                   # noqa: PLC0415
        from PIL import Image                                # noqa: PLC0415
        g = Image.open(path).convert("L")
        w, h = g.size
        a = np.asarray(g.resize((512, max(1, int(512 * h / w)))), dtype=np.float64)
        lap = a[:-2, 1:-1] + a[2:, 1:-1] + a[1:-1, :-2] + a[1:-1, 2:] - 4 * a[1:-1, 1:-1]
        return float(lap.var())
    except Exception:
        return 0.0


def _best_still(extract_frame, source_file, start, end, out) -> bool:
    """The sharpest of three frames (30/50/70% into the shot) as the still — a
    single mid-point frame is often caught mid-motion (a jogging shot came out
    as a smear). Falls back to the mid frame if the extra grabs fail."""
    dur = max(0.0, (end or start) - start)
    times = ([start + dur * f for f in (0.3, 0.5, 0.7)] if dur > 0.6
             else [(start + (end or start)) / 2.0])
    best, best_score, tmps = None, -1.0, []
    for k, t in enumerate(times):
        tmp = f"{out}.c{k}.jpg" if len(times) > 1 else out
        if _try(extract_frame, source_file, t, tmp, STILL_WIDTH) is None:
            continue
        tmps.append(tmp)
        sc = _still_sharpness(tmp) if len(times) > 1 else 1.0
        if sc > best_score:
            best, best_score = tmp, sc
    if best is None:
        return False
    if best != out:
        os.replace(best, out)
    for t in tmps:
        if t != best and t != out and os.path.exists(t):
            try:
                os.remove(t)
            except OSError:
                pass
    return os.path.isfile(out)


def _grab_clip(cut_clip, source_file, start, want_s, out):
    """Cut [start, start+want_s] of the source into `out`. Returns True/ok."""
    try:
        cut_clip(source_file, start, start + want_s, out)
        return True
    except Exception:
        return False


# How many ranked candidates to visually check before giving a shot up.
MAX_VERIFY_TRIES = 4
# A confident "no" below this is ignored — the model must be fairly sure to
# reject, so a hesitant verifier never throws away a decent shot.
REJECT_BELOW = 0.55
# --- variety controls: stop the same catalogue shot filling a whole video ---
# Many beats scope to the SAME scene/episode (a 3-beat Krakower hook, an 8-beat
# finale run), and a stateless picker hands each of them the identical #1 clip —
# so the viewer sees one frame recur every minute (mass-produced, demonetised).
# We track what has already been placed and prefer a fresh shot every time.
DIVERSIFY_POOL = 24        # how many ranked candidates to weigh for variety
MAX_CLIP_REPEATS = 3       # never show one catalogue shot more than this per video
MIN_REPEAT_GAP = 8         # ...and never again within this many scenes


def build_manifest(beats: list, library: dict, out_dir: str, scope: str = "",
                   verify: bool = True, refs: dict | None = None,
                   cut_clip=None, extract_frame=None, verify_until: float = 0.0,
                   grab_frames=None, confirm=None, log=lambda *a: None) -> dict:
    """Cut a verified shot for each request and return the manifest.

    For every shot the script wants, the ranked candidates are checked in
    order: a few frames of a candidate are shown to Gemini, which says whether
    the shot actually depicts the described moment. The FIRST candidate it
    confirms is cut; if none pass, the shot becomes a gap (NEEDS VISUAL) rather
    than a confident wrong clip. This is the friend's brief's second pass — the
    thing that keeps the video accurate rather than approximate.

    Everything external is injected (`cut_clip`, `extract_frame`, `grab_frames`
    for verification, `confirm` = the model call) so the whole loop is tested
    offline. Verification fails OPEN: an unconfigured or unreachable model
    accepts the top candidate, so the tool still builds, just unverified.
    """
    if cut_clip is None or extract_frame is None:
        from .cutter import cut_clip as _cc, extract_frame as _ef
        cut_clip = cut_clip or _cc
        extract_frame = extract_frame or _ef
    if grab_frames is None:
        grab_frames = _real_frames
    if confirm is None:
        from . import gemini
        confirm = gemini.confirm_shot

    refs = refs or {}
    verify_on = verify and _verifier_ready(confirm)
    if not verify_on:
        log("  visual verify: OFF — placing clips straight from the library "
            "(character-matched, no Gemini, no API cost)")
    elif verify_until and verify_until > 0:
        log(f"  visual verify: ON for the first {verify_until/60:.0f} min only "
            f"(the intro), then straight from the library"
            + (f" · {len(refs)} reference photo set(s)" if refs else ""))
    else:
        log("  visual verify: ON (Gemini)"
            + (f" · {len(refs)} character reference(s)" if refs else
               " · no cast reference photos (identity by description only)"))

    by_beat = defaultdict(list)
    for req in plan_mod.requests_from_beats(beats):
        by_beat[req.beat].append(req)

    scenes = []
    cut, gap, rejected = 0, 0, 0
    _cum = 0.0                                     # narration seconds reached so far
    used_counts = defaultdict(int)                 # shot.id -> times placed so far
    last_used_scene = {}                           # shot.id -> last scene it appeared in
    reused = 0                                      # placements that had to repeat a shot
    # Parallel cutting: selection stays sequential (repeat tracking needs it),
    # but the ffmpeg cuts — I/O-bound, minutes when footage is on Google Drive —
    # are queued and run together at the end. MV_CUT_WORKERS=1 = old inline way.
    cut_workers = max(1, int(os.environ.get("MV_CUT_WORKERS", "6")))
    deferred = [] if cut_workers > 1 else None
    for beat in beats:
        bn = beat.get("beat") or 0
        # intro-only verify: check clips with Gemini while we are inside the
        # first `verify_until` seconds of narration, then stop paying for it.
        beat_verify = verify_on and (verify_until <= 0 or _cum < verify_until)
        _cum += float(beat.get("narration_seconds") or 0) or 0.0
        scene_dir = os.path.join(out_dir, f"scene_{bn:03d}")
        os.makedirs(scene_dir, exist_ok=True)
        assets = []
        beat_pool = []                    # every candidate this beat saw (for padding)
        placed_this_beat = set()          # shot.ids already in this scene (no dupes)

        # Order any candidate list by FRESHNESS, not rank alone: an unused shot
        # beats a used one, fewer past uses beats more, a shot shown in the last
        # MIN_REPEAT_GAP scenes is pushed back, relevance rank breaks ties. So
        # identical requests across beats walk DOWN the list (#1,#2,#3…) instead
        # of all grabbing #1 — the thing that made one frame recur every minute.
        def _order_fresh(cands):
            def _key(rank_cand):
                rank, c = rank_cand
                cid = c.shot.id
                too_recent = (bn - last_used_scene.get(cid, -999)) < MIN_REPEAT_GAP
                return (used_counts.get(cid, 0), 1 if too_recent else 0, rank)
            ordered = [c for _, c in sorted(enumerate(cands), key=_key)]
            fresh = [c for c in ordered
                     if used_counts.get(c.shot.id, 0) < MAX_CLIP_REPEATS]
            return fresh or ordered

        for idx, req in enumerate(by_beat.get(bn, [])):
            cands = plan_mod.candidates(req, library, scope=scope,
                                        limit=DIVERSIFY_POOL)
            beat_pool.extend(cands)
            ordered = _order_fresh(cands)
            # never the same shot twice inside one scene
            ordered = [c for c in ordered
                       if c.shot.id not in placed_this_beat] or ordered
            # The script's scene window is used up (every candidate there has
            # already been placed): before REPEATING a shot, look across the
            # whole pinned episode for unused footage of the same characters /
            # description. A Malcolm build repeated 77 of 204 placements because
            # 8-10 script shots all pointed into one ~2-minute window.
            if ordered and used_counts.get(ordered[0].shot.id, 0) >= 1:
                import dataclasses as _dc                   # noqa: PLC0415
                wide = plan_mod.candidates(_dc.replace(req, scene_range=""),
                                           library, scope=scope,
                                           limit=DIVERSIFY_POOL * 3)
                unused = [c for c in wide
                          if used_counts.get(c.shot.id, 0) == 0
                          and c.shot.id not in placed_this_beat]
                if unused:
                    beat_pool.extend(unused)
                    ordered = unused + ordered

            chosen = None
            tries = ordered if not beat_verify else ordered[:MAX_VERIFY_TRIES]
            for cand in tries:
                if beat_verify:
                    # grab frames directly (NOT via _try): _try turns a function
                    # that returns an empty list into `True` (its `... or True`),
                    # and that bool then reached confirm() as `frames`, which
                    # iterates it -> "'bool' object is not iterable" and the whole
                    # video failed. A shot whose frames can't be grabbed must be
                    # an empty LIST, which confirm handles ("no frames to check").
                    try:
                        frames = grab_frames(cand.shot.file,
                                             cand.shot.start, cand.shot.end)
                    except Exception:
                        frames = []
                    if not isinstance(frames, list):
                        frames = []
                    ok, conf, why = confirm(req.visual, req.characters, frames,
                                            _refs_for(req.characters, refs))
                    if not ok and conf >= REJECT_BELOW:
                        rejected += 1
                        continue
                chosen = cand
                break
            if chosen is None:
                gap += 1
                continue
            if used_counts.get(chosen.shot.id, 0) >= 1:
                reused += 1
            used_counts[chosen.shot.id] += 1
            last_used_scene[chosen.shot.id] = bn
            placed_this_beat.add(chosen.shot.id)

            shot = chosen.shot
            if req.kind == "still":
                name = f"still_{idx:02d}.jpg"
                job = (lambda f=shot.file, a=shot.start, e=shot.end,
                       o=os.path.join(scene_dir, name):
                       _best_still(extract_frame, f, a, e, o))
                kind = "image"
            else:
                name = f"clip_{idx:02d}.mp4"
                want = max(MIN_CLIP_S, (req.duration or 0) + CLIP_PAD_S)
                job = (lambda f=shot.file, a=shot.start, w=want,
                       o=os.path.join(scene_dir, name):
                       _grab_clip(cut_clip, f, a, w, o))
                kind = "video"
            ok = True if deferred is not None else job()
            if not ok:
                gap += 1
                continue
            asset = {
                "file": name, "kind": kind, "source": shot.source,
                "source_start": round(shot.start, 2),
                "source_file": shot.file,
                "placed_by": chosen.method, "confidence": chosen.why[:60]}
            assets.append(asset)
            if deferred is not None:
                deferred.append((job, assets, asset))
            cut += 1

        # Pad an asset-starved beat with EXTRA distinct footage instead of
        # letting the timeline stretch a few clips to 10-12s each. A 30s beat
        # the script gave 3 shots for would otherwise hold each ~10s; here it
        # gets the cuts its narration deserves, drawn from the same verified
        # scope (the library has thousands of shots — the old picker just never
        # reached past the first few). Extra clips are same-scope B-roll, so
        # they are not re-verified; freshness + the repeat cap still apply.
        from . import timeline as _tl
        budget = float(beat.get("narration_seconds") or 0) or \
            (len((beat.get("narration") or "").split()) / _tl.WORDS_PER_MINUTE * 60.0)
        desired = _tl.segment_count(budget, available=DIVERSIFY_POOL)
        pad_i = 0
        for cand in _order_fresh(beat_pool):
            if len(assets) >= desired:
                break
            sid = cand.shot.id
            if sid in placed_this_beat or used_counts.get(sid, 0) >= MAX_CLIP_REPEATS:
                continue
            shot = cand.shot
            name = f"pad_{pad_i:02d}.mp4"
            want = max(MIN_CLIP_S, CLIP_PAD_S + 4.0)
            job = (lambda f=shot.file, a=shot.start, w=want,
                   o=os.path.join(scene_dir, name):
                   _grab_clip(cut_clip, f, a, w, o))
            if deferred is None and not job():
                continue
            if used_counts.get(sid, 0) >= 1:
                reused += 1
            used_counts[sid] += 1
            last_used_scene[sid] = bn
            placed_this_beat.add(sid)
            asset = {
                "file": name, "kind": "video", "source": shot.source,
                "source_start": round(shot.start, 2),
                "placed_by": "variety-fill", "confidence": cand.why[:60]}
            assets.append(asset)
            if deferred is not None:
                deferred.append((job, assets, asset))
            cut += 1
            pad_i += 1

        scenes.append({"scene": bn, "narration": beat.get("narration", ""),
                       "assets": assets})
        if bn % 5 == 0 or bn == (beats[-1].get("beat") if beats else 0):
            log(f"      scene {bn}: {len(assets)} verified asset(s)")

    if deferred:
        from concurrent.futures import ThreadPoolExecutor   # noqa: PLC0415
        log(f"  cutting {len(deferred)} clip(s) with {cut_workers} parallel workers...")

        def _do(item):
            try:
                return bool(item[0]())
            except Exception:
                return False
        with ThreadPoolExecutor(max_workers=cut_workers) as ex:
            results = list(ex.map(_do, deferred))
        # a cut can fail under parallel load (a slow Google-Drive read) — give
        # every failure one more, serial, try before it becomes a gap
        retry = [i for i, ok in enumerate(results) if not ok]
        for i in retry:
            results[i] = _do(deferred[i])
        if retry:
            log(f"  re-tried {len(retry)} failed cut(s): "
                f"{sum(1 for i in retry if results[i])} recovered")
        for (job, lst, asset), ok in zip(deferred, results):
            if not ok:                        # failed cut -> drop it, count a gap
                try:
                    lst.remove(asset)
                except ValueError:
                    pass
                cut -= 1
                gap += 1

    manifest = {"video": _title(beats), "scenes": scenes,
                "cut": cut, "gap": gap, "rejected": rejected}
    with open(os.path.join(out_dir, "manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    distinct = len(used_counts)
    log(f"  {cut} shots cut · {rejected} rejected by verify · {gap} left as gaps")
    log(f"  variety: {distinct} distinct clips across {cut} placements"
        + (f" · {reused} repeat(s) (capped at {MAX_CLIP_REPEATS}× each, "
           f"≥{MIN_REPEAT_GAP} scenes apart)" if reused else " · no repeats"))
    return manifest


def _try(fn, *args):
    try:
        return fn(*args) or True
    except Exception:
        return None


def _verifier_ready(confirm) -> bool:
    """Only turn verification on when the model is actually reachable, so a
    build never grinds through 300 failing calls."""
    try:
        from . import gemini
        if confirm is not gemini.confirm_shot:
            return True                       # an injected verifier (tests)
        ok, _why = gemini.available()
        return ok
    except Exception:
        return False


def _real_frames(path: str, start: float, end: float, n: int = 3) -> list:
    """A few JPEG frames spread across a window, for the verifier. More than a
    couple so a required face that only appears mid-shot is actually seen."""
    from . import catalog
    return catalog.real_grab(path, n=n)(start, end)


def tl_scenes(out_dir: str) -> list:
    """The rendered timeline's scenes ({scene, start, end, ...}) — where each
    beat actually lands in the finished video, so a punch-in knows its time."""
    try:
        with open(os.path.join(out_dir, "timeline.json"), encoding="utf-8") as f:
            return (json.load(f) or {}).get("scenes", []) or []
    except (OSError, ValueError):
        return []


def _title(beats: list) -> str:
    for b in beats:
        for s in (b.get("shots") or []):
            if s.get("source"):
                return str(s["source"])
    return "video"


def _show_token(s: str) -> str:
    """The show name, robust to 'Breaking Bad', 'Breaking Bad S02E01' and a
    release year in brackets — 'Malcolm in the Middle (2000) S06E01' must give
    the same token as the clue's plain 'Malcolm in the Middle', or scoping finds
    nothing for that show."""
    s = re.sub(r"\bs\d{1,2}\s*e\d{1,3}(?:\s*-?\s*e\d{1,3})?\b", "", str(s or ""),
               flags=re.I)
    s = re.sub(r"\(\s*(?:19|20)\d{2}\s*\)", "", s)
    return re.sub(r"\s+", " ", s).strip().lower()


# words that look like names but are not — keeps a narration scan from firing on
# ordinary English. Extended per false positive, never guessed at scale.
_NAME_STOP = {"the", "and", "then", "when", "who", "she", "her", "him", "his",
              "they", "them", "that", "this", "with", "from", "into", "back",
              "over", "under", "years", "later", "before", "after", "still",
              "here", "there", "what", "some", "more", "most", "even", "just",
              "like", "such", "only", "also", "does", "done", "made", "make"}


def _library_characters(library: dict) -> set:
    """Every character name the library actually tags (minus 'unknown')."""
    out = set()
    for s in library.values():
        for c in (getattr(s, "characters", None) or []):
            if isinstance(c, str) and c.strip() and c.strip().lower() != "unknown":
                out.add(c.strip())
    return out


def _chars_in_text(text: str, names: set, strict: bool = False) -> list:
    """Which of `names` are named in `text`.

    strict=True  -> only a FULL-name hit ('Tony Soprano'), used to DECIDE which
                    shows a video spans, where a single token like 'mike' or
                    'night' must not drag in the wrong franchise.
    strict=False -> also a distinctive single token (first/last name, >3 letters,
                    not a common word), used to fill blank shots AFTER the library
                    is already restricted to the right shows, where 'Mike' can only
                    mean the one Mike those shows tag."""
    tl = " " + re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()) + " "
    # (score, first position, name): full name 3, first name 2, other token
    # (a surname) 1. Ranked so the person the line is about comes first — a
    # 'Phil ... the Dunphy family' line used to return Frank/Claire/Luke Dunphy
    # in arbitrary set order and the shot got Phil's father.
    scored = []
    for n in names:
        low = n.lower()
        m = re.search(r"\b" + re.escape(low) + r"\b", tl)
        if m:
            scored.append((3, m.start(), n))
            continue
        if strict or " " not in low:              # multi-word name needs its full form
            continue
        toks = low.split()
        for i, tok in enumerate(toks):
            if len(tok) > 3 and tok not in _NAME_STOP:
                m = re.search(r"\b" + re.escape(tok) + r"\b", tl)
                if m:
                    scored.append((2 if i == 0 else 1, m.start(), n))
                    break
    if any(sc >= 2 for sc, _p, _n in scored):
        # a bare surname adds nothing when someone is named properly
        scored = [x for x in scored if x[0] >= 2]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [n for _s, _p, n in scored]


def _show_name_map(library: dict):
    """(display{showkey: 'Young Sheldon'}, name_show{name: showkey},
    name_full{name: full character name}) from the library's character tags —
    only names that belong to ONE show and are tagged >=15 times (real, not a
    mis-tag). First names are included when they too are unique to one show."""
    from collections import Counter                       # noqa: PLC0415
    from .subtitles import show_key                       # noqa: PLC0415
    display, tag_count, char_shows = {}, Counter(), defaultdict(set)
    for s in library.values():
        raw = str(getattr(s, "source", "") or "")
        k = show_key(_show_token(raw))
        if k and k not in display:
            display[k] = re.sub(r"\s*\bS\d{1,2}\s*E\d{1,3}.*$", "", raw,
                                flags=re.I).strip()
        for c in (getattr(s, "characters", None) or []):
            c = str(c).strip().lower()
            if c and c != "unknown":
                tag_count[c] += 1
                char_shows[c].add(k)
    name_show, name_full, first_shows = {}, {}, defaultdict(set)
    for c, ks in char_shows.items():
        if tag_count[c] < 15 or len(ks) != 1:
            continue
        k = next(iter(ks))
        name_show[c] = k
        name_full[c] = c
        first = c.split()[0]
        if len(first) > 2 and first not in _NAME_STOP:
            first_shows[first].add(k)
            name_full.setdefault(first, c)
    for first, ks in first_shows.items():
        if len(ks) == 1 and first not in name_show:
            name_show[first] = next(iter(ks))
    return display, name_show, name_full


def _split_beats_by_show(beats: list, library: dict, log=lambda *a: None) -> list:
    """Cross-show scripts: a comparison beat's narration switches show sentence
    by sentence ('Malcolm is told...', 'Sheldon is told...'), but a beat is ONE
    scene, so all its clips played in one block and the order inside a scene is
    not kept — the voice talked about Sheldon over Malcolm footage. Split each
    such beat at every change of subject into sub-beats (each with its own
    narration, so the aligner and prostudio time it on its own), and give each
    sub-beat only clips of ITS show: the clue's shots of that show, topped up
    with shots requested from the sub-beat's sentences + characters.
    Single-show videos and single-subject beats pass through unchanged."""
    from .subtitles import show_key                       # noqa: PLC0415
    display, name_show, name_full = _show_name_map(library)
    if len(display) < 2 or not name_show:
        return beats

    def subject(text):
        t = " " + re.sub(r"[^a-z0-9 ]+", " ", text.lower()) + " "
        cnt = defaultdict(int)
        for n, k in name_show.items():
            h = len(re.findall(r"\b" + re.escape(n) + r"\b", t))
            if h:
                cnt[k] += h
        if not cnt:
            return None, []
        k = max(cnt, key=cnt.get)
        if list(cnt.values()).count(cnt[k]) > 1:
            return None, []                               # a tie: no clear subject
        who = sorted({name_full.get(n, n) for n, kk in name_show.items()
                      if kk == k and re.search(r"\b" + re.escape(n) + r"\b", t)})
        return k, who

    out, splits = [], 0
    for b in beats:
        narr = str(b.get("narration") or "")
        shots = list(b.get("shots") or [])
        sents = [x.strip() for x in re.split(r"(?<=[.!?])\s+|\n+", narr) if x.strip()]
        shot_show = [show_key(_show_token(s.get("source"))) for s in shots]
        default = max(set(shot_show), key=shot_show.count) if shot_show else None
        # sentence -> subject show (inherit the previous one when unnamed)
        runs, cur = [], None
        for snt in sents:
            k, who = subject(snt)
            k = k or cur or default
            if runs and runs[-1][0] == k:
                runs[-1][1].append(snt)
                runs[-1][2].update(who)
            else:
                runs.append([k, [snt], set(who)])
            cur = k
        # fold tiny runs (< 8 words) into the previous run — no 2-second scenes
        merged = []
        for r in runs:
            words = sum(len(s.split()) for s in r[1])
            if merged and words < 8:
                merged[-1][1].extend(r[1])
                merged[-1][2].update(r[2])
            else:
                merged.append(r)
        if len(merged) < 2 or len({r[0] for r in merged}) < 2:
            out.append(b)
            continue
        splits += 1
        total_w = max(1, sum(len(s.split()) for r in merged for s in r[1]))
        secs = float(b.get("narration_seconds") or 0)
        pool = defaultdict(list)                          # show -> the clue's shots
        for s, k in zip(shots, shot_show):
            pool[k].append(s)
        runs_of = defaultdict(list)
        for i, r in enumerate(merged):
            runs_of[r[0]].append(i)
        for i, (k, snts, who) in enumerate(merged):
            w = sum(len(s.split()) for s in snts)
            need = max(2, round(len(shots) * w / total_w)) if shots else 2
            # this run's share of the clue's shots for its show
            mine = pool.get(k, [])
            idxs = runs_of[k]
            j = idxs.index(i)
            per = len(mine) / len(idxs) if idxs else 0
            take = mine[int(j * per):int((j + 1) * per)]
            sub_shots = list(take[:need])
            n = 0
            while len(sub_shots) < need and k in display:
                snt = snts[n % len(snts)]
                sub_shots.append({
                    "kind": "clip" if n % 2 == 0 else "still", "source": display[k],
                    "season_episode": "", "visual": snt[:240],
                    "characters": [x.title() for x in sorted(who)] or [],
                    "duration_target_sec": 0})
                n += 1
            nb = dict(b)
            nb["narration"] = " ".join(snts)
            nb["narration_seconds"] = round(secs * w / total_w, 2) if secs else 0
            nb["shots"] = sub_shots
            if i:
                nb["header"] = ""
            out.append(nb)
    if splits:
        for n, b in enumerate(out, 1):
            b["beat"] = n
        log(f"  cross-show sync: {splits} beat(s) split at each change of subject "
            f"-> {len(out)} scenes, each showing only the show its sentences are about")
    return out


def _fix_beat_show_mismatch(beats: list, library: dict,
                            log=lambda *a: None) -> int:
    """Cross-show scripts: catch a clue beat whose narration is about show X
    while every shot the clue lists for it comes from show Y, and re-source
    those shots from X by the narration's own sentences + characters.

    Seen on 'Malcolm vs. Young Sheldon': beat 5's narration was 100% about
    Sheldon but the clue listed 16 Malcolm shots (another beat's content), so
    the video showed Malcolm while the voice talked about Sheldon. The tool
    followed the clue faithfully — this is the guard against a clue writer
    that shifted shots between beats. Returns how many shots were re-sourced."""
    from collections import Counter                       # noqa: PLC0415
    from .subtitles import show_key                       # noqa: PLC0415
    display, tag_count, char_shows = {}, Counter(), defaultdict(set)
    for s in library.values():
        raw = str(getattr(s, "source", "") or "")
        k = show_key(_show_token(raw))
        if k and k not in display:
            display[k] = re.sub(r"\s*\bS\d{1,2}\s*E\d{1,3}.*$", "", raw,
                                flags=re.I).strip()
        for c in (getattr(s, "characters", None) or []):
            c = str(c).strip().lower()
            if c and c != "unknown":
                tag_count[c] += 1
                char_shows[c].add(k)
    if len(display) < 2:
        return 0                                           # single-show video
    # names that point to exactly ONE show (full name + a distinctive first
    # name), from characters tagged often enough to be real, not a mis-tag
    name_show, name_full = {}, {}
    first_shows = defaultdict(set)
    for c, ks in char_shows.items():
        if tag_count[c] < 15 or len(ks) != 1:
            continue
        k = next(iter(ks))
        name_show[c] = k
        name_full[c] = c
        first = c.split()[0]
        if len(first) > 2 and first not in _NAME_STOP:
            first_shows[first].add(k)
            name_full.setdefault(first, c)
    for first, ks in first_shows.items():
        if len(ks) == 1 and first not in name_show:
            name_show[first] = next(iter(ks))
    if not name_show:
        return 0
    fixed = 0
    for b in beats:
        shots = b.get("shots") or []
        text = " " + re.sub(r"[^a-z0-9 ]+", " ", str(b.get("narration") or "").lower()) + " "
        if not shots or not text.strip():
            continue
        mentions = Counter()
        for n, k in name_show.items():
            hits = len(re.findall(r"\b" + re.escape(n) + r"\b", text))
            if hits:
                mentions[k] += hits
        total = sum(mentions.values())
        if total < 3:
            continue
        shot_keys = Counter(show_key(_show_token(s.get("source"))) for s in shots)
        dom, dc = mentions.most_common(1)[0]
        share = dc / total
        if share < 0.6 or dom not in display:
            continue                     # genuinely mixed narration: leave it
        if shot_keys.get(dom, 0) >= 0.25 * len(shots):
            continue                     # the clue already shows the right show
        # re-source: the narration's sentences that name show `dom`'s people
        sents = [x.strip() for x in re.split(r"(?<=[.!?])\s+|\n+",
                                               str(b.get("narration") or "")) if x.strip()]
        about = []
        for snt in sents:
            st = " " + re.sub(r"[^a-z0-9 ]+", " ", snt.lower()) + " "
            who = sorted({name_full.get(n, n) for n, k in name_show.items()
                          if k == dom and re.search(r"\b" + re.escape(n) + r"\b", st)})
            if who:
                about.append((snt, who))
        if not about:
            continue
        n_new = max(1, round(len(shots) * share))
        step = len(shots) / n_new
        for j in range(n_new):
            i = min(len(shots) - 1, int(j * step))
            snt, who = about[j % len(about)]
            old = shots[i]
            shots[i] = {"kind": old.get("kind", "clip"), "source": display[dom],
                        "season_episode": "", "visual": snt[:240],
                        "characters": [w.title() for w in who],
                        "duration_target_sec": old.get("duration_target_sec")}
        fixed += n_new
        was = ", ".join(f"{display.get(k, k)} x{v}" for k, v in shot_keys.items())
        log(f"  clue fix: beat {b.get('beat')} narration is about {display[dom]} "
            f"({share:.0%} of names) but its shots were [{was}] -> {n_new} shot(s) "
            f"re-sourced from {display[dom]} by the narration's characters")
    return fixed


def _char_to_shows(full_library: dict) -> dict:
    """{character_lower: {show tokens it is tagged in}} across the WHOLE library
    — so a narration that names 'Tony Soprano' can pull in The Sopranos even if
    the clue forgot to tag that source (cross-franchise videos)."""
    m = defaultdict(set)
    for s in full_library.values():
        show = _show_token(getattr(s, "source", ""))
        for c in (getattr(s, "characters", None) or []):
            if isinstance(c, str) and c.strip() and c.strip().lower() != "unknown":
                m[c.strip().lower()].add(show)
    return m


def _shows_for_video(beats: list, full_library: dict, clean: str,
                     log=lambda *a: None) -> set:
    """The shows this video draws on: the ones its clue shots NAME, plus any
    show the narration clearly points to by naming its characters. Cross-
    franchise safe (Gus + Tony -> Breaking Bad + Better Call Saul + Sopranos)."""
    shows = set()
    for b in beats:                                    # 1) explicit clue sources
        for shot in (b.get("shots") or []):
            tok = _show_token(shot.get("source"))
            if tok:
                shows.add(tok)
    # 2) shows the NARRATION names, via their characters
    char2shows = _char_to_shows(full_library)
    text = clean or " ".join(str(b.get("narration") or "") for b in beats)
    all_names = {c for c in char2shows}
    # STRICT full-name matching: a bare 'mike'/'night' must not drag in the wrong
    # franchise; only a full name ('Tony Soprano') decides a show belongs.
    named = _chars_in_text(text, {n.title() for n in all_names}, strict=True)
    per_show_hits = defaultdict(set)
    for n in named:
        for sh in char2shows.get(n.lower(), ()):
            per_show_hits[sh].add(n.lower())
    for sh, hits in per_show_hits.items():
        if sh in shows:
            continue
        if len(hits) >= 2:                             # >=2 of its characters named
            shows.add(sh)
            log(f"  narration names {sorted(hits)[:4]} -> also using '{sh}'")
    return shows


_CREDIT_RE = re.compile(
    r"title card|title sequence|end card|closing credits|end credits|credits roll|"
    r"(?:black|blank|dark) (?:screen|background)",
    re.I)


def _drop_credit_shots(library: dict, log=lambda *a: None) -> dict:
    """Never place shots that are ONLY credits / a title card / a black screen
    ('White text displaying cast credits appears on a black screen', 'The
    closing credits roll'). Cast/crew names overlaid on a real scene are kept —
    the user is fine with those (2026-09-27); only the scene itself matters."""
    out = {k: s for k, s in library.items()
           if not _CREDIT_RE.search(f"{getattr(s, 'description', '')} "
                                    f"{' '.join(getattr(s, 'tags', None) or [])} "
                                    f"{getattr(s, 'action', '')}")}
    if len(out) < len(library):
        log(f"  credits/title-card shots excluded: {len(library) - len(out)}")
    return out


def _restrict_to_shows(library: dict, beats: list, clean: str = "",
                       log=lambda *a: None) -> dict:
    """Keep only the shows THIS video draws on — from the clue's sources AND the
    narration's named characters (cross-franchise safe). Everything else is
    dropped so no other show's footage can leak in, even with Gemini verify off.
    Left untouched if nothing names a show (a single-episode catalog passed in)."""
    shows = _shows_for_video(beats, library, clean, log=log)
    if not shows:
        return library
    from .subtitles import show_key                      # noqa: PLC0415
    want = {show_key(sh): sh for sh in shows}             # canonical -> as named
    out = {k: s for k, s in library.items()
           if show_key(_show_token(s.source)) in want}
    found = {_show_token(s.source) for s in out.values()}
    found_keys = {show_key(f) for f in found}
    absent = sorted(v for k, v in want.items() if k not in found_keys)
    if not out:
        # NEVER fall back to the whole library: that silently fills a Malcolm
        # video with Game of Thrones / Sopranos clips (and searches 800k shots,
        # which is also why such builds took hours). Fail loudly instead.
        raise RuntimeError(
            "none of this video's shows are in the library ("
            + ", ".join(sorted(shows)) + ") — index them, or add their folder "
            "to the library roots (MI_EXTRA_ROOTS). Refusing to use other shows' "
            "footage.")
    if absent:
        log(f"  WARNING: not in the library, no footage for: {', '.join(absent)}")
    log(f"  library scoped to this video's shows ({', '.join(sorted(found))}): "
        f"{len(out)} of {len(library)} shots — no other show can leak in")
    return out


_NON_LIBRARY = {"real_world", "stock", "b-roll", "broll", "archival", "press"}


def _resource_non_library(beats: list, library: dict, log=lambda *a: None) -> int:
    """Stock / real-world shots ('a calculator showing 600000 x 0.03', 'two
    agents shaking hands') have no footage in the library — a Modern Family
    clue had 318 of 369 shots like that, so the picker matched those captions
    against random frames of the show. Turn each into a request for THIS
    video's show: the beat's own title (or the script's main one), no episode,
    the narration line as the caption, characters left blank so
    _fill_shot_characters gives it the people the line is about."""
    from collections import Counter                       # noqa: PLC0415
    from .subtitles import show_key                       # noqa: PLC0415
    display = {}
    for s in library.values():
        raw = str(getattr(s, "source", "") or "")
        k = show_key(_show_token(raw))
        if k and k not in display:
            display[k] = re.sub(r"\s*\bS\d{1,2}\s*E\d{1,3}.*$", "", raw, flags=re.I).strip()
    if not display:
        return 0

    def lib_key(shot):
        k = show_key(_show_token(str(shot.get("source") or "")))
        return k if k in display else None

    overall = Counter(k for b in beats for s in (b.get("shots") or [])
                      for k in [lib_key(s)] if k)
    main = overall.most_common(1)[0][0] if overall else next(iter(display))
    changed = 0
    for b in beats:
        shots = b.get("shots") or []
        here = Counter(k for s in shots for k in [lib_key(s)] if k)
        dom = here.most_common(1)[0][0] if here else main
        line = " ".join(str(b.get("narration") or "").split())[:220]
        for s in shots:
            kind = str(s.get("type") or "").lower()
            if lib_key(s) and kind not in _NON_LIBRARY:
                continue
            s["source"] = display[dom]
            s["season_episode"] = ""
            s["visual"] = line or s.get("visual", "")
            s["characters"] = []
            s.pop("type", None)
            changed += 1
    if changed:
        log(f"  {changed} stock/real-world shot(s) (not in any library) turned into "
            f"footage requests from the video's own show, matched to their line")
    return changed


def _fill_shot_characters(beats: list, library: dict, log=lambda *a: None) -> int:
    """Give every shot that names no character the characters its BEAT's
    narration is about, so the search can place THAT person's footage instead of
    a look-alike scene. This is what makes an under-specified clue (most shots
    blank) still land on the right character without any Gemini call — the
    library already tags who is in each shot; we just tell it who the line is
    about. Returns how many shots were filled."""
    from . import catalog                                       # noqa: PLC0415
    names = _library_characters(library)
    if not names:
        return 0
    filled = 0
    for b in beats:
        beat_chars = _chars_in_text(str(b.get("narration") or ""), names)
        if not beat_chars:
            continue
        for shot in (b.get("shots") or []):
            have = catalog.list_entries(shot.get("characters")
                                        or shot.get("people"))
            if not have:
                shot["characters"] = beat_chars[:3]
                filled += 1
    if filled:
        log(f"  filled {filled} blank shot(s) with the character(s) their line "
            "names, so they place that person's footage (not a look-alike)")
    return filled


def make_video(script_beats: list, library: dict, audio: str, out_dir: str,
               total_seconds: float = 0.0, scope: str = "", pace: str = "normal",
               clean: str = "", verify: bool = True, cast_dir: str = "",
               verify_until: float = 0.0, language: str = "en",
               intro_punch: bool = False, intro_punch_seconds: float = 180.0,
               cold_open: bool = False, ken_burns: bool = False,
               render_video: bool = True, log=lambda *a: None) -> str:
    """Whole of Stage 3: cut the shots, time them to the voiceover, render.

    Returns the finished mp4 path. Reuses `timeline` (pacing), `narration`
    (aligning each beat to the second its line is actually spoken) and `render`
    (cut→concat→audio) unchanged; only the manifest in between is new.

    `clean` is the full narration text. It matters because a genspark script
    covers only the visual beats — maybe a third of what is spoken — so the
    beats have to be located inside the FULL narration first, then that
    narration aligned to the audio. Without it (or without a transcriber) the
    timing falls back to an even-read estimate, which still renders but drifts
    wherever the narrator paused.
    """
    from . import timeline, render, narration, probe
    os.makedirs(out_dir, exist_ok=True)

    if not total_seconds and os.path.isfile(audio):
        try:
            total_seconds = probe.probe(audio).duration
        except Exception:
            total_seconds = 0.0

    # Repair beat order BEFORE anything is cut: a script whose beats are out of
    # the narration's reading order would otherwise time by an even-read guess
    # and drift seconds off the voice. Deterministic, and a no-op when the
    # beats are already in order. (Runs once so manifest, alignment and
    # timeline all share the corrected order.)
    if clean:
        script_beats = narration.order_by_clean(script_beats, clean, log=log)

    # confine the search to the shows THIS video draws on (clue sources + the
    # narration's named characters), so no other show's footage can leak in —
    # the real cause of a Breaking Bad video showing Young Sheldon / GoT clips.
    library = _restrict_to_shows(library, script_beats, clean=clean, log=log)
    library = _drop_credit_shots(library, log=log)
    # then give every blank shot the character its line is about, so retrieval
    # places that person's footage instead of a random look-alike scene.
    _resource_non_library(script_beats, library, log=log)
    _fill_shot_characters(script_beats, library, log=log)
    # a cross-show clue can list one show's shots under another show's
    # narration (shifted beats) — re-source those beats from the right show
    _fix_beat_show_mismatch(script_beats, library, log=log)
    # and a beat whose narration alternates shows sentence by sentence is cut
    # into one scene per subject, each with only its own show's clips
    script_beats = _split_beats_by_show(script_beats, library, log=log)

    # clue-quality warning: a shot with no season_episode can't be pinned to its
    # exact scene — it is matched by look alone, which is where "the narration is
    # about Hank but the clip shows someone else" comes from. Tell the user how
    # much of the script is scene-pinned so a weak clue is visible up front.
    _shots = [s for b in script_beats for s in (b.get("shots") or [])]
    _pinned = sum(1 for s in _shots if str(s.get("season_episode") or "").strip())
    if _shots:
        pct = round(100 * _pinned / len(_shots))
        msg = f"  clue precision: {_pinned}/{len(_shots)} shots ({pct}%) name an episode"
        if pct < 80:
            msg += (" — the rest match by look only, so some clips may be the "
                    "right show but the wrong scene. For scene-accurate videos, "
                    "have GPT put source + season_episode on EVERY shot.")
        log(msg)

    refs = load_refs(cast_dir)
    if cast_dir and not refs:
        log(f"  (cast folder me koi character folder nahi mila: {cast_dir})")
    log("  cutting + verifying matched shots...")
    build_manifest(script_beats, library, out_dir, scope=scope, verify=verify,
                   verify_until=verify_until, refs=refs, log=log)
    # stills are held on screen for seconds — AI-upscale them (Real-ESRGAN, GPU)
    try:
        from . import upscale                                 # noqa: PLC0415
        upscale.enhance_stills(out_dir, log=log, video_seconds=total_seconds)
    except Exception as exc:
        log(f"  AI upscale skipped ({exc})")
    manifest = timeline.load_manifest(out_dir)

    # Word-sync: place each beat where its line is actually spoken. Graceful —
    # no transcriber or a failed listen just leaves `spans` None and the
    # timeline uses its even-read estimate.
    spans = None
    log("  voiceover ke saath timing align kar rahe hain...")
    try:
        heard = narration.align_audio(script_beats, audio,
                                      total_seconds=total_seconds, clean=clean,
                                      language=language, log=log)
        log(heard.summary())
        if heard.ok:
            spans = heard.spans
    except Exception as exc:
        log(f"      alignment skip ({exc}) — even-read estimate use hoga")

    tl = timeline.plan(script_beats, manifest, total_seconds=total_seconds,
                       audio=audio, pace=pace, spans=spans)
    timeline.write(tl, out_dir)
    # the beats AS BUILT (cross-show split renumbers them): verify and the
    # intro hooks map scene N -> beat N, so they must read these, not the clue
    try:
        with open(os.path.join(out_dir, "build_beats.json"), "w", encoding="utf-8") as f:
            json.dump(script_beats, f, ensure_ascii=False)
    except OSError:
        pass
    log(tl.summary())

    # The launcher path (prostudio) re-renders the final video from the scene
    # folders and ignores this video.mp4 — so rendering it here is ~10 wasted
    # minutes per video. `render_video=False` stops after the scene folders +
    # timeline.json (both of which prostudio and the intro-hooks stage DO use).
    if not render_video:
        log("  skipping makevideo's own render — prostudio will render the "
            "final video from the scene folders (saves ~10 min)")
        return os.path.join(out_dir, "timeline.json")

    log("  rendering final video..."
        + (" (Ken Burns motion on stills)" if ken_burns else ""))
    res = render.render_folder(out_dir, audio=audio, motion=ken_burns, log=log)
    log(render.describe(res))
    video_path = os.path.join(out_dir, "video.mp4")

    # Intro hooks: a cold-open (original-audio line before the narration) and/or
    # diegetic punch-ins (the real voice on strong lines in the first minutes).
    # Best-effort: a failure here never loses the rendered video.
    if (intro_punch or cold_open) and os.path.isfile(video_path):
        try:
            from . import punchins
            cold_spec = (punchins.find_cold_open(script_beats, library, log=log)
                         if cold_open else {})
            exclude = ({punchins._line_key(cold_spec["video"],
                                           cold_spec["line_start"])}
                       if cold_spec else set())
            if intro_punch:
                picks = punchins.find_intro_punches(
                    script_beats, tl_scenes(out_dir), library,
                    intro_s=intro_punch_seconds, exclude_lines=exclude, log=log)
                if picks:
                    punched = os.path.join(out_dir, "video_punch.mp4")
                    punchins.apply(video_path, picks, punched, log=log)
                    os.replace(punched, video_path)
                else:
                    log("  intro punch-ins: no hook line with exact_dialogue "
                        "resolved in the intro — nothing to splice")
            if cold_spec:
                colded = os.path.join(out_dir, "video_cold.mp4")
                punchins.prepend_cold_open(video_path, cold_spec, colded, log=log)
                os.replace(colded, video_path)
            elif cold_open:
                log("  cold-open: no opening hook line with exact_dialogue "
                    "resolved — skipped")
        except Exception as exc:
            log(f"  intro effects skip ({type(exc).__name__}: {exc}) — "
                "rendered video kept as-is")
    return video_path
