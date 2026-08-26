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


def load_refs(cast_dir: str, per: int = 4) -> dict:
    """{character_name_lower: [photo_bytes, ...]} from a cast folder.

    Layout is one subfolder per character — `cast/Victor/1.jpg`,
    `cast/Hank/1.jpg` — the same shape the tool's cast feature already uses.
    These reference photos are what let the verifier tell one character from
    another instead of guessing. Each is shrunk to a thumbnail once here (see
    `_thumb`) so a big cast does not balloon every model call.
    """
    refs = {}
    if not cast_dir or not os.path.isdir(cast_dir):
        return refs
    for name in sorted(os.listdir(cast_dir)):
        d = os.path.join(cast_dir, name)
        if not os.path.isdir(d):
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


def build_manifest(beats: list, library: dict, out_dir: str, scope: str = "",
                   verify: bool = True, refs: dict | None = None,
                   cut_clip=None, extract_frame=None,
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
    log(f"  visual verify: {'ON (Gemini)' if verify_on else 'OFF'}"
        + (f" · {len(refs)} character reference(s)" if refs else
           " · NO reference photos (identity is a guess — add a cast folder)"))

    by_beat = defaultdict(list)
    for req in plan_mod.requests_from_beats(beats):
        by_beat[req.beat].append(req)

    scenes = []
    cut, gap, rejected = 0, 0, 0
    for beat in beats:
        bn = beat.get("beat") or 0
        scene_dir = os.path.join(out_dir, f"scene_{bn:03d}")
        os.makedirs(scene_dir, exist_ok=True)
        assets = []
        for idx, req in enumerate(by_beat.get(bn, [])):
            cands = plan_mod.candidates(req, library, scope=scope)
            chosen = None
            for cand in cands[:MAX_VERIFY_TRIES]:
                if verify_on:
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
        scenes.append({"scene": bn, "narration": beat.get("narration", ""),
                       "assets": assets})
        if bn % 5 == 0 or bn == (beats[-1].get("beat") if beats else 0):
            log(f"      scene {bn}: {len(assets)} verified asset(s)")

    manifest = {"video": _title(beats), "scenes": scenes,
                "cut": cut, "gap": gap, "rejected": rejected}
    with open(os.path.join(out_dir, "manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    log(f"  {cut} shots cut · {rejected} rejected by verify · {gap} left as gaps")
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


def _title(beats: list) -> str:
    for b in beats:
        for s in (b.get("shots") or []):
            if s.get("source"):
                return str(s["source"])
    return "video"


def _restrict_to_shows(library: dict, beats: list, log=lambda *a: None) -> dict:
    """Keep only the shows the SCRIPT names (its shots' `source`).

    The launcher hands makevideo the whole `F:\\Movies` (every show merged), so
    without this a shot with a blank/no-match `source` — or any shot when verify
    is off — could be filled from Young Sheldon or GoT in a Breaking Bad video,
    because the per-shot `scoped()` falls back to the ENTIRE library when its
    episode has no hit. Restricting up front to the script's own universe (BB +
    BCS here) makes that fallback land inside the right shows, so a wrong-show
    clip is impossible even with no Gemini verify. Left untouched if the script
    names no show at all (a single-episode catalog passed directly)."""
    def _show_token(s: str) -> str:
        # the show name, robust to either form the data uses: a clue's
        # source="Breaking Bad" (show only, episode in a separate field) OR a
        # catalog's source="Breaking Bad S02E01" (show + episode together).
        return re.sub(r"\bs\d{1,2}\s*e\d{1,3}\b", "", str(s or ""),
                      flags=re.I).strip().lower()
    shows = set()
    for b in beats:
        for shot in (b.get("shots") or []):
            tok = _show_token(shot.get("source"))
            if tok:
                shows.add(tok)
    if not shows:
        return library
    out = {k: s for k, s in library.items() if _show_token(s.source) in shows}
    if out:
        log(f"  library scoped to this script's shows ({', '.join(sorted(shows))}): "
            f"{len(out)} of {len(library)} shots — no other show can leak in")
        return out
    return library


def make_video(script_beats: list, library: dict, audio: str, out_dir: str,
               total_seconds: float = 0.0, scope: str = "", pace: str = "normal",
               clean: str = "", verify: bool = True, cast_dir: str = "",
               log=lambda *a: None) -> str:
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

    # confine the search to the shows THIS script names, so no other show's
    # footage can ever be pulled in (the real cause of a Breaking Bad video
    # showing Young Sheldon / GoT clips when verify is off).
    library = _restrict_to_shows(library, script_beats, log=log)

    refs = load_refs(cast_dir)
    if cast_dir and not refs:
        log(f"  (cast folder me koi character folder nahi mila: {cast_dir})")
    log("  cutting + verifying matched shots...")
    build_manifest(script_beats, library, out_dir, scope=scope, verify=verify,
                   refs=refs, log=log)
    manifest = timeline.load_manifest(out_dir)

    # Word-sync: place each beat where its line is actually spoken. Graceful —
    # no transcriber or a failed listen just leaves `spans` None and the
    # timeline uses its even-read estimate.
    spans = None
    log("  voiceover ke saath timing align kar rahe hain...")
    try:
        heard = narration.align_audio(script_beats, audio,
                                      total_seconds=total_seconds, clean=clean,
                                      log=log)
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
    return os.path.join(out_dir, "video.mp4")
