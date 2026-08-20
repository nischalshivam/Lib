"""Run a queue of videos unattended.

Order of operations is the whole point:

    1. pre-flight EVERY job          (minutes, no rendering)
    2. render only the jobs that passed
    3. report

Queue 25 videos, go to sleep, and in the morning the ones that could be built
are built and the ones that could not are named, with the reason. A job that
fails never touches the ones behind it.

Output is written in the layout the existing editor tools already read:

    out/
      scene_001/
        clip_01.mp4      real footage, cut on shot boundaries
        image_01.jpg     a still from the same scene
        scene.txt        the narration for this beat
      scene_002/
      manifest.json      every asset with its score and provenance
      report.txt

Resuming is free: a scene whose folder already holds its assets is skipped, so
re-running after an interruption picks up where it stopped.
"""
from __future__ import annotations

import json
import os
import time
import traceback
from dataclasses import dataclass, field

from . import (align, cast, clues as clues_mod, cutter, frames,
               jobs as jobs_mod, placeholder, probe, refine as refine_mod,
               term, tiers, timings, verify)
from .probe import ProbeError

MANIFEST = "manifest.json"


@dataclass
class SceneResult:
    index: int
    narration: str = ""
    clips: list = field(default_factory=list)
    stills: list = field(default_factory=list)
    status: str = "empty"        # cut | reused | fallback | empty
    note: str = ""
    source: str = ""
    confidence: str = ""
    # How each asset in this scene got its position. An interpolated shot is
    # a good guess — the scene's own chronology between two anchors — but it
    # is still a guess, and a manifest that does not distinguish the two
    # gives an editor no way to know which shots are worth checking.
    methods: dict = field(default_factory=dict)     # {"clip_01.mp4": "anchor"}
    # Where in the episode each asset was taken from. An editor asked to
    # lengthen a clip or replace a still needs to know where to go back to,
    # and a folder of clip_01.mp4 files says nothing about that.
    origins: dict = field(default_factory=dict)     # {"clip_01.mp4": 2013.4}
    # And WHICH episode it came from, per asset.
    #
    # A beat routinely draws from two episodes — the scene, and a flashback
    # it refers to — and the manifest used to label every asset in a scene
    # with whichever episode the FIRST one happened to come from. Six shots
    # of a real build were reported as Season 4 Episode 1 while sitting in
    # Season 3 Episode 13, which is exactly the kind of wrong label that
    # sends an investigation into the wrong file.
    sources: dict = field(default_factory=dict)     # {"clip_01.mp4": "S03E13.mp4"}
    # NEEDS VISUAL cards: the beats this mode refused to fill, holding their
    # own duration. They are assets on the timeline like any other, and they
    # are the reason the rest of the timeline can be trusted.
    cards: list = field(default_factory=list)
    # {"image_x.jpg": "A"} — what each asset was worth when it was chosen.
    tiers: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.clips or self.stills or self.cards)

    @property
    def anchored(self) -> int:
        return sum(1 for m in self.methods.values() if m == "anchor")

    @property
    def verified(self) -> int:
        """Assets whose picture was checked against the shot's description."""
        return sum(1 for m in self.methods.values() if m == "verified")

    @property
    def interpolated(self) -> int:
        return sum(1 for m in self.methods.values() if m == "interpolated")

    @property
    def paced(self) -> int:
        """Assets laid in script order across the scene the run was found in.

        Not checked against anything, like filler — but unlike filler it is
        in the right place relative to its neighbours, so a scene made of
        these plays through instead of jumping about."""
        return sum(1 for m in self.methods.values() if m == "paced")

    @property
    def filler(self) -> int:
        """Assets from the right episode but no particular moment of it.

        The one kind of asset the tool cannot justify, counted separately so
        it can never hide inside "interpolated" — an editor scanning the
        manifest should be able to find every one of them in a second."""
        return sum(1 for m in self.methods.values() if m == "filler")


@dataclass
class JobResult:
    job: jobs_mod.Job
    status: str = "pending"      # done | partial | skipped | failed
    scenes: list = field(default_factory=list)
    seconds: float = 0.0
    error: str = ""

    @property
    def clips(self) -> int:
        return sum(len(s.clips) for s in self.scenes)

    @property
    def stills(self) -> int:
        return sum(len(s.stills) for s in self.scenes)

    @property
    def gaps(self) -> int:
        return sum(1 for s in self.scenes if not s.ok)

    @property
    def icon(self) -> str:
        return {"done": term.sym("ok"), "partial": term.sym("warn"),
                "skipped": term.sym("skip"), "failed": term.sym("fail"),
                "pending": term.sym("pending")}[self.status]


def _scene_dir(job, index: int) -> str:
    return os.path.join(job.out, f"scene_{index:03d}")


def _already_built(scene_dir: str) -> tuple:
    """(clips, stills) already present, so a resumed run does not redo them."""
    if not os.path.isdir(scene_dir):
        return [], []
    names = sorted(os.listdir(scene_dir))
    clips = [os.path.join(scene_dir, n) for n in names
             if n.startswith("clip_") and n.lower().endswith(".mp4")]
    stills = [os.path.join(scene_dir, n) for n in names
              if n.startswith("image_") and n.lower().endswith((".jpg", ".png"))]
    return clips, stills


def _narration_for(beat: dict) -> str:
    return (beat.get("narration") or beat.get("Script Cue")
            or beat.get("script_cue") or "").strip()


# How far either side of a placement to look for still frames.
#
# Four seconds was too generous. Shots in an aligned run sit about three and a
# half seconds apart, so a window of four either side made every scan overlap
# both its neighbours almost completely — and neighbouring scenes then chose
# from the same pool of frames. The contact sheet showed the consequence: the
# same red-lit frame returning again and again down the page.
STILL_WINDOW_S = 1.5

