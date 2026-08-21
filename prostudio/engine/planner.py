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
               clip_min=2.0, clip_max=5.0, img_min=2.6, img_max=7.0,
               jl_offset=0.4, niche="Movie Essay"):
    import math
    from .audio_sync import duration
    from .subjects import detect_faces
    shots = []
    starved = 0
    for si, (scene, (w0, w1)) in enumerate(zip(scenes, windows)):
        # J/L cut: shift the visual boundary off the audio boundary
        v0 = max(0.0, w0 - jl_offset) if (si % 2 == 1 and si > 0) else w0
        v1 = w1
        span = v1 - v0
        pool_v = list(scene.videos)
        pool_i = list(scene.images)
        # budget clips first (they carry the story) — capped to each clip's
        # REAL length so a short clip is never frozen-stretched
        t = v0
        scene_shots = []
        for v in pool_v:
            if v1 - t < clip_min:
                break
            d = min(clip_max, max(clip_min, span * 0.45), v1 - t)
            real = duration(v.path)
            if real > 0:
                d = min(d, max(1.2, real))
            scene_shots.append(Shot(v.path, "video", t, t + d, si, scene.mood))
            t += d
        # images fill the rest — each UNIQUE image used once; longer holds
        # over repeats; top up a media-starved scene with distinct clip frames.
        remaining = v1 - t
        imgs = list(pool_i)
        if remaining >= img_min:
            max_hold, target = 8.5, 5.5
            need_min = max(1, math.ceil(remaining / max_hold))
            n_pref = max(1, round(remaining / target))
            if len(imgs) < max(need_min, n_pref) and scene.videos:
                short = max(need_min, n_pref) - len(imgs)
                imgs += _clip_fill_frames(scene.videos[0].path, min(short, 4))
            n = min(len(imgs), max(need_min, n_pref)) if imgs else 0
            seq = imgs[:n]
            if seq:
                share = remaining / len(seq)
                for k, m in enumerate(seq):
                    d = share if k < len(seq) - 1 else (v1 - t)
                    scene_shots.append(Shot(m.path, m.kind, t, t + d, si,
                                            scene.mood))
                    t += d

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
