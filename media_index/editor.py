"""Changing a built video by hand.

Sixteen builds went into making the tool pick the right shot on its own, and
it now picks a good one most of the time. Most of the time is not the same
as every time, and the last stretch is not a coding problem: SigLIP cannot
reliably tell one dim interior from another, and no amount of tuning will
make it. What closes that gap is a person, two clicks, and ten frames to
choose between.

So this does three things to a folder that has already been built:

  * offers alternatives — the same episode, searched by description
  * swaps one shot for another moment of the same episode
  * changes how long a shot holds, or removes it

## What it will not do

It will not touch the footage the builder cut for shots nobody changed, and
it will not re-run a build. Every edit here is one asset and one entry in
`timeline.json`, so a mistake costs one shot rather than an hour — which is
the whole reason editing exists as a separate step from building.
"""
from __future__ import annotations

import json
import os
import shutil

from . import cutter, library, visual

ALT_DIR = "_alternatives"       # thumbnails for the chooser, inside the job
ALT_KEEP = 10
ALT_APART_S = 8.0               # ten different moments, not ten of one
ALT_WIDTH = 640


class EditError(RuntimeError):
    pass


def _read(path: str) -> dict:
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _write(path: str, data: dict) -> None:
    # Written beside and moved into place: a half-written timeline is worse
    # than an old one, and a browser can ask for this file at any moment.
    tmp = path + ".writing"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _find(timeline: dict, scene: int, filename: str) -> tuple:
    for s in (timeline.get("scenes") or []):
        if int(s.get("scene") or 0) != int(scene):
            continue
        for item in (s.get("items") or []):
            if item.get("file") == filename:
                return s, item
    raise EditError(f"scene {scene} has no {filename}")


def episode_path(db_path: str, name: str) -> str:
    """The full path of an episode the manifest names only by file name."""
    if not name:
        return ""
    if os.path.isfile(name):
        return name
    con = library.connect(db_path)
    try:
        for row in con.execute("SELECT path FROM media").fetchall():
            if os.path.basename(row["path"]) == os.path.basename(name):
                return row["path"]
    finally:
        con.close()
    return ""


def alternatives(out: str, db_path: str, scene: int, filename: str,
                 query: str = "", log=lambda *a: None) -> dict:
    """Ten other moments of the same episode, ranked against a description.

    The description is whatever the person typed, defaulting to the scene's
    own narration. That is deliberate: the build searched for the script's
    visual line and got this wrong, so offering the same search again would
    mostly offer the same answer.
    """
    from . import embed

    timeline = _read(os.path.join(out, "timeline.json"))
    scene_row, item = _find(timeline, scene, filename)
    wanted = (query or "").strip() or scene_row.get("narration", "")
    if not wanted:
        raise EditError("nothing to search for — type a description")

    episode = episode_path(db_path, item.get("source") or "")
    if not episode:
        raise EditError(f"this shot's episode is not in the library: "
                        f"{item.get('source') or 'unknown'}")

    con = library.connect(db_path)
    try:
        index = visual.load(con, db_path, episode)
    finally:
        con.close()
    if index is None:
        raise EditError(
            f"{os.path.basename(episode)} has no picture index yet — "
            "run L in start.bat, with the script box left blank")

    ok, why = embed.available()
    if not ok:
        raise EditError(
            "The picture model is not installed, so there is nothing to "
            f"search with — {why}. Install it with: "
            "pip install torch transformers sentencepiece")
    try:
        backend = embed.load(log=log)
        vec = backend.encode_texts([wanted])[0]
    except Exception as exc:
        # The model is what makes a choice possible at all. Its own error
        # names huggingface and a config file, which explains nothing to
        # someone who only wants a different shot.
        raise EditError("The picture model could not be loaded, so there is "
                        f"nothing to search with — {str(exc)[:180]}") from exc
    picks = visual.top_in(index, vec, n=ALT_KEEP, apart=ALT_APART_S)

    folder = os.path.join(out, ALT_DIR)
    shutil.rmtree(folder, ignore_errors=True)       # last chooser's frames
    os.makedirs(folder, exist_ok=True)
    shown = []
    for n, match in enumerate(picks, 1):
        still = os.path.join(folder, f"alt_{n:02d}.jpg")
        try:
            cutter.extract_frame(episode, match.time, still, width=ALT_WIDTH)
        except Exception as exc:                    # one bad frame, not none
            log(f"  could not read {match.time:.1f}s — {exc}")
            continue
        shown.append({
            "file": os.path.basename(still),
            "at": round(match.time, 2),
            "score": round(match.similarity, 4),
            "lift": round(match.lift, 2),
            "confidence": match.confidence,
            "current": abs(match.time - float(item.get("source_start") or -1e9))
                       < ALT_APART_S / 2,
        })
    return {"scene": scene, "file": filename, "query": wanted,
            "episode": os.path.basename(episode), "folder": ALT_DIR,
            "candidates": shown, "searched": len(index)}


