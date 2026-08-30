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
                mid = (shot.start + shot.end) / 2.0
                ok = _try(extract_frame, shot.file, mid,
                          os.path.join(scene_dir, name), STILL_WIDTH) is not None
                kind = "image"
            else:
                name = f"clip_{idx:02d}.mp4"
                want = max(MIN_CLIP_S, (req.duration or 0) + CLIP_PAD_S)
                ok = _grab_clip(cut_clip, shot.file, shot.start, want,
                                os.path.join(scene_dir, name))
                kind = "video"
            if not ok:
                gap += 1
                continue
            assets.append({
                "file": name, "kind": kind, "source": shot.source,
                "source_start": round(shot.start, 2),
                "placed_by": chosen.method, "confidence": chosen.why[:60]})
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
            if not _grab_clip(cut_clip, shot.file, shot.start, want,
                              os.path.join(scene_dir, name)):
                continue
            if used_counts.get(sid, 0) >= 1:
                reused += 1
            used_counts[sid] += 1
            last_used_scene[sid] = bn
            placed_this_beat.add(sid)
            assets.append({
                "file": name, "kind": "video", "source": shot.source,
                "source_start": round(shot.start, 2),
                "placed_by": "variety-fill", "confidence": cand.why[:60]})
            cut += 1
            pad_i += 1

        scenes.append({"scene": bn, "narration": beat.get("narration", ""),
                       "assets": assets})
        if bn % 5 == 0 or bn == (beats[-1].get("beat") if beats else 0):
            log(f"      scene {bn}: {len(assets)} verified asset(s)")

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
    """The show name, robust to 'Breaking Bad' or 'Breaking Bad S02E01'."""
    return re.sub(r"\bs\d{1,2}\s*e\d{1,3}\b", "", str(s or ""),
                  flags=re.I).strip().lower()


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
    hits = []
    for n in names:
        low = n.lower()
        if re.search(r"\b" + re.escape(low) + r"\b", tl):
            hits.append(n)
            continue
        if strict or " " not in low:              # multi-word name needs its full form
            continue
        for tok in low.split():
            if len(tok) > 3 and tok not in _NAME_STOP \
                    and re.search(r"\b" + re.escape(tok) + r"\b", tl):
                hits.append(n)
                break
    return hits


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


def _restrict_to_shows(library: dict, beats: list, clean: str = "",
                       log=lambda *a: None) -> dict:
    """Keep only the shows THIS video draws on — from the clue's sources AND the
    narration's named characters (cross-franchise safe). Everything else is
    dropped so no other show's footage can leak in, even with Gemini verify off.
    Left untouched if nothing names a show (a single-episode catalog passed in)."""
    shows = _shows_for_video(beats, library, clean, log=log)
    if not shows:
        return library
    out = {k: s for k, s in library.items() if _show_token(s.source) in shows}
    if out:
        log(f"  library scoped to this video's shows ({', '.join(sorted(shows))}): "
            f"{len(out)} of {len(library)} shots — no other show can leak in")
        return out
    return library


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
               cold_open: bool = False, log=lambda *a: None) -> str:
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
    # then give every blank shot the character its line is about, so retrieval
    # places that person's footage instead of a random look-alike scene.
    _fill_shot_characters(script_beats, library, log=log)

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
    log(tl.summary())

    log("  rendering final video...")
    res = render.render_folder(out_dir, audio=audio, log=log)
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
