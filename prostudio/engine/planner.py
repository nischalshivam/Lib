"""The editor brain — scenes + QC'd media + timing -> a concrete shot plan.

Rules (from the user's manual-editing workflow + competitor analysis):
  - video clips carry the story: 2-5s each, placed first in every scene
  - images fill the remaining narration time: 3-7s, ALWAYS with motion
  - best-scored media first; repeats only when a scene is starved (with a
    different motion so it doesn't look repeated)
  - J/L cuts: visual boundaries lead/lag the audio boundary by ~0.4s
    (alternating), like a human editor
  - every item gets a text zone from subject analysis (negative space)
"""
from __future__ import annotations

import glob
import os
import random
import re
import subprocess
import tempfile
from dataclasses import dataclass, field

from .qc import IMAGE_EXT, VIDEO_EXT, MediaScore, qc_scene_media

_FRAME_CACHE = os.path.join(tempfile.gettempdir(), "prostudio_frames")


def _clip_fill_frames(clip_path, n):
    """Extract N stills from DIFFERENT moments of a clip → distinct Ken Burns
    shots (used when a scene is starved of images, instead of repeating a clip)."""
    from .audio_sync import duration
    os.makedirs(_FRAME_CACHE, exist_ok=True)
    d = max(1.0, duration(clip_path))
    base = os.path.splitext(os.path.basename(clip_path))[0]
    tag = str(abs(hash(clip_path)) % 100000)
    outs = []
    for i in range(n):
        ts = d * (i + 0.5) / n
        out = os.path.join(_FRAME_CACHE, f"{base}_{tag}_{i}.jpg")
        if not os.path.isfile(out):
            try:
                subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss",
                                f"{ts:.2f}", "-i", clip_path, "-frames:v", "1",
                                "-q:v", "2", out],
                               capture_output=True, timeout=60)
            except subprocess.TimeoutExpired:
                pass
        if os.path.isfile(out):
            outs.append(MediaScore(path=out, kind="image", ok=True))
    return outs


@dataclass
class Scene:
    index: int
    dir: str
    narration: str = ""
    mood: str = "neutral"
    videos: list = field(default_factory=list)   # MediaScore
    images: list = field(default_factory=list)
    rejected: list = field(default_factory=list)


@dataclass
class Shot:
    path: str
    kind: str                # image | video
    t0: float
    t1: float
    scene_i: int
    mood: str = "neutral"
    zoom_in: bool = True
    punch_in: bool = False   # organic mid-shot push
    drift_seed: int = 0
    src_in: float = 0.0      # seconds INTO a source video to start (in-point);
                             #   lets the user pick the best N sec of a long clip
    framing: str = ""        # "" = auto, "blurfill"/"full" force a framing look
    move: str = ""           # camera move: in/out/panl/panr/panu/pand/hold
    faces: list = field(default_factory=list)   # face boxes for text-zone veto
    transition: str | None = None   # into the NEXT shot

    @property
    def secs(self):
        return self.t1 - self.t0


def _natkey(p):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", p)]


_SCENE_HDR = re.compile(r"^\s*(#+\s*)?scene\s*\d+|^[A-Z0-9 ,'&\-]{6,}$", re.I)
_NARR_LBL = re.compile(r"(narration\s*/?\s*text|script\s*cue|narration)\s*:\s*(.*)", re.I)
_ONSCR_LBL = re.compile(r"on[-\s]?screen\s*text\s*:\s*(.*)", re.I)


def parse_instructor(path: str):
    """Parse the visual-editor / instructor file → per-scene
    {'narration':…, 'on_screen':…} blocks in order. Flexible about labels."""
    blocks, cur, field = [], None, None
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n")
            hdr = re.match(r"^\s*(#+\s*)?scene\s*\d+", line, re.I)
            if hdr:
                cur = {"narration": "", "on_screen": ""}
                blocks.append(cur)
                field = None
                continue
            if cur is None:
                continue
            m = _NARR_LBL.search(line)
            if m:
                cur["narration"] = m.group(2).strip().strip('“”"')
                field = "narration"
                continue
            m = _ONSCR_LBL.search(line)
            if m:
                cur["on_screen"] = m.group(1).strip().strip('“”"')
                field = "on_screen"
                continue
            if re.match(r"^\s*(SUMMARY|IMAGE|CLIP|VISUAL|EDITOR|SPOKEN|NOTES)\b", line, re.I):
                field = None
                continue
            if field and line.strip():
                cur[field] = (cur[field] + " " + line.strip()).strip()
    for b in blocks:
        b["narration"] = " ".join(b["narration"].split())
        if b["on_screen"].strip().lower() in ("none", "-", "n/a", "na"):
            b["on_screen"] = ""
    return blocks