def replace(out: str, db_path: str, scene: int, filename: str, at: float,
            seconds: float = 0.0, log=lambda *a: None) -> dict:
    """Cut the same episode at `at` and put it where the old shot was.

    The new asset keeps the old file name. Everything that refers to a shot
    — the timeline, the manifest, the rendered segment — refers to it by
    name, and renaming would mean finding every one of them.
    """
    timeline = _read(os.path.join(out, "timeline.json"))
    scene_row, item = _find(timeline, scene, filename)
    episode = episode_path(db_path, item.get("source") or "")
    if not episode:
        raise EditError("this shot's episode is not in the library")

    scene_dir = os.path.join(out, f"scene_{int(scene):03d}")
    os.makedirs(scene_dir, exist_ok=True)
    target = os.path.join(scene_dir, filename)
    hold = float(seconds or item.get("duration") or 4.0)

    if item.get("kind") == "video":
        cutter.cut_clip(episode, at, at + hold, target)
    else:
        cutter.extract_frame(episode, at, target, width=1920)

    item["source_start"] = round(float(at), 2)
    item["placed_by"] = "chosen"        # a person decided this one
    item["confidence"] = "high"
    _write(os.path.join(out, "timeline.json"), timeline)
    _touch_manifest(out, scene, filename, at)
    # The rendered segment for this scene is now wrong. Deleting it is how
    # the renderer is told to redo only this one rather than all of them.
    _forget_segment(out, timeline, scene, filename)
    log(f"  scene {scene}: {filename} now from {at:.1f}s")
    return {"scene": scene, "file": filename, "source_start": item["source_start"]}


def _touch_manifest(out: str, scene: int, filename: str, at: float) -> None:
    path = os.path.join(out, "manifest.json")
    try:
        manifest = _read(path)
    except (OSError, ValueError):
        return                              # the timeline is the one that matters
    for s in (manifest.get("scenes") or []):
        if int(s.get("scene") or 0) != int(scene):
            continue
        for a in (s.get("assets") or []):
            if a.get("file") == filename:
                a["source_start"] = round(float(at), 2)
                a["placed_by"] = "chosen"
    _write(path, manifest)


def _forget_segment(out: str, timeline: dict, scene: int, filename: str) -> None:
    """Drop the one rendered piece that held this shot.

    The renderer numbers segments `seg_0001.mp4` upward in exactly the order
    it walks the timeline, and skips any that already exist. Deleting the one
    that matters means the next render redoes one shot rather than all of
    them — which is the difference between a swap costing four seconds and
    costing forty minutes.
    """
    work = os.path.join(out, "segments")
    if not os.path.isdir(work):
        return
    n = 0
    for s in (timeline.get("scenes") or []):
        for item in (s.get("items") or []):
            n += 1
            if int(s.get("scene") or 0) == int(scene) \
                    and item.get("file") == filename:
                try:
                    os.remove(os.path.join(work, f"seg_{n:04d}.mp4"))
                except OSError:
                    pass                    # never rendered yet; nothing to do
                # The joined video no longer matches its parts.
                try:
                    os.remove(os.path.join(out, "video.mp4"))
                except OSError:
                    pass
                return


def set_duration(out: str, scene: int, filename: str, seconds: float) -> dict:
    """Hold a shot longer or shorter, and slide the rest of the video."""
    path = os.path.join(out, "timeline.json")
    timeline = _read(path)
    _scene_row, item = _find(timeline, scene, filename)
    item["duration"] = round(max(0.4, float(seconds)), 2)
    _retime(timeline)
    _write(path, timeline)
    _forget_all_segments(out)
    return {"total_seconds": timeline.get("total_seconds", 0.0)}


def remove(out: str, scene: int, filename: str) -> dict:
    """Take a shot out. Its time goes to the rest of its own scene."""
    path = os.path.join(out, "timeline.json")
    timeline = _read(path)
    scene_row, item = _find(timeline, scene, filename)
    items = scene_row["items"]
    if len(items) <= 1:
        # An empty scene is a hole, and a hole becomes a still that sits for
        # thirty seconds — the exact fault that cost builds eleven and twelve.
        raise EditError("this is the scene's only shot — replace it instead "
                        "of removing it, or the scene becomes a hole")
    freed = float(item.get("duration") or 0.0)
    items.remove(item)
    share = freed / len(items)
    for other in items:
        other["duration"] = round(float(other.get("duration") or 0.0) + share, 2)
    _retime(timeline)
    _write(path, timeline)
    _forget_all_segments(out)
    return {"scene": scene, "removed": filename,
            "total_seconds": timeline.get("total_seconds", 0.0)}


def _retime(timeline: dict) -> None:
    """Lay every start time out again after a duration changed.

    A timeline whose starts do not follow its durations renders correctly and
    reports nonsense — every number the editor shows would drift from what is
    actually on screen.
    """
    at = 0.0
    for scene in (timeline.get("scenes") or []):
        scene["start"] = round(at, 2)
        for item in (scene.get("items") or []):
            item["start"] = round(at, 2)
            at += float(item.get("duration") or 0.0)
        scene["end"] = round(at, 2)
    timeline["total_seconds"] = round(at, 2)


def _forget_all_segments(out: str) -> None:
    """Timings changed, so every rendered piece is the wrong length."""
    shutil.rmtree(os.path.join(out, "segments"), ignore_errors=True)
    for name in ("video.mp4",):
        try:
            os.remove(os.path.join(out, name))
        except OSError:
            pass


def summary(out: str) -> dict:
    """What the editor header shows: length, shots, and how they got there."""
    try:
        timeline = _read(os.path.join(out, "timeline.json"))
    except (OSError, ValueError):
        return {}
    counts: dict = {}
    shots = 0
    for scene in (timeline.get("scenes") or []):
        for item in (scene.get("items") or []):
            shots += 1
            key = item.get("placed_by") or "unknown"
            counts[key] = counts.get(key, 0) + 1
    return {"total_seconds": timeline.get("total_seconds", 0.0),
            "shots": shots, "counts": counts,
            "scenes": len(timeline.get("scenes") or []),
            "audio": timeline.get("audio", "")}
