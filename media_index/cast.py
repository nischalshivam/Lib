"""Who is in the frame, from a handful of photographs.

The complaint this module exists for, in the words it was made in:

    "ye na ho ki baat ho rahi hai walter white and gus ke scene ki and waha
     screen pe show ho rha hai walter white ki wife and uske bete ka scene"

That failure is not a placement failure. The tool put the shot in a
plausible stretch of the right episode; the stretch simply contained the
wrong people. Nothing in the pipeline had any notion of who a person was,
so nothing could rule it out.

## Why photographs and not descriptions

The picture search already reads `visual`, and on this material it is weak:
measured on a real episode, **only 2 of 84 descriptions beat what a caption
about nothing scores in the same episode**. That is chance. SigLIP is being
asked to connect "a calm man in glasses and a yellow shirt" to a dim
interior frame, and it mostly cannot.

Comparing two *pictures* is the thing that model is actually good at, and
the two pictures here are unusually well matched: a still from the show
against frames of the same show, with the same lens, the same grade and the
same lighting. So the reference images are encoded with the identical
encoder the library was built with, and compared against frames that are
already on disk. No re-index, no new dependency, no second model.

## What this is not

It is not face recognition. It answers "does this frame look like the
pictures I was given", which is a wider question — a reference still of Gus
in his office will match the office as happily as the man. Hence:

  - give it CLOSE shots of the person, several, from different scenes
  - it earns its keep by RULING OUT, not by picking. A frame that scores far
    below the others for every named character is a frame those characters
    are probably not in, and that is the judgement that keeps Skyler and
    Walt Jr. off the screen during a sentence about Gus.

## The folder

    cast/
      Gus/     1.jpg 2.jpg 3.jpg
      Walter/  1.jpg 2.jpg
      Mike/    ...

Folder name is the character's name. The script names the same person in a
shot's `characters` field, and the two are matched loosely, so "Gus",
"gus fring" and "Gustavo Fring" all find the `Gus` folder.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np

from . import embed, visual
from .library import normalize
from .probe import ProbeError, require_ffmpeg

IMAGE_TYPES = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
# More than this from one person is a photo album, not a reference. The
# encoder cost is per image and the tenth photograph of the same face moves
# the average by nothing measurable.
MAX_IMAGES = 12
# A frame has to stand this far above the episode's ordinary agreement with
# a face before that face is called present. A similarity on its own is
# unreadable and every episode's ordinary level is different, so this is in
# the units `spread_of` below produces.
PRESENT_LIFT = 1.5
# ...and this is where the bonus is fully earned. Between the two it fades
# in, so a frame the reference only half agrees with gets half the nudge.
CERTAIN_LIFT = 4.0
# How far a shot's score may be pushed by who is in it. Deliberately a
# nudge, not a veto: the reference images are a handful of stills and this
# must never be able to overrule a quoted line or a stated time.
CAST_WEIGHT = 0.35


class CastError(Exception):
    pass


@dataclass
class Person:
    name: str
    images: list = field(default_factory=list)
    vec: np.ndarray | None = None      # the mean of their reference images

    @property
    def key(self) -> str:
        return normalize(self.name)


def read_image(path: str, size: int = embed.IMAGE_SIZE) -> np.ndarray:
    """One photograph as the pixels the library's frames were made from.

    Through ffmpeg, with the same filter string `visual.frame_batches` uses,
    because "the same size" is not enough — those frames were STRETCHED to a
    square, not letterboxed, and a reference image that preserved its aspect
    ratio would be a differently-shaped picture of the same face. Small
    difference, and it is exactly the kind that quietly costs the accuracy
    this module was built to buy.
    """
    import subprocess                                       # noqa: PLC0415

    cmd = [require_ffmpeg(), "-v", "error", "-i", path,
           "-vf", f"scale={size}:{size},format=rgb24",
           "-frames:v", "1", "-f", "rawvideo", "-"]
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, timeout=60).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise CastError(f"could not read {os.path.basename(path)}: {exc}") from exc
    want = size * size * 3
    if len(out) < want:
        raise CastError(f"could not read {os.path.basename(path)}")
    return np.frombuffer(out[:want], dtype=np.uint8).reshape(size, size, 3)


def look(root: str) -> list:
    """[{name, images}] for a cast folder, without loading any model.

    What the New Video page shows the moment a folder is chosen: somebody
    should learn that they spelt a folder `Guss` in the first second, not
    after a forty-minute build.
    """
    if not root or not os.path.isdir(root):
        raise CastError("ye folder nahi mila")
    out = []
    for name in sorted(os.listdir(root)):
        here = os.path.join(root, name)
        if not os.path.isdir(here):
            continue
        shots = [f for f in sorted(os.listdir(here))
                 if f.lower().endswith(IMAGE_TYPES)]
        if shots:
            out.append({"name": name, "images": len(shots)})
    if not out:
        raise CastError("is folder me koi character folder nahi — "
                        "cast\\Gus\\1.jpg jaisa structure chahiye")
    return out


def load(root: str, backend=None, log=lambda *a: None) -> dict:
    """{normalised name: Person} with every reference image encoded.

    Never raises for one bad photograph. A folder of ten stills where two
    are corrupt is nine-tenths of a working reference, and refusing all of
    it would be the tool choosing tidiness over the person's video.
    """
    if not root or not os.path.isdir(root):
        return {}
    try:
        backend = backend or embed.load(log=log)
    except embed.EmbedError as exc:
        log(f"      characters are off — {exc}")
        return {}

    people: dict = {}
    for name in sorted(os.listdir(root)):
        here = os.path.join(root, name)
        if not os.path.isdir(here):
            continue
        files = [os.path.join(here, f) for f in sorted(os.listdir(here))
                 if f.lower().endswith(IMAGE_TYPES)][:MAX_IMAGES]
        pixels, kept = [], []
        for path in files:
            try:
                pixels.append(read_image(path))
                kept.append(path)
            except (CastError, ProbeError) as exc:
                log(f"      {name}: skipped {os.path.basename(path)} — {exc}")
        if not pixels:
            continue
        vecs = backend.encode_images(np.stack(pixels))
        # The mean of several stills, re-normalised. One photograph is one
        # angle in one room; the average of six is much closer to "this
        # person" and much further from "this room", which is the whole
        # difficulty with using a general image model for a face.
        mean = embed.unit(vecs.mean(axis=0))[0]
        person = Person(name=name, images=kept, vec=mean)
        people[person.key] = person
        log(f"      {name}: {len(kept)} reference image(s)")
    return people


def named_in(shot: dict, people: dict) -> list:
    """Which of the known people a shot says are in it.

    Reads the script's `characters` field, and falls back to looking for the
    names inside `visual` — a caption saying "Gus stands over Victor" names
    them just as clearly as a list would, and a script written before this
    feature existed still gets the benefit.
    """
    if not people or not shot:
        return []
    said = shot.get("characters") or shot.get("people") or []
    if isinstance(said, str):
        said = [p for p in said.replace(";", ",").split(",") if p.strip()]
    found, seen = [], set()
    for one in said:
        key = normalize(str(one))
        for person in people.values():
            if not key or person.key in seen:
                continue
            if person.key in key or key in person.key:
                found.append(person)
                seen.add(person.key)
    if found:
        return found
    text = normalize(f"{shot.get('visual') or ''} {shot.get('note') or ''}")
    for person in people.values():
        if person.key and person.key in text and person.key not in seen:
            found.append(person)
            seen.add(person.key)
    return found


def presence(index: visual.VisualIndex, person: Person) -> np.ndarray:
    """One lift per frame: how far this frame stands out for this person.

    Scaled from the BOTTOM of the distribution — the median down to the 25th
    percentile — and not from the top, which is where every other
    measurement in this package takes its scale from.

    That difference is the whole reason this works. A description matches a
    handful of frames, so the 95th percentile is safely outside the match
    and `p95 - median` is a clean measure of ordinary. A main character is
    in a third of the episode, so the 95th percentile lands INSIDE their own
    frames — the scale is set by the very thing being measured, and the lift
    saturates at about 1.0 no matter how certain the agreement is. Measured
    on a fixture where the person held ten frames of sixty: every one of
    them scored 1.0, under the bar, and the feature did nothing at all.

    The bottom quarter of frames is the one part of the episode a character
    is reliably absent from, whatever their screen time, so it is the only
    stable ruler available here.
    """
    if person is None or person.vec is None or not len(index):
        return np.zeros(len(index), dtype=np.float32)
    sims = index.similarities(person.vec)
    med = float(np.median(sims))
    spread = med - float(np.percentile(sims, 25))
    if spread <= 1e-6:
        return np.zeros(len(index), dtype=np.float32)
    return ((sims - med) / spread).astype(np.float32)


def frames_with(index: visual.VisualIndex, wanted: list) -> np.ndarray:
    """A per-frame bonus in [0, 1] for the people a shot names.

    The best of the named people rather than all of them. A script naming
    "Gus, Walter, Jesse" for a wide shot is naming who is in the SCENE; a
    frame holding any one of them is a frame of that scene, and demanding
    all three would rule out every close-up in it.
    """
    if not len(index) or not wanted:
        return np.zeros(len(index), dtype=np.float32)
    best = np.max(np.stack([presence(index, p) for p in wanted]), axis=0)
    return np.clip((best - PRESENT_LIFT) / (CERTAIN_LIFT - PRESENT_LIFT),
                   0.0, 1.0).astype(np.float32)