# Every clip is cut this long whatever the timeline later uses, so that no
# planned duration can ever exceed the footage on disk. It must not be less
# than timeline.MAX_CLIP_S; a test asserts that they agree.
CLIP_HEADROOM_S = 6.0

# Fail-closed: a MOVING clip says "watch this happen — this is the moment".
# Only a placement that was actually located or verified may make that claim.
# A guess (interpolated between anchors, paced across a stretch, or filler)
# is shown as a STILL instead — a frozen frame is an honest "roughly this
# scene", where wrong motion is a confident lie. This is GPT's point: until a
# verified-still fallback exists, unverified motion must never ship. Stills
# still play (with a slow hold), so nothing goes black; the video just stops
# pretending a guessed moment is real.
MOTION_OK = frozenset({"anchor", "stated", "chosen", "verified", "vlm",
                       "picture"})

# Two assets taken from within this much of the same moment of the same
# episode are the same picture, whatever the placement says.
#
# The last net, not the fix — placement is where the spreading is decided.
# But when placement went wrong it went wrong invisibly: 31 of the first 66
# pictures of a finished video came out of one six-second stretch, and
# nothing in the pipeline objected because each frame was, technically, a
# different frame. The perceptual de-duplicator missed them precisely
# because a hand moving through a shot makes every frame slightly different.
# Time cannot be argued with in the same way.
REPEAT_APART_S = 2.0
# How far a repeated shot may be moved to find footage nobody has used.
SHIFT_REACH_S = 45.0
# Filler is spread across the middle of an episode — never the titles, never
# the credits — and kept well apart so a beat with nothing does not become a
# beat with the same corridor four times.
FILLER_SPREAD = (0.10, 0.90)
FILLER_APART_S = 20.0


def _wants_still(shot: dict) -> bool:
    return str(shot.get("kind") or "").strip().lower() == "still"


def _still_count(shot: dict, default: int) -> int:
    try:
        return max(1, int(shot.get("count") or default))
    except (TypeError, ValueError):
        return default


_LENGTHS: dict = {}


def episode_length(path: str) -> float:
    """How long a video is, asked once per file. 0.0 if it cannot be read."""
    if path not in _LENGTHS:
        try:
            _LENGTHS[path] = float(probe.probe(path).duration or 0.0)
        except (ProbeError, OSError):
            _LENGTHS[path] = 0.0
    return _LENGTHS[path]


def _repeated(used: dict | None, path: str, at: float,
              apart: float = REPEAT_APART_S) -> bool:
    """Has this moment of this episode already been used in the video?"""
    if used is None:
        return False
    return any(abs(at - t) < apart for t in used.get(path, ()))


def _free_moment(used: dict | None, path: str, at: float,
                 reach: float = SHIFT_REACH_S) -> float | None:
    """The nearest second of this episode nobody has used yet.

    Refusing a repeated shot outright was the first version and it emptied
    seven scenes of a real build — the repetition became holes, and the
    holes became stills sitting on screen for half a minute. These
    placements are interpolated guesses to begin with; moving one a few
    seconds costs nothing anybody can measure and keeps the scene.
    """
    if not _repeated(used, path, at):
        return at
    step = REPEAT_APART_S
    d = step
    while d <= reach:
        for cand in (at + d, at - d):
            if cand >= 0 and not _repeated(used, path, cand):
                return cand
        d += step
    return None