def read_scenes(scenes_dir: str, log=print) -> list:
    dirs = sorted((d for d in glob.glob(os.path.join(scenes_dir, "scene_*"))
                   if os.path.isdir(d)), key=_natkey)
    if not dirs:
        raise FileNotFoundError(f"no scene_* folders in {scenes_dir}")
    from .script_nlp import scene_mood
    scenes, seen = [], []
    total_rej = 0
    for i, d in enumerate(dirs):
        s = Scene(index=i, dir=d)
        files = sorted(
            (f for f in glob.glob(os.path.join(d, "*"))
             if f.lower().endswith(IMAGE_EXT + VIDEO_EXT)), key=_natkey)
        s.videos, s.images, s.rejected = qc_scene_media(files, seen, log)
        total_rej += len(s.rejected)
        txt = os.path.join(d, "scene.txt")
        if os.path.isfile(txt):
            raw = open(txt, encoding="utf-8", errors="replace").read()
            m = re.search(r"NARRATION\s*/?\s*TEXT\s*:\s*(.+)", raw,
                          re.S | re.I)
            body = m.group(1) if m else raw
            body = body.strip().strip('“”"').strip()
            s.narration = " ".join(body.split())
        s.mood = scene_mood(s.narration)
        scenes.append(s)
    log(f"  scenes: {len(scenes)}, media rejected by QC: {total_rej}")
    return scenes


def _borrow_images(scenes, si):
    """Images from the NEAREST scene that has any (closest index first).

    Used only to fill a scene that has zero usable media of its own, so its
    narration window is still covered and the whole video stays in audio sync.
    We borrow IMAGES (never another scene's clip) so the story order is not
    disturbed."""
    order = sorted((k for k in range(len(scenes)) if k != si),
                   key=lambda k: (abs(k - si), k))
    for k in order:
        if scenes[k].images:
            return list(scenes[k].images)
    for k in order:                               # no images anywhere -> frames
        if scenes[k].videos:
            return _clip_fill_frames(scenes[k].videos[0].path, 4)
    return []


def _placeholder_still():
    """A neutral dark frame — absolute last resort when the ENTIRE project
    has no usable media at all (so the timeline never breaks)."""
    os.makedirs(_FRAME_CACHE, exist_ok=True)
    p = os.path.join(_FRAME_CACHE, "placeholder.png")
    if not os.path.isfile(p):
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f",
                        "lavfi", "-i", "color=c=0x0b0d10:s=1280x720",
                        "-frames:v", "1", p], capture_output=True, timeout=30)
    return p


