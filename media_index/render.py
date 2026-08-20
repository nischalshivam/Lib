"""Turn a timeline into a finished video file.

Everything before this produces folders. Folders are not a video, and until
something plays end to end there is no way to judge the thing that is
actually being made — a contact sheet cannot tell you that a cut lands two
beats late, or that a still sits dead on screen for nine seconds.

The method is deliberately dull, because dull is what survives 150 shots:

  1. render every item to its own segment, all in one identical format
  2. concatenate the segments without re-encoding
  3. lay the narration over the result

The obvious alternative — one enormous ffmpeg filter graph with every clip
as an input — is faster and falls over. A 150-input command exceeds what
Windows will accept on a command line, one bad source kills the whole render
with no clue which, and there is nothing to resume from. Separate segments
cost one extra encode and buy a build that can be interrupted, restarted,
and diagnosed a shot at a time.

## Stills move

A still held for ten seconds is a slideshow, and a slideshow is the second
thing a viewer notices after identical durations. Every essay channel worth
copying puts a slow push or drift on a held frame, so these do too — with
the direction and speed varying per shot, seeded off the timeline so a
re-render is identical.

The image is scaled up before the move and back down after. Panning a
1920-wide still directly makes the pixel grid crawl, which is visible and
looks cheap; doing the motion at 3840 and resampling down does not.
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import time
from dataclasses import dataclass, field

from .probe import ProbeError, probe, require_ffmpeg

WIDTH, HEIGHT = 1920, 1080
FPS = 30
# High enough that the concatenated master loses nothing worth having, and
# the final mux is a stream copy so this is the only encode that matters.
SEGMENT_CRF = 18
SEGMENT_PRESET = "veryfast"
# How far a still travels over its time on screen. Small on purpose: the
# move should be felt rather than seen.
ZOOM_RANGE = (1.06, 1.16)
WORK_DIR = "segments"
# The longest any one picture may stay on screen once holes are absorbed.
# The same ceiling the timeline plans to, because a viewer cannot tell the
# difference between a still that was planned to run twelve seconds and one
# that ended up running twelve seconds.
MAX_HOLD_S = 12.0


class RenderError(RuntimeError):
    pass


@dataclass
class RenderResult:
    path: str = ""
    segments: int = 0
    reused: int = 0
    failed: list = field(default_factory=list)     # [(file, reason)]
    seconds: float = 0.0
    duration: float = 0.0
    planned: float = 0.0          # what the timeline asked for

    @property
    def ok(self) -> bool:
        return bool(self.path) and os.path.isfile(self.path)


def _run(cmd: list, timeout: int = 1800) -> None:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RenderError(str(exc)) from exc
    if r.returncode != 0:
        tail = (r.stderr or b"")[-400:].decode("utf-8", "replace")
        raise RenderError(tail.strip() or f"ffmpeg exited {r.returncode}")


FIT = (f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
       f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1")


def still_filter(duration: float, seed: int, motion: bool = True) -> str:
    """The move applied to a held frame.

    Direction and distance vary per shot so that twenty stills in a row do
    not all drift the same way — which would be a signature of its own,
    just a subtler one than identical durations.
    """
    frames = max(2, int(round(duration * FPS)))
    if not motion:
        return f"{FIT},fps={FPS}"
    rng = random.Random(f"still:{seed}")
    end = rng.uniform(*ZOOM_RANGE)
    # Half push in, half pull out.
    if rng.random() < 0.5:
        z = f"min(1+({end - 1:.4f})*on/{frames},{end:.4f})"
    else:
        z = f"max({end:.4f}-({end - 1:.4f})*on/{frames},1.0)"
    # Drift the centre a little as well, in one of four directions.
    dx, dy = rng.choice([(1, 0), (-1, 0), (0, 1), (0, -1)])
    px = f"(iw-iw/zoom)/2+{dx}*(iw-iw/zoom)/2*on/{frames}*0.35"
    py = f"(ih-ih/zoom)/2+{dy}*(ih-ih/zoom)/2*on/{frames}*0.35"
    return (f"scale={WIDTH * 2}:-2,"
            f"zoompan=z='{z}':x='{px}':y='{py}':d={frames}:"
            f"s={WIDTH}x{HEIGHT}:fps={FPS},setsar=1")


def _absorb(out: list, i: int, hole: float) -> None:
    """Share a hole between the shots on either side of it.

    Either side works and neither breaks sync: concatenation only cares
    about total duration, so a second added before the hole and a second
    added after it both leave everything downstream where it belongs. What
    matters is that no single shot swallows the lot — a picture that sits
    still for half a minute is the most obvious thing in a video.

    Nearest shots first, widening outward over the whole video if the ones
    beside the hole are already at MAX_HOLD_S. Only if EVERY shot is at its
    limit does the remainder go to the nearest one anyway — a finished video
    that is the right length beats a tidy rule.
    """
    left = i
    order = sorted(range(len(out)), key=lambda k: (abs(k - i - 0.5), k))
    for j in order:
        if hole <= 0.01:
            break
        room = MAX_HOLD_S - out[j]["duration"]
        if room <= 0.01:
            continue
        take = min(room, hole)
        out[j]["duration"] = round(out[j]["duration"] + take, 3)
        out[j]["held"] = round(out[j].get("held", 0) + take, 2)
        hole -= take
    if hole > 0.01:                      # nowhere left: the length still wins
        out[left]["duration"] = round(out[left]["duration"] + hole, 3)
        out[left]["held"] = round(out[left].get("held", 0) + hole, 2)


def _cover_failure(made: list, at: int, seconds: float, motion: bool,
                   log) -> None:
    """Give a failed shot's seconds to the shots that did render.

    The alternative is what happened on a real build: two segments would not
    encode, the video came out eleven seconds short, and every cut after the
    first failure sat ahead of the narration. Length is the one thing that
    must survive a failure — a wrong picture for four seconds is a mistake, a
    video that ends while the voice is still talking is a broken video.

    Nearest neighbours first, each to MAX_HOLD_S, re-rendering only the ones
    whose duration actually changes.
    """
    if not made:
        return
    order = sorted(range(len(made)), key=lambda k: abs(made[k][0] - at))
    left = seconds
    for k in order:
        if left <= 0.01:
            break
        _idx, item, seg, scene_dir, n = made[k]
        room = MAX_HOLD_S - item["duration"]
        if room <= 0.01:
            continue
        take = min(room, left)
        item["duration"] = round(item["duration"] + take, 3)
        item["held"] = round(item.get("held", 0) + take, 2)
        left -= take
        try:
            render_item(item, scene_dir, seg, seed=n, motion=motion)
        except (RenderError, ProbeError, ValueError) as exc:
            log(f"      could not lengthen segment {n} — {exc}")
    if left > 0.01:
        log(f"      {left:.1f}s of a failed shot could not be covered")


def plan_segments(timeline: dict) -> list:
    """Every segment to render, with the holes closed.

    Concatenation has no idea what time an item was meant to start at — it
    simply plays one file after another. So a beat with no footage does not
    leave a gap in the finished video, it *shortens* it, and everything
    afterwards slides earlier by that much. On the real eleven-minute build
    two empty beats and 42 clips shorter than they were planned to run left
    the picture 45 seconds ahead of the voice by the end, and the video
    ended while the narrator was still talking.

    Whatever the timeline says a beat occupies, that much video comes out.
    A hole is absorbed by the shots around it, and it is SHARED — because
    giving a whole hole to the one shot before it is how a twelve-second
    still ended up on screen for thirty seconds. Three and a half minutes of
    a real eleven-minute build had no footage, that time went to eleven
    shots, and each of them held eighteen seconds longer than planned.
    """
    out = []
    for scene in (timeline.get("scenes") or []):
        for item in (scene.get("items") or []):
            out.append({
                "scene": scene.get("scene"),
                "file": item.get("file", ""),
                "kind": item.get("kind", "image"),
                "start": float(item.get("start") or 0.0),
                "duration": max(0.05, float(item.get("duration") or 0.0)),
            })
    if not out:
        return out

    for i in range(len(out) - 1):
        hole = out[i + 1]["start"] - (out[i]["start"] + out[i]["duration"])
        if hole > 0.01:
            _absorb(out, i, hole)
    lead = out[0]["start"]
    if lead > 0.01:
        _absorb(out, 0, lead)
    total = float(timeline.get("total_seconds") or 0.0)
    tail = total - (out[-1]["start"] + out[-1]["duration"])
    if tail > 0.01:
        # The narration runs on past the last picture. Holding the closing
        # shots is right: cutting to black while someone is still speaking is
        # the most visible mistake a video can end on. Shared backwards from
        # the end, for the same reason every other hole is shared.
        _absorb(out, len(out) - 1, tail)
    return out


def render_item(item: dict, source_dir: str, out_path: str, seed: int,
                motion: bool = True) -> None:
    """One visual, encoded to the one format every segment shares.

    The segment comes out at exactly the duration asked for, whatever the
    source holds. A clip cut to four seconds and asked to run five and a
    half used to yield four — ffmpeg's `-t` cannot invent footage — and 42
    of those quietly removed 34 seconds from an eleven-minute video and
    pulled everything after them out of sync with the voice.

    `tpad` clones the final frame to cover the shortfall, so the picture
    holds instead of the timeline slipping. It is a freeze rather than an
    invention, and it is visible in the report.
    """
    name = item.get("file") or ""
    src = os.path.join(source_dir, name)
    if not os.path.isfile(src):
        raise RenderError(f"missing {name}")
    duration = max(0.05, float(item.get("duration") or 0))
    ff = require_ffmpeg()

    if str(item.get("kind")) == "video":
        cmd = [ff, "-y", "-v", "error", "-i", src, "-t", f"{duration:.3f}",
               "-vf", (f"tpad=stop_mode=clone:stop_duration={duration:.3f},"
                       f"{FIT},fps={FPS}")]
    else:
        cmd = [ff, "-y", "-v", "error", "-loop", "1", "-i", src,
               "-t", f"{duration:.3f}",
               "-vf", still_filter(duration, seed, motion)]
    # No audio on a segment. The film's own sound under a narration track is
    # a mixing decision, and mixing it in here would bake it in permanently.
    cmd += ["-an", "-c:v", "libx264", "-crf", str(SEGMENT_CRF),
            "-preset", SEGMENT_PRESET, "-pix_fmt", "yuv420p",
            "-r", str(FPS), out_path]
    _run(cmd)


def _concat(segments: list, out_path: str, work: str) -> None:
    """Join segments without re-encoding them."""
    listing = os.path.join(work, "segments.txt")
    with open(listing, "w", encoding="utf-8") as f:
        for seg in segments:
            # ffmpeg's concat parser takes single quotes literally, so a
            # path containing one has to escape it. Windows paths rarely do;
            # a show called "Bob's Burgers" does.
            safe = os.path.abspath(seg).replace("'", "'\\''")
            f.write(f"file '{safe}'\n")
    _run([require_ffmpeg(), "-y", "-v", "error", "-f", "concat", "-safe", "0",
          "-i", listing, "-c", "copy", out_path])


def _add_audio(video: str, audio: str, out_path: str) -> None:
    """Lay the narration over the picture, copying the video through."""
    _run([require_ffmpeg(), "-y", "-v", "error", "-i", video, "-i", audio,
          "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
          "-c:a", "aac", "-b:a", "192k", "-shortest", out_path])


def render(timeline: dict, out_path: str, source_dir: str = "",
           audio: str = "", motion: bool = True, resume: bool = True,
           log=lambda *a: None) -> RenderResult:
    """Build the finished file. Never raises — a bad shot is reported.

    Resuming is free and matters: this is the slow step, and a queue of six
    videos overnight must not lose four hours to one interrupted render.
    """
    res = RenderResult()
    t0 = time.time()
    source_dir = source_dir or os.path.dirname(os.path.abspath(out_path))
    work = os.path.join(source_dir, WORK_DIR)
    os.makedirs(work, exist_ok=True)

    items = plan_segments(timeline)
    if not items:
        res.failed.append(("timeline", "no items to render"))
        return res

    res.planned = round(sum(i["duration"] for i in items), 2)
    held = [i for i in items if i.get("held")]
    log(f"  rendering {len(items)} segment(s) at {WIDTH}x{HEIGHT}, "
        f"{res.planned / 60:.1f} min of picture")
    if held:
        log(f"      {len(held)} shot(s) hold a little longer to cover "
            f"{sum(i['held'] for i in held):.0f}s the script left empty")

    segments, made, holes = [], [], []
    for n, item in enumerate(items, 1):
        seg = os.path.join(work, f"seg_{n:04d}.mp4")
        scene_dir = os.path.join(source_dir, f"scene_{item['scene']:03d}")
        if resume and os.path.isfile(seg) and os.path.getsize(seg) > 1024:
            segments.append(seg)
            made.append((n - 1, item, seg, scene_dir, n))
            res.reused += 1
            continue
        try:
            render_item(item, scene_dir, seg, seed=n, motion=motion)
            segments.append(seg)
            made.append((n - 1, item, seg, scene_dir, n))
            res.segments += 1
        except (RenderError, ProbeError, ValueError) as exc:
            res.failed.append((item.get("file", "?"), str(exc)[:160]))
            log(f"      segment {n} failed — {exc}")
            # A shot that would not render takes its seconds with it, and
            # concatenation has no idea anything is missing: the video simply
            # comes out short and every cut after it drifts ahead of the
            # voice. Two failures cost a real build eleven seconds that way.
            holes.append((n - 1, item["duration"]))
        if n % 25 == 0:
            log(f"      {n}/{len(items)}  ({time.time() - t0:.0f}s)")

    for at, seconds in holes:
        _cover_failure(made, at, seconds, motion, log)

    if not segments:
        res.failed.append(("render", "every segment failed"))
        return res

    silent = os.path.join(work, "picture.mp4")
    log(f"  joining {len(segments)} segment(s)")
    try:
        _concat(segments, silent, work)
    except RenderError as exc:
        res.failed.append(("concat", str(exc)[:200]))
        return res

    final = out_path
    if audio and os.path.isfile(audio):
        log("  laying the narration over it")
        try:
            _add_audio(silent, audio, final)
        except RenderError as exc:
            res.failed.append(("audio", str(exc)[:200]))
            final = silent
    else:
        os.replace(silent, final)
        if audio:
            res.failed.append(("audio", f"not found: {audio}"))

    res.path = final if os.path.isfile(final) else ""
    try:
        res.duration = probe(res.path).duration if res.path else 0.0
    except ProbeError:
        res.duration = 0.0
    # The one check that catches a whole class of silent failure. Every
    # earlier version of this shortened the video without saying so, and a
    # video that ends while the narrator is still talking is the loudest
    # possible symptom of the quietest possible bug.
    if res.path and res.planned and abs(res.duration - res.planned) > 1.0:
        res.failed.append((
            "length",
            f"asked for {res.planned:.0f}s, got {res.duration:.0f}s — "
            f"{res.planned - res.duration:+.0f}s"))
        log(f"  WARNING: the video is {res.planned - res.duration:.0f}s "
            "shorter than the timeline; the picture will drift ahead of "
            "the voice")
    res.seconds = time.time() - t0
    return res


def render_folder(out_dir: str, out_name: str = "video.mp4",
                  audio: str = "", motion: bool = True, resume: bool = True,
                  log=lambda *a: None) -> RenderResult:
    """Render the timeline.json sitting in a built folder."""
    path = os.path.join(out_dir, "timeline.json")
    if not os.path.isfile(path):
        res = RenderResult()
        res.failed.append(("timeline.json",
                           "not found — plan the timing first"))
        return res
    with open(path, "r", encoding="utf-8-sig") as f:
        timeline = json.load(f)
    audio = audio or timeline.get("audio") or ""
    return render(timeline, os.path.join(out_dir, out_name),
                  source_dir=out_dir, audio=audio, motion=motion,
                  resume=resume, log=log)


def describe(res: RenderResult) -> str:
    from . import term
    d = term.sym("dot")
    if not res.ok:
        why = "; ".join(f"{a}: {b}" for a, b in res.failed[:3])
        return f"  nothing was written — {why or 'unknown'}"
    return (f"  {os.path.basename(res.path)} {d} "
            f"{res.duration / 60:.1f} min of "
            f"{res.planned / 60:.1f} planned {d} "
            f"{res.segments} rendered, {res.reused} reused {d} "
            f"{res.seconds / 60:.0f} min"
            + (f" {d} {len(res.failed)} shot(s) failed" if res.failed else ""))