def _filler_moment(used: dict | None, path: str, duration: float,
                   k: int, window: tuple | None = None) -> float | None:
    """Somewhere in this episode nobody has been yet, for a shot with no
    placement at all.

    Three runs of a real script carried no quoted line and matched no
    picture, so 198 seconds of an eleven-minute video had nothing to show
    and the shots around those holes were stretched to cover them. The
    script still names the episode, and footage from the right episode is
    what an editor reaches for when the exact frame cannot be found. It is
    marked as filler everywhere it appears — this is the one place the tool
    shows something it cannot justify, and it says so.

    The golden ratio spreads successive calls across the episode instead of
    clustering them, without needing any state beyond a counter.
    """
    if duration <= 0:
        return None
    lo, hi = FILLER_SPREAD[0] * duration, FILLER_SPREAD[1] * duration
    if window and window[1] > window[0]:
        # The stretch of the episode this run actually occupies. Filler stays
        # inside it. Scattered across the whole episode instead, a video
        # about one four-minute scene pulled eighty-one shots from the whole
        # forty-seven minutes of it — which is why the footage looked
        # unrelated: it was.
        #
        # Clamped to the same bounds as the spread, never past them. A window
        # that reaches the titles is a window that puts "Previously on" under
        # a sentence about a killing.
        lo = max(lo, min(window[0], hi - 8.0))
        hi = min(hi, max(window[1], lo + 8.0))
        if hi - lo < 8.0:
            lo, hi = (FILLER_SPREAD[0] * duration,
                      FILLER_SPREAD[1] * duration)
    # Near where the video already is in this episode, not anywhere in it.
    #
    # Scattered across the whole film, filler found the title cards — a real
    # build put "Produced by" and "Written by" on screen — and characters the
    # narration has never mentioned. The essay is somewhere specific at that
    # moment, every other shot from this episode says where, and footage from
    # the same part of the story is the only kind that can pass unnoticed.
    seen = sorted(used.get(path, ())) if used else []
    if seen:
        near = seen[len(seen) // 2]
        for step in range(1, 60):
            for at in (near + step * FILLER_APART_S, near - step * FILLER_APART_S):
                if lo <= at <= hi and not _repeated(used, path, at,
                                                    apart=FILLER_APART_S):
                    return at
    # Twenty seconds apart was chosen for spreading filler across a whole
    # episode, where two shots that close really are the same moment. Inside
    # a four-minute scene it is the wrong number entirely: eighty-five shots
    # cannot fit, the walk finds nothing, and thirty beats of a real build
    # came out EMPTY — which the renderer then covered by holding their
    # neighbours for 323 seconds.
    #
    # So the distance gives way, not the window. Within one scene, shots
    # three seconds apart are three different shots; an empty beat is never
    # better than a close one.
    for apart in (FILLER_APART_S, 10.0, 5.0, REPEAT_APART_S):
        for i in range(96):
            frac = ((k + i) * 0.618033988749895) % 1.0
            at = lo + frac * (hi - lo)
            if not _repeated(used, path, at, apart=apart):
                return at
    return None


def _mark_used(used: dict | None, path: str, at: float) -> None:
    if used is not None:
        used.setdefault(path, []).append(at)


def _filler_for(episode: str, used: dict | None, log,
                window: tuple | None = None) -> tuple:
    """(seconds, path) somewhere in the episode a beat names, or (None, '')."""
    if not episode or not os.path.isfile(episode):
        return None, ""
    try:
        length = probe.probe(episode).duration
    except (ProbeError, OSError):
        return None, ""
    taken = len(used.get(episode, ())) if used else 0
    return (_filler_moment(used, episode, float(length or 0.0), taken, window),
            episode)


# Why a shot was held back, in the words the card shows.
HELD_BACK_WHY = {
    "none": "na koi quoted line, na picture match",
    "filler": "sirf sahi episode — moment ka koi saboot nahi",
    "paced": "script ke order se anumaan — koi saboot nahi",
    "interpolated": "do placed shots ke beech ka anumaan",
    "picture": "sirf picture se mila — dialogue se confirm nahi",
    "verified": "picture ne confirm kiya, par dialogue se nahi",
}


def _needs_visual(job, scene_dir: str, index: int, res, p, tier: str,
                  shot: dict, episode: str, log) -> bool:
    """Draw the card for one shot this mode would not show.

    A card is an asset. It sits on the timeline, holds the narration's own
    duration, and carries every fact somebody needs to fix it by hand. That
    is the whole trade this mode makes: less footage, and complete trust in
    the footage there is.
    """
    name = f"card_{p.shot:02d}.png"
    made = ""
    try:
        made = placeholder.card(os.path.join(scene_dir, name), {
            "scene": index,
            "seconds": max(1.0, (p.end_ms - p.start_ms) / 1000.0),
            "narration": res.narration,
            "episode": (os.path.basename(episode) if episode
                        else str(shot.get("season_episode") or "")),
            "why": HELD_BACK_WHY.get(p.method if p.ok else "none",
                                     "koi saboot nahi"),
            "must_show": shot.get("characters") or [],
        })
    except (ProbeError, OSError) as exc:
        log(f"      scene {index}: card nahi ban paya — {exc}")
    if not made:
        return False
    res.cards.append(made)
    res.methods[name] = "needs_visual"
    res.tiers[name] = tier
    res.origins[name] = 0.0
    res.sources[name] = os.path.basename(episode) if episode else ""
    return True


def build_scene(job, index: int, beat: dict, placements: list,
                seen: list | None = None, log=lambda *a: None,
                used: dict | None = None, episode: str = "",
                windows: dict | None = None, mode: str = tiers.BALANCED,
                stated: dict | None = None) -> SceneResult:
    """Cut every shot of one beat. Never raises — a bad scene is reported.

    Driven by alignment rather than by dialogue matches alone. On a real
    scene breakdown only 7% of shots quote a line — the famous scenes are
    the quiet ones — so cutting only what matched dialogue threw away 92% of
    the script and the queue produced almost nothing. Alignment places the
    silent shots along the scene between the few that did match.
    """
    res = SceneResult(index=index, narration=_narration_for(beat))
    scene_dir = _scene_dir(job, index)
    os.makedirs(scene_dir, exist_ok=True)

    clips, stills = _already_built(scene_dir)
    if clips or stills:
        res.clips, res.stills = clips, stills
        res.status = "reused"
        res.note = "already built — resumed"
        return res

    beat_no = beat.get("beat", index)
    shots = beat.get("shots") or []
    mine = [p for p in placements if p.beat == beat_no]
    unplaced = 0
    repeats = 0
    # What this mode refuses to show. Every one of these keeps its exact
    # duration on the timeline as a NEEDS VISUAL card, because the one thing
    # worse than an unfilled beat is a filled one nobody can trust.
    held_back: list = []
    # Shots refused because the footage they wanted is already on screen
    # somewhere else. Kept, because refusing is only the right answer while
    # the beat has something ELSE to show — see the second pass below.
    crowded: list = []

    filled = 0
    for p in mine:
        n = p.shot
        shot = shots[n - 1] if 0 < n <= len(shots) else {}
        wanted = p.end_ms - p.start_ms
        # The tier is decided from how this shot was placed, before anything
        # is cut. A mode that will not show this tier must not spend a
        # minute of ffmpeg on it, and must not quietly drop it either.
        was_stated = bool(stated and (p.beat, p.shot) in stated)
        tier = tiers.tier_of(p.method if p.ok else "none", stated=was_stated)
        if not tiers.places(mode, tier):
            held_back.append((p, tier, shot))
            continue
        if not p.ok or not p.path:
            # No line, no picture — but the script named the episode, and
            # showing the right episode beats showing nothing at all.
            # This shot's own window, not the beat's. A beat routinely
            # draws from several episodes — 24 of 34 on a real script — so a
            # single window per beat is one episode's stretch applied to
            # everybody else's footage.
            at, path = _filler_for(episode, used, log,
                                   (windows or {}).get((p.beat, p.shot)))
            if at is None:
                unplaced += 1
                continue
            p = align.Placement(beat=p.beat, shot=p.shot, path=path,
                                start_ms=int(at * 1000),
                                end_ms=int(at * 1000) + max(4000, wanted),
                                method="filler", confidence="low")
            filled += 1
        moved = _free_moment(used, p.path, p.start_ms / 1000.0)
        if moved is None:
            # Everything within reach is already on screen somewhere.
            repeats += 1
            crowded.append(p)
            continue
        # An episode has an end, and a placement can walk off it. Two shots
        # of a real build were cut at 2918s and 3488s of a 2848-second
        # episode: ffmpeg wrote a file with no video in it, both segments
        # failed to render, and the video came out eleven seconds short.
        # Cheaper to notice here than to discover it during the render.
        length = episode_length(p.path)
        if length and moved >= length - 1.0:
            log(f"      scene {index}: shot {n} is past the end of "
                f"{os.path.basename(p.path)} ({moved:.0f}s of {length:.0f}s)")
            unplaced += 1
            continue
        start = moved
        end = start + max(1.0, wanted / 1000.0)
        res.source = res.source or os.path.basename(p.path)
        # The weakest placement in the scene, not the last one seen: a scene
        # is only as trustworthy as its least certain shot.
        rank = {"high": 3, "medium": 2, "low": 1}
        if not res.confidence or rank.get(p.confidence, 0) < rank.get(res.confidence, 0):
            res.confidence = p.confidence

        try:
            # A moving clip only when the placement earned it. A guess
            # becomes a still below — never confident wrong motion.
            if not _wants_still(shot) and p.method in MOTION_OK:
                clip_path = os.path.join(scene_dir, f"clip_{n:02d}.mp4")
                # Cut the LONGEST the timeline could ever ask for, not the
                # nominal clip length. These two disagreed: clips were cut
                # at 4.0s and the timeline planned up to 6.0s, so 42 clips
                # of a real build were asked to run longer than the footage
                # that existed. ffmpeg cannot invent frames, so each one
                # came out short, and 34 seconds vanished from an
                # eleven-minute video — silently, and cumulatively, until
                # the picture finished 45 seconds ahead of the voice.
                #
                # This is raw material. How much of it is used is the
                # timeline's decision, made later and changeable without
                # re-cutting anything.
                headroom = max(job.clip_seconds, CLIP_HEADROOM_S)
                cutter.cut_clip(p.path, start, min(end, start + headroom),
                                clip_path, height=job.height)
                res.clips.append(clip_path)
                res.methods[os.path.basename(clip_path)] = p.method
                res.tiers[os.path.basename(clip_path)] = tier
                res.origins[os.path.basename(clip_path)] = round(start, 2)
                res.sources[os.path.basename(clip_path)] = os.path.basename(p.path)
                _mark_used(used, p.path, start)

            want = _still_count(shot, job.stills_per_scene)
            got = _stills_for(p.path, start, end, scene_dir, n, want, seen, log,
                              used)
            for still, at in got:
                res.stills.append(still)
                res.methods[os.path.basename(still)] = p.method
                res.tiers[os.path.basename(still)] = tier
                res.origins[os.path.basename(still)] = round(at, 2)
                res.sources[os.path.basename(still)] = os.path.basename(p.path)
                _mark_used(used, p.path, at)
        except (ProbeError, ValueError, OSError) as exc:
            log(f"      scene {index}: shot {n} failed — {exc}")
            continue

    # Cards for everything this mode would not show, and for a beat that
    # ended up with nothing at all. The duration is the narration's, not the
    # footage's — a gap that does not hold its own time is a gap that shifts
    # every scene after it.
    for p, tier, shot in held_back:
        made = _needs_visual(job, scene_dir, index, res, p, tier, shot,
                             episode, log)
        if not made:
            unplaced += 1

    if not (res.clips or res.stills) and crowded and not res.cards:
        # A repeated shot is a small fault. An empty beat is a large one: the
        # renderer covers it by holding a neighbour across it, so a beat with
        # nothing becomes somebody else's shot on screen for ten seconds, in
        # the wrong place, with no label saying so. Four scenes of a real
        # build went that way and every one of them was visible.
        #
        # So the de-duplication gives up here and only here, once it is the
        # difference between a repeat and a hole.
        log(f"      scene {index}: showing {len(crowded)} repeated shot(s) "
            "rather than leaving this beat empty")
        for p in crowded:
            n = p.shot
            shot = shots[n - 1] if 0 < n <= len(shots) else {}
            start = p.start_ms / 1000.0
            end = start + max(1.0, (p.end_ms - p.start_ms) / 1000.0)
            length = episode_length(p.path)
            if length and start >= length - 1.0:
                continue
            res.source = res.source or os.path.basename(p.path)
            res.confidence = res.confidence or p.confidence
            try:
                if not _wants_still(shot):
                    clip_path = os.path.join(scene_dir, f"clip_{n:02d}.mp4")
                    cutter.cut_clip(p.path, start,
                                    min(end, start + max(job.clip_seconds,
                                                         CLIP_HEADROOM_S)),
                                    clip_path, height=job.height)
                    res.clips.append(clip_path)
                    res.methods[os.path.basename(clip_path)] = p.method
                    res.origins[os.path.basename(clip_path)] = round(start, 2)
                    res.sources[os.path.basename(clip_path)] = \
                        os.path.basename(p.path)
                else:
                    got = _stills_for(p.path, start, end, scene_dir, n,
                                      _still_count(shot, job.stills_per_scene),
                                      None, log, None)
                    for still, at in got:
                        res.stills.append(still)
                        res.methods[os.path.basename(still)] = p.method
                        res.origins[os.path.basename(still)] = round(at, 2)
                        res.sources[os.path.basename(still)] = \
                            os.path.basename(p.path)
            except (ProbeError, ValueError, OSError) as exc:
                log(f"      scene {index}: shot {n} failed — {exc}")

    if res.cards and not (res.clips or res.stills):
        res.status = "needs_visual"
        res.note = (f"{len(res.cards)} shot(s) ke liye koi bharosemand "
                    "footage nahi — editor me bharna hoga")
    elif res.clips or res.stills:
        res.status = "cut" if res.clips else "fallback"
        if res.cards:
            res.note = (f"{len(res.cards)} shot(s) card ban gaye — "
                        "unke liye saboot nahi tha")
        elif filled:
            res.note = (f"{filled} shot(s) filled from this episode — no line "
                        "and no picture matched them")
        elif unplaced:
            res.note = f"{unplaced} shot(s) could not be placed"
        elif repeats:
            res.note = (f"{repeats} shot(s) skipped — already on screen "
                        "earlier in this video")
        elif not res.clips:
            res.note = "stills only"
    elif repeats:
        res.status = "empty"
        res.note = (f"every shot here ({repeats}) was already on screen "
                    "earlier in this video")
    else:
        res.status = "empty"
        res.note = ("nothing in this beat could be placed — no quoted line "
                    "anywhere near it")

    if res.narration:
        with open(os.path.join(scene_dir, "scene.txt"), "w",
                  encoding="utf-8") as f:
            f.write(res.narration)
    return res


def _stills_for(path: str, start: float, end: float, scene_dir: str,
                shot_no: int, want: int, seen: list | None,
                log=lambda *a: None, used: dict | None = None) -> list:
    """Sharp, distinct frames from around a placement.

    Sampling at fixed fractions of the clip was cheaper and wrong: it lands on
    motion blur, on the black frame between two shots, and on five views of
    one static moment. These are scored and de-duplicated against every still
    already taken for this video.

    Returns [(path, seconds_into_the_episode)]. The time travels with the
    file because it cannot be recovered afterwards, and an editor asked to
    swap one still for a better one has to know where to look.
    """
    # The window widens with the number of stills wanted, and so does the
    # minimum gap between them. A fixed 1.5s window asked for two frames out
    # of eight seconds of a static two-hander, and the de-duplicator quite
    # correctly found the two best — which were the same picture, because in
    # eight seconds of that shot nothing moves. On the last build 75 of 103
    # still-shots produced a pair, and side by side on the contact sheet many
    # of those pairs are plainly one image printed twice.
    reach = STILL_WINDOW_S * max(1, want)
    lo = max(0.0, start - reach)
    hi = end + reach
    try:
        cands = frames.scan(path, lo, hi)
    except ProbeError as exc:
        log(f"      still scan failed — {exc}")
        return []
    gap = max(frames.MIN_GAP_S, (hi - lo) / (want * 2.0)) if want > 1 else \
        frames.MIN_GAP_S
    # A frame from a moment already on screen is the same picture however
    # different its pixels happen to be — and in a moving shot they always
    # are, which is why the perceptual test alone let a six-second stretch
    # supply thirty-one pictures.
    cands = [c for c in cands
             if not _repeated(used, path, c.time)]
    best = frames.pick(cands, want, min_gap=gap, exclude=seen)
    out = []
    for k, c in enumerate(best, 1):
        still = os.path.join(scene_dir, f"image_{shot_no:02d}_{k}.jpg")
        try:
            cutter.extract_frame(path, c.time, still, width=1920)
            out.append((still, c.time))
            if seen is not None:
                seen.append((c.phash, c.colour))
        except ProbeError:
            pass
    return out


def _asset_score(scene, path: str, ceiling: float) -> float:
    """How much an editor should trust this asset.

    A shot anchored on a quoted line is on that line to the millisecond. A
    shot interpolated along the scene is in the right place to within a shot
    or two. Flattening both to one number would hide the difference at the
    only moment it can still be checked cheaply.
    """
    method = scene.methods.get(os.path.basename(path), "unknown")
    base = {"high": 1.0, "medium": 0.7}.get(scene.confidence, 0.5)
    # "verified" sits with "anchor" on purpose. One was located by a line that
    # is provably spoken there; the other by a picture that provably matches
    # the description. Both were checked against the film. Interpolation was
    # not, and the gap between "checked" and "inferred" is the only thing in
    # this manifest an editor cannot recover by looking.
    weight = 1.0 if method in ("anchor", "verified") else 0.75
    return round(ceiling * base * weight, 3)


def write_manifest(job, result: JobResult) -> str:
    """The contract the editor tools read: assets, scores, provenance."""
    payload = {
        "video": job.name,
        "generated_at": int(time.time()),
        "clip_seconds": job.clip_seconds,
        "scenes": [{
            "scene": s.index,
            "narration": s.narration,
            "status": s.status,
            "note": s.note,
            "source": s.source,
            "confidence": s.confidence,
            "anchored": s.anchored,
            "verified": s.verified,
            "interpolated": s.interpolated,
            "paced": s.paced,
            "filler": s.filler,
            "needs_visual": len(s.cards),
            "assets": (
                [{"file": os.path.basename(p), "kind": "video",
                  "placed_by": s.methods.get(os.path.basename(p), "unknown"),
                  "tier": s.tiers.get(os.path.basename(p), "C"),
                  "source_start": s.origins.get(os.path.basename(p)),
                  "source": s.sources.get(os.path.basename(p), s.source),
                  "score": _asset_score(s, p, 1.0)}
                 for p in s.clips]
                + [{"file": os.path.basename(p), "kind": "image",
                    "placed_by": s.methods.get(os.path.basename(p), "unknown"),
                    "tier": s.tiers.get(os.path.basename(p), "C"),
                    "source_start": s.origins.get(os.path.basename(p)),
                    "source": s.sources.get(os.path.basename(p), s.source),
                    "score": _asset_score(s, p, 0.9)}
                   for p in s.stills]
                + [{"file": os.path.basename(p), "kind": "image",
                    "placed_by": "needs_visual", "tier": "C",
                    "source_start": None,
                    "source": s.sources.get(os.path.basename(p), ""),
                    "score": 0.0}
                   for p in s.cards]),
        } for s in result.scenes],
    }
    path = os.path.join(job.out, MANIFEST)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return path


# How far past the shots a run DID place its filler may sit. A run whose
# anchors cover ninety seconds is describing a sequence, not a whole episode.
RUN_SPAN_PAD_S = 120.0
# ...but never tighter than the run itself needs. A span is a hint about
# WHERE, and it must not become a statement about how much room there is.
# One placed shot out of eighty-five collapsed a whole run to four minutes,
# filler could not fit inside it, and eighteen scenes of a real build came
# out empty — which the renderer covered by holding their neighbours across
# them. Every second of screen time the run asks for gets at least this much
# episode to find it in.
RUN_SPAN_PER_SHOT_S = 8.0


def _run_needs(run) -> float:
    """The least amount of episode a run's shots can be spread across."""
    return max(RUN_SPAN_PAD_S * 2.0, len(run.entries) * RUN_SPAN_PER_SHOT_S)


def _spans_by_beat(beats: list, placements: list) -> dict:
    """{(beat, shot): (lo, hi)} — the stretch each run's placed shots cover.

    The strongest statement about where a run belongs is not a model's
    opinion; it is the shots of that same run which were already placed on
    real evidence. A run with four anchors between 31 and 36 minutes is
    describing that sequence, and its unplaced shots belong beside them —
    not spread across the episode by a golden-ratio walk that has never
    heard of the scene.

    This is what was missing. Every other guard reasoned about one shot at a
    time; a run knows more than any of its shots do.
    """
    out: dict = {}
    by_key = {(p.beat, p.shot): p for p in placements}
    for run in align.runs(beats):
        real = []
        for entry in run.entries:
            p = by_key.get((entry.beat, entry.shot))
            if p is not None and p.ok and p.path:
                real.append((p.start_ms / 1000.0, p.end_ms / 1000.0))
        if not real:
            continue
        lo = min(a for a, _b in real) - RUN_SPAN_PAD_S
        hi = max(b for _a, b in real) + RUN_SPAN_PAD_S
        short = _run_needs(run) - (hi - lo)
        if short > 0:
            lo -= short / 2.0
            hi += short / 2.0
        for entry in run.entries:
            out[(entry.beat, entry.shot)] = (max(0.0, lo), hi)
    return out


def _apply_clues(job, report, log) -> dict:
    """The clue script's windows, applying it here only if nobody has yet.

    The pre-flight does this work and hands the enriched beats straight to
    this function's caller, so the normal path is a lookup. The fallback
    matters for the CLI, where a job can be run against a report built some
    other way — and it costs one subtitle query per remembered line, which
    is cheap enough not to be worth a flag.
    """
    if getattr(report, "clue_windows", None):
        if getattr(report, "clue_note", ""):
            log(f"  {report.clue_note}")
        return dict(report.clue_windows)
    path = (job.extras.get("clues") or "").strip()
    if not path:
        return {}
    try:
        found = clues_mod.read(path)
        if not found:
            return {}
        log(f"  clue script: {len(found)} clue — har line subtitle me check "
            "hogi, yaad kiya hua kuch bhi seedha nahi maana jayega")
        return clues_mod.enrich(job.db, report.beats, found, log=log).windows
    except Exception as exc:                    # never fatal, ever
        log(f"  clue script lagaya nahi ja saka — {exc}")
        log("  build waise hi chalega, bas clue wala fayda nahi milega.")
        return {}


def _episodes_by_beat(db_path: str, beats: list) -> dict:
    """{beat number: episode file} for every run in the script.

    Never raises: an episode the library cannot resolve simply has no
    filler, which is the behaviour this replaced.
    """
    out: dict = {}
    try:
        for run in align.runs(beats):
            path = align.episode_file(db_path, run)
            if not path:
                continue
            for entry in run.entries:
                out.setdefault(entry.beat, path)
    except Exception:
        return out

    # A beat can name no episode at all. Six shots of a real script were
    # press portraits — Vince Gilligan, an actor at a premiere, rows of
    # cinema seats — which live nowhere in a library of episodes, so those
    # beats had nothing and the video had a hole where the narration was
    # talking about the writers' room. The neighbouring beats know which
    # episode the essay is in at that point, and that is the right answer
    # for a held face under a line about the making of it.
    order = [b.get("beat") for b in beats if b.get("beat") is not None]
    last = ""
    for beat_no in order:                      # carry forward
        last = out.get(beat_no) or last
        if last:
            out.setdefault(beat_no, last)
    last = ""
    for beat_no in reversed(order):            # then back, for the opening
        last = out.get(beat_no) or last
        if last:
            out.setdefault(beat_no, last)
    return out


def run_job(job, report, log=print) -> JobResult:
    """Build one video's footage. Isolated: never propagates an exception."""
    t0 = time.time()
    result = JobResult(job=job)
    try:
        os.makedirs(job.out, exist_ok=True)
        # Placed once for the whole script: a run of shots from one episode
        # is laid along that scene together, which is what lets the silent
        # ones inherit a position from the few that quote a line.
        mode = tiers.normalise(job.extras.get("mode"))
        if mode == tiers.STRICT:
            log("  STRICT mode — sirf wahi footage lagegi jiska saboot hai. "
                "Baaki har beat par NEEDS VISUAL card aayega.")
        elif mode == tiers.DRAFT:
            log("  DRAFT mode — har beat bhara jayega, kamzor wale bhi. "
                "Ye rough cut ke liye hai; ise accuracy mat samajhna.")
        # Before anything is placed, because a clue's whole contribution is
        # to make the visual script quote lines it did not quote before —
        # and the aligner can only use what the script says when it reads
        # it. Every line was checked against the real subtitles inside; what
        # arrives here is already evidence rather than recollection.
        clue_windows = _apply_clues(job, report, log)
        placements = align.align(job.db, report.beats, log=log)
        log("  " + align.summarise(placements))
        # Alignment says where a shot probably is. This says whether the
        # picture there is the one the script asked for, and moves it when it
        # is not. Without the model installed it reports why and changes
        # nothing — a build never depends on it.
        checked = verify.apply(job.db, report.beats, placements, log=log)
        log(checked.summary())
        seen: list = []          # every still already taken, for de-duplication
        used: dict = {}          # and every moment of every episode used
        # Which episode each beat belongs to, whether or not anything in it
        # could be placed. A beat nobody could place still names its episode,
        # and that is enough to show the right show rather than nothing.
        owns = _episodes_by_beat(job.db, report.beats)
        # Shots dialogue could not place used to fall straight through to
        # filler. Now the picture index is asked where the description
        # actually happens — which is the only thing that works on a scene
        # nobody speaks in, and those are the scenes worth making videos
        # about. Runs after verify so it only sees what is genuinely homeless.
        # Which stretch of its episode each run happens in. Asked of the
        # whole run at once rather than shot by shot: twenty descriptions
        # from one scene agreeing a little is worth far more than one of
        # them being confident, and on a scene nobody speaks in it is the
        # only signal there is.
        # Who the script says is on screen, from the reference photographs.
        # Empty unless somebody supplied a cast folder, and everything below
        # behaves exactly as it did before when it is.
        people = cast.load(job.extras.get("cast") or "", log=log)
        windows = verify.locate_runs(job.db, report.beats, people=people,
                                     log=log)
        # And a time somebody stated overrules all of it. The model gets an
        # opinion only where nobody has told it the answer.
        said = (timings.from_script(report.beats)
                + timings.parse_lines(job.extras.get("timings") or ""))
        stated = timings.windows_for(report.beats, said, log=log)
        # ...but only where a quoted line does not say otherwise. A line
        # matched in the real subtitles is a millisecond somebody can go and
        # check; a typed range is not, and on a real script four of five
        # were wrong by seven to fifteen minutes.
        stated = timings.honour(report.beats, placements, stated, log=log)
        # Clue windows sit between the two: stronger than the picture
        # model's opinion, because both their ends are timestamps read out
        # of a subtitle file — and weaker than a line somebody typed, which
        # is a person who has watched the episode. So they are applied
        # first and a typed range overwrites them.
        clue_windows = timings.honour(report.beats, placements,
                                      clue_windows, log=log)
        windows.update(clue_windows)
        windows.update(stated)
        # Counted as stated for tiering: a bracketed run is "which two
        # minutes", never "which second", so a shot paced inside one is
        # Tier B. `tiers.tier_of` applies that ceiling; nothing here can
        # promote filler past it.
        # `{**a, **b}` rather than `dict(a, **b)`: these keys are (beat,
        # shot) tuples, and the second spelling routes them through keyword
        # arguments, which must be strings. It raised "TypeError: keywords
        # must be strings" after forty minutes of cutting.
        stated = {**clue_windows, **stated}
        verify.place_by_picture(job.db, report.beats, placements,
                                episodes=owns, windows=windows,
                                people=people, log=log)
        # And whatever is still homeless after that is not homeless at all: it
        # is a shot with a known position in a known sequence inside a known
        # stretch of the episode. Laying those out in order is the difference
        # between a scene that plays and eighty-five clips in eighty-five
        # unrelated places. Runs last, so it only ever fills what neither the
        # dialogue nor the picture could speak for.
        verify.pace_runs(job.db, report.beats, placements, windows, log=log)
        # A run's own placed shots outrank any model's opinion about where it
        # belongs — they are measurements, and the window is a guess. Worked
        # out after place_by_picture so it sees everything that got placed.
        found = _spans_by_beat(report.beats, placements)
        for key, span in found.items():
            # ...except where somebody stated the time. That is not an
            # opinion to be improved on, and widening it to whatever the
            # run's own shots happen to cover would quietly undo the one
            # instruction the tool was actually given.
            if key not in stated:
                windows[key] = span
        if found:
            log(f"    {len(set(found.values()))} run(s) bounded by their own "
                "placed shots; filler stays inside those")
        # Last of all, and only if a vision model is configured: hand every
        # still-interpolated shot in a wide window to Gemini to pick the
        # actual frame. This is the one step that can reach the silent beats
        # — a killing, a bell, a straightened tie — that carry no dialogue to
        # anchor on. Off by default; a build with no key runs exactly as
        # before. It moves guesses onto looked-at frames and never touches an
        # anchor or a stated time.
        refine_mod.refine_runs(report.beats, placements, windows, log=log)
        for i, beat in enumerate(report.beats, 1):
            scene = build_scene(job, i, beat, placements, seen, log, used,
                                owns.get(beat.get("beat", i), ""),
                                windows=windows, mode=mode, stated=stated)
            result.scenes.append(scene)
            mark = {"cut": "·", "reused": "=", "fallback": "~", "empty": "!",
                    "needs_visual": "□"}
            log(f"    scene {i:03d} {mark.get(scene.status, '?')} "
                f"{len(scene.clips)} clip(s), {len(scene.stills)} still(s)"
                + (f", {len(scene.cards)} card(s)" if scene.cards else "")
                + (f"   {scene.note}" if scene.note else ""))
        # What the build worked out for itself, in the form the Scene
        # timings box takes. A run that quoted a line has already said where
        # it is to the millisecond; printing that back is the difference
        # between "look it up in your player" and "paste this in".
        learned = timings.derive(report.beats, placements)
        if learned:
            log("  Scene timings, worked out from the lines that matched — "
                "inhe box me paste kar do:")
            for shots, line, count in learned:
                log(f"      {line:<22} ({shots} shots, {count} matched line(s))")
        tally: dict = {}
        for scene in result.scenes:
            for name in list(scene.methods):
                got = scene.tiers.get(name, "C")
                tally[got] = tally.get(got, 0) + 1
        if tally:
            log("  " + tiers.summary(tally))
        cards = sum(len(s.cards) for s in result.scenes)
        if cards:
            log(f"  {cards} NEEDS VISUAL card(s) — inhe editor me bharna hai")
        write_manifest(job, result)
        result.status = "done" if result.gaps == 0 else "partial"
    except Exception as exc:                    # one job must never kill the queue
        result.status = "failed"
        result.error = f"{type(exc).__name__}: {exc}"
        log(f"    FAILED: {result.error}")
        log(traceback.format_exc(limit=3))
    result.seconds = time.time() - t0
    return result


def run_queue(job_file: str, log=print, dry_run=False,
              allow_gaps=True) -> list[JobResult]:
    """Pre-flight everything, then build what passed."""
    queue = jobs_mod.load_jobs(job_file)
    log(f"{len(queue)} job(s) queued from {os.path.basename(job_file)}")

    reports = jobs_mod.preflight_all(queue, log=lambda m: log("  " + str(m)))
    log(jobs_mod.format_reports(reports))
    if dry_run:
        return [JobResult(job=r.job, status="skipped") for r in reports]

    results = []
    runnable = {id(r) for r in reports
                if r.status == "READY" or (allow_gaps and r.status == "GAPS")}
    log(f"\nBUILDING {len(runnable)} of {len(reports)} job(s)\n")

    for i, rep in enumerate(reports, 1):
        if id(rep) not in runnable:
            log(f"[{i}/{len(reports)}] ⏭  skipping {rep.job.name!r} — "
                + "; ".join(c.name for c in rep.failures() if c.fatal))
            results.append(JobResult(job=rep.job, status="skipped",
                                     error="; ".join(
                                         f"{c.name}: {c.detail}"
                                         for c in rep.failures() if c.fatal)))
            continue
        log(f"[{i}/{len(reports)}] building {rep.job.name!r} "
            f"({len(rep.beats)} beats)")
        results.append(run_job(rep.job, rep, log))

    log(format_results(results))
    _write_queue_report(job_file, reports, results)
    return results


def format_results(results: list[JobResult]) -> str:
    lines = ["", "RESULTS", ""]
    width = max((len(r.job.name) for r in results), default=10)
    for i, r in enumerate(results, 1):
        if r.status == "skipped":
            lines.append(f"  {r.icon} {i:>2}. {r.job.name:<{width}}  skipped — "
                         f"{r.error[:70]}")
            continue
        lines.append(f"  {r.icon} {i:>2}. {r.job.name:<{width}}  "
                     f"{len(r.scenes)} scenes {term.sym('dot')} {r.clips} clips "
                     f"{term.sym('dot')} {r.stills} stills "
                     f"{term.sym('dot')} {r.seconds:.0f}s"
                     + (f" {term.sym('dot')} {r.gaps} gap(s)" if r.gaps else "")
                     + (f" {term.sym('dot')} {r.error}" if r.error else ""))
    done = sum(1 for r in results if r.status == "done")
    partial = sum(1 for r in results if r.status == "partial")
    failed = sum(1 for r in results if r.status == "failed")
    skipped = sum(1 for r in results if r.status == "skipped")
    d = term.sym("dot")
    lines += ["", f"  {done} complete {d} {partial} with gaps {d} "
                  f"{failed} failed {d} {skipped} skipped"]
    return "\n".join(lines)


def _write_queue_report(job_file: str, reports, results) -> None:
    out = os.path.splitext(job_file)[0] + "_report.json"
    payload = []
    for rep, res in zip(reports, results):
        payload.append({
            "name": rep.job.name,
            "preflight": rep.status,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail,
                        "fatal": c.fatal} for c in rep.checks],
            "shots_total": rep.shots_total,
            "shots_resolved": rep.shots_resolved,
            "result": res.status,
            "clips": res.clips, "stills": res.stills, "gaps": res.gaps,
            "seconds": round(res.seconds, 1), "error": res.error,
            "out": rep.job.out,
        })
    try:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except OSError:
        pass