def plan_shots(scenes, windows, rng: random.Random, log=print,
               clip_min=4.0, clip_max=5.0, img_min=4.0, img_max=7.0,
               jl_offset=0.4, niche="Movie Essay",
               safe_mode=None, hook_secs=60.0, img_share=0.70):
    """safe_mode (copyright): after the first `hook_secs` seconds, hold real
    moving footage to ~(1-img_share) of the screen time — the rest is filled with
    STILL frames pulled from the exact same matched clips (a freeze of the scene
    being discussed). Each remaining video clip stays <= clip_max (~5s). The first
    minute (the hook) is left clip-heavy for retention. This slashes how much
    continuous copyrighted video is on screen, which is what YouTube Content ID
    matches. Default ON; disable with env PS_SAFE=0."""
    import math
    from .audio_sync import duration
    from .subjects import detect_faces
    if safe_mode is None:
        safe_mode = os.environ.get("PS_SAFE", "1").strip().lower() not in ("0", "", "false", "no")
    try:
        img_share = float(os.environ.get("PS_IMG_SHARE", img_share))
        hook_secs = float(os.environ.get("PS_HOOK_SECS", hook_secs))
    except ValueError:
        pass
    shots = []
    starved = 0
    safe_clips = safe_imgs = 0
    for si, (scene, (w0, w1)) in enumerate(zip(scenes, windows)):
        # J/L cut: shift the visual boundary off the audio boundary
        v0 = max(0.0, w0 - jl_offset) if (si % 2 == 1 and si > 0) else w0
        v1 = w1
        # shots play back-to-back, so a J-cut must TAKE its lead from the
        # previous scene's last shot — before this, every odd scene added
        # jl_offset (0.4 s) to the video: 31 scenes = 12.4 s of drift, the
        # picture ended up seconds behind the voice.
        if shots and shots[-1].t1 > v0:
            shots[-1].t1 = max(shots[-1].t0 + 0.5, v0)
            v0 = shots[-1].t1
        span = v1 - v0
        pool_v = list(scene.videos)
        pool_i = list(scene.images)
        # In the hook (first minute) OR when safe_mode is off, clips carry the
        # story freely. In the safe zone, cap moving footage to ~(1-img_share)
        # of this scene's time so most of the screen is still frames.
        in_hook = (not safe_mode) or (v0 < hook_secs)
        vid_budget = span if in_hook else span * (1.0 - img_share)
        # budget clips first (they carry the story) — capped to each clip's
        # REAL length so a short clip is never frozen-stretched, and (safe zone)
        # to the moving-footage budget so clips never dominate.
        t = v0
        used_v = 0.0
        scene_shots = []
        for v in pool_v:
            if v1 - t < clip_min:
                break
            if not in_hook and (vid_budget - used_v) < clip_min:
                break                              # safe zone: clip budget spent
            d = min(clip_max, max(clip_min, span * 0.45), v1 - t)
            if not in_hook:
                d = min(d, vid_budget - used_v)
            real = duration(v.path)
            if real > 0:
                d = min(d, max(1.2, real))
            if d < clip_min - 0.4:
                break
            scene_shots.append(Shot(v.path, "video", t, t + d, si, scene.mood))
            t += d
            used_v += d
            if not in_hook:
                safe_clips += 1
        # images fill the rest — each UNIQUE image used once; longer holds
        # over repeats; top up with distinct STILL FRAMES cut from THIS scene's
        # own matched clips (a freeze of the exact scene) so the safe zone can be
        # image-heavy without copyrighted motion.
        remaining = v1 - t
        imgs = list(pool_i)
        if remaining >= img_min:
            # each still holds ~6.5s (min ~5) so cards are never rapid-fire
            max_hold, target = 9.5, 6.5
            need_min = max(1, math.ceil(remaining / max_hold))
            n_pref = max(1, round(remaining / target))
            need = max(need_min, n_pref)
            if len(imgs) < need and scene.videos:
                # pull frames from ALL of this scene's clips (not just the first)
                # so the safe zone has enough DISTINCT exact-scene stills.
                per = max(1, math.ceil((need - len(imgs)) / len(scene.videos)))
                for v in scene.videos:
                    if len(imgs) >= need:
                        break
                    imgs += _clip_fill_frames(v.path, min(per, 4))
            n = min(len(imgs), need) if imgs else 0
            seq = imgs[:n]
            if seq:
                share = remaining / len(seq)
                for k, m in enumerate(seq):
                    d = share if k < len(seq) - 1 else (v1 - t)
                    scene_shots.append(Shot(m.path, m.kind, t, t + d, si,
                                            scene.mood))
                    t += d
                    if not in_hook and m.kind == "image":
                        safe_imgs += 1

        # ---- GUARANTEE the whole window is covered (keeps audio in sync) ----
        # If this scene ran short (few/no clips, all images used, or the folder
        # was empty / entirely QC-rejected), fill the gap: this scene's OWN clip
        # frames first, then BORROW the nearest scene's images, then a neutral
        # still. A scene never contributes less video than its narration, so no
        # later scene can drift out of sync, and clips never bleed between scenes.
        covered = scene_shots[-1].t1 if scene_shots else v0
        if covered < v1 - 0.05:
            src = []
            if scene.videos:
                src = _clip_fill_frames(scene.videos[0].path, 4)
            if not src:
                src = _borrow_images(scenes, si)
                if src:
                    starved += 1
            if not src:
                src = [MediaScore(path=_placeholder_still(), kind="image",
                                  ok=True)]
                starved += 1
            gap = v1 - covered
            k = max(1, min(len(src) * 4, round(gap / 5.5)))
            tt = covered
            for idx in range(k):
                m = src[idx % len(src)]
                d = (v1 - tt) if idx == k - 1 else gap / k
                scene_shots.append(Shot(m.path, m.kind, tt, tt + d, si,
                                        scene.mood))
                tt += d

        # INTERLEAVE so video clips are spread out (a still between them), not
        # bunched. With the safe-zone mix ~70% stills the stills naturally
        # separate the clips, so no short filler frames are injected (those made
        # the edit flash by 1s at a time) — each shot keeps its full 4-7s hold.
        vids = [s for s in scene_shots if s.kind == "video"]
        imgs = [s for s in scene_shots if s.kind == "image"]
        if vids and imgs:
            gap = max(1, round(len(imgs) / len(vids)))   # stills between clips
            woven, vi, ii = [], 0, 0
            while vi < len(vids) or ii < len(imgs):
                if vi < len(vids):
                    woven.append(vids[vi]); vi += 1
                for _ in range(gap):
                    if ii < len(imgs):
                        woven.append(imgs[ii]); ii += 1
            tt = scene_shots[0].t0
            for s in woven:                              # re-stamp times in order
                d = s.t1 - s.t0
                s.t0, s.t1 = tt, tt + d
                tt += d
            # keep the scene window EXACT after shrink/inject: land the last shot
            # on the original scene end so audio stays perfectly in sync.
            if woven:
                woven[-1].t1 = max(woven[-1].t0 + 0.8, v1)
            scene_shots = woven

        # per-shot flavour: alternating zoom, occasional punch-in, drift seed,
        # detected faces (so text can be placed in negative space later)
        for j, sh in enumerate(scene_shots):
            sh.zoom_in = (j + si) % 2 == 0
            sh.punch_in = rng.random() < 0.22 and sh.secs > 3.0
            sh.drift_seed = rng.randrange(1000)
            sh.faces = detect_faces(sh.path, sh.kind)
        shots.extend(scene_shots)

    if starved:
        log(f"  note: {starved} scene(s) had too little footage — filled with "
            "borrowed/neutral visuals to keep audio in sync (add more clips to "
            "those scene folders for a richer edit)")
    if safe_mode:
        sv = sum(1 for s in shots if s.kind == "video")
        si_ = sum(1 for s in shots if s.kind == "image")
        vid_t = sum(s.secs for s in shots if s.kind == "video")
        img_t = sum(s.secs for s in shots if s.kind == "image")
        tot_t = max(0.001, vid_t + img_t)
        log(f"  copyright-safe ON: first {hook_secs:.0f}s clip-heavy, then "
            f"~{int(img_share*100)}% stills. Whole video: {img_t/tot_t*100:.0f}% "
            f"still / {vid_t/tot_t*100:.0f}% clip on screen "
            f"({si_} stills, {sv} clips; every clip <= {clip_max:.0f}s)")
    else:
        log("  copyright-safe OFF (PS_SAFE=0) — clips carry the whole video")
    # camera moves: varied + anti-repeat across the whole video (premium feel)
    from .variety import pick_framings, pick_moves
    for sh, mv in zip(shots, pick_moves(len(shots), rng)):
        sh.move = mv
    # framing per SCENE from the niche's pool ('full' -> auto so non-16:9 still
    # blur-fills instead of cropping)
    fr = pick_framings(len(scenes), niche, rng)
    for sh in shots:
        picked = fr[sh.scene_i] if sh.scene_i < len(fr) else "full"
        sh.framing = "" if picked == "full" else picked
    # transitions: within-scene soft, scene-boundary strong (format decides look)
    for a, b in zip(shots, shots[1:]):
        a.transition = "scene" if b.scene_i != a.scene_i else "soft"
    return shots
