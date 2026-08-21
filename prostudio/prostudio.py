#!/usr/bin/env python3
"""ProStudio — script + scene folders + narration audio -> finished 4K MP4.

Single job:
  python prostudio.py --scenes DIR --audio narration.mp3 --out video.mp4 \
      [--script clean_script.txt] [--format F2|auto|random] [--language en]
      [--niche "Movie Essay"] [--no-keyword-colors] [--resolution 4K|1080p]

Queue (the GUI writes this):
  python prostudio.py --queue jobs.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import RESOLUTIONS  # noqa: E402
from engine.audio_sync import scene_windows, word_time  # noqa: E402
from engine.formats import FORMATS, NICHE_BASE, resolve_format  # noqa: E402
from engine.planner import plan_shots, read_scenes  # noqa: E402
from engine.renderer import render_job  # noqa: E402
from engine.script_nlp import chunk_scene, select_text_events  # noqa: E402


@dataclass
class Job:
    scenes_dir: str
    audio: str
    out_path: str
    script: str = ""            # optional clean script (fallback narration)
    instructor: str = ""        # optional visual-editor file (per-scene guide)
    format_choice: str = "auto"
    language: str = "en"
    niche: str = "Movie Essay"
    keyword_colors: bool = True
    text: bool = False          # on-screen text OFF by default (optional);
                                #   clean footage = premium clips/animation focus
    resolution: str = "4K"
    whisper_model: str = "base"
    seed: int = 0
    # derived
    format_key: str = ""
    crf: int = 19
    preset: str = "medium"


def plan_job(job: Job, job_index: int = 0, log=print) -> dict:
    """Everything EXCEPT the final render: QC, audio sync, text plan, shot
    plan, text-zone assignment. Returns an editable plan so a review UI can
    show/adjust it before the (slow) final export."""
    rng = random.Random(job.seed or (job_index + 1) * 7919)
    job.format_key = resolve_format(job.format_choice, job_index, rng)
    if job.resolution not in RESOLUTIONS:
        job.resolution = "4K"
    log("=" * 62)
    log(f"JOB: {os.path.basename(job.out_path)}  "
        f"[{job.format_key} | {job.niche} | {job.language} | {job.resolution}]")
    log("=" * 62)

    # 1) scenes + media QC
    log("[  2%] checking footage (removing black/blurry/duplicate media) ...")
    scenes = read_scenes(job.scenes_dir, log)

    # visual-editor / instructor file (optional): the editor's per-scene plan
    # overrides the scene.txt narration and can pin the exact on-screen text.
    forced_text = {}            # scene_i -> explicit on-screen text
    if job.instructor and os.path.isfile(job.instructor):
        from engine.planner import parse_instructor
        blocks = parse_instructor(job.instructor)
        if blocks:
            for i, s in enumerate(scenes):
                if i < len(blocks):
                    b = blocks[i]
                    if b.get("narration"):
                        s.narration = b["narration"]
                        from engine.script_nlp import scene_mood
                        s.mood = scene_mood(s.narration)
                    if job.text and b.get("on_screen"):
                        forced_text[i] = b["on_screen"]
            log(f"  visual-editor file: {len(blocks)} scene blocks applied"
                + (f", {len(forced_text)} with pinned on-screen text"
                   if forced_text else ""))
        else:
            log("  visual-editor file: no scene blocks recognized (ignored)")

    # language sanity: warn if the script uses a non-Latin script the bundled
    # fonts can't draw (text ON only) — the video still renders, text may show
    # boxes until a matching font is set via PS_FONT_SANS/SERIF/MONO.
    if job.text:
        # fail-fast font check: if the on-screen-text font can't be opened,
        # don't render every shot and THEN crash at compositing — warn now and
        # continue with clean footage so the video still completes.
        from engine.formats import FORMATS
        from PIL import ImageFont
        for role_font in {FORMATS[job.format_key]["font"]}:
            try:
                ImageFont.truetype(role_font, 40)
            except Exception:
                log(f"  WARNING: on-screen-text font could not be opened "
                    f"({role_font}). Rendering CLEAN footage (no text) so the "
                    "video still completes. Reinstall/redownload the tool so "
                    "assets/fonts/ is present, or set PS_FONT_SANS to a .ttf.")
                job.text = False
                break
    if job.text:
        from engine.textlayout import script_needs_font
        sample = " ".join(s.narration for s in scenes[:4])
        script = script_needs_font(sample)
        if script:
            log(f"  NOTE: script looks like {script}. The bundled fonts are "
                "Latin-only — set PS_FONT_SANS/SERIF/MONO to a font for this "
                "language, or turn on-screen text OFF. (Video still renders.)")
    # narration fallback from clean script (split by sentences across scenes)
    empty = [s for s in scenes if not s.narration]
    if empty and job.script and os.path.isfile(job.script):
        text = open(job.script, encoding="utf-8", errors="replace").read()
        import re
        sents = re.split(r"(?<=[.!?])\s+", " ".join(text.split()))
        per = max(1, len(sents) // max(1, len(scenes)))
        for i, s in enumerate(scenes):
            if not s.narration:
                s.narration = " ".join(sents[i * per:(i + 1) * per])

    # 2) audio-synced scene windows (+ whisper word times when available)
    log("[ 12%] syncing to narration audio "
        "(first run may download the Whisper model — a few minutes, one time) ...")
    windows, words = scene_windows(scenes, job.audio,
                                   model_size=job.whisper_model,
                                   language=job.language, log=log)

    # 3) text plan (NLP chunks + crucial moments + cadence policy)
    if job.text:
        from engine.audio_sync import align_narration_times
        scene_chunks = []
        for s, w in zip(scenes, windows):
            chunks = chunk_scene(s.narration, colorize=job.keyword_colors)
            wtimes = align_narration_times(s.narration, w, words)
            scene_chunks.append((chunks, w, s.narration, wtimes))
        events = select_text_events(scene_chunks, windows)
        # editor-pinned on-screen text always wins for its scene: drop the
        # auto text there and place the pinned lines across the scene window.
        if forced_text:
            from engine.script_nlp import forced_scene_events
            events = [e for e in events if e[2] not in forced_text]
            for si, ftext in forced_text.items():
                events += forced_scene_events(ftext, windows[si], si,
                                              colorize=job.keyword_colors)
            events.sort(key=lambda e: e[0])
            events = [tuple(e) for e in events]
            # de-overlap after merge (previous text rolls off as next lands)
            events = [list(e) for e in events]
            for a, b in zip(events, events[1:]):
                if a[1] > b[0] - 0.08:
                    a[1] = max(a[0] + 0.7, b[0] - 0.08)
            events = [tuple(e) for e in events if e[1] - e[0] >= 0.55]
        log(f"  text events: {len(events)}"
            + ("  (word-synced via whisper)" if words else
               "  (silence-sync fallback; whisper gives word-perfect timing)"))
    else:
        events = []
        log("  on-screen text: OFF (clean footage for manual editing)")

    # 4) shot plan (clips first, J/L cuts, drift seeds, subject-safe zones)
    log("[ 20%] planning shots (arranging clips + images, avoiding faces) ...")
    shots = plan_shots(scenes, windows, rng, log, niche=job.niche)
    log(f"  shots: {len(shots)}  "
        f"(avg {sum(s.secs for s in shots)/max(1,len(shots)):.1f}s)")

    # ROTATE text position per event (editorial variety, not captions), but
    # veto any zone that would cover a detected face on the shot underneath.
    from engine.subjects import ZONES, _overlap
    from engine.textlayout import ZONE_ROTATION, ZONE_XY
    ev_with_zone = []
    cursor = 0
    prev_zone = None
    for (t0, t1, si, ch) in events:
        faces = []
        for sh in shots:
            if sh.t0 <= t0 < sh.t1:
                faces = sh.faces
                break

        def covers_face(zname):
            cx, cy = ZONE_XY[zname]
            box = (cx - 0.22, cy - 0.10, cx + 0.22, cy + 0.10)
            return any(_overlap(box, f) > 0.010 for f in faces)

        chosen = None
        for step in range(len(ZONE_ROTATION)):
            z = ZONE_ROTATION[(cursor + step) % len(ZONE_ROTATION)]
            if z != prev_zone and not covers_face(z):
                chosen = z
                cursor = (cursor + step + 1) % len(ZONE_ROTATION)
                break
        if chosen is None:                      # every zone hits a face
            chosen = min(ZONE_ROTATION,
                         key=lambda z: sum(_overlap(
                             (ZONE_XY[z][0]-0.22, ZONE_XY[z][1]-0.10,
                              ZONE_XY[z][0]+0.22, ZONE_XY[z][1]+0.10), f)
                             for f in faces))
            cursor = (cursor + 1) % len(ZONE_ROTATION)
        prev_zone = chosen
        ev_with_zone.append((t0, t1, si, ch, chosen))

    return {
        "job": job, "shots": shots, "events": ev_with_zone,
        "scenes": scenes, "windows": windows, "words": words,
        "rejected_media": sum(len(s.rejected) for s in scenes),
    }


def render_from_plan(job: Job, shots, ev_with_zone, log=print,
                     proxy=False, resume=False) -> dict:
    """Render the (possibly user-edited) plan to a final MP4 (or a fast proxy
    for the browser review page)."""
    t_start = time.time()
    from engine.renderer import render_job as _render
    out, total = _render(job, shots, ev_with_zone, log, proxy=proxy,
                         resume=resume)
    report = {
        "output": out, "seconds": round(total, 1),
        "format": job.format_key, "niche": job.niche,
        "language": job.language,
        "resolution": ("proxy" if proxy else job.resolution),
        "shots": len(shots), "text_events": len(ev_with_zone),
        "render_minutes": round((time.time() - t_start) / 60, 1),
    }
    if not proxy:
        rp = os.path.splitext(out)[0] + "_report.json"
        with open(rp, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
    return report


def run_job(job: Job, job_index: int = 0, log=print, resume=False) -> dict:
    """Plan + render in one shot (the classic non-review path)."""
    plan = plan_job(job, job_index, log)
    rep = render_from_plan(plan["job"], plan["shots"], plan["events"], log,
                           resume=resume)
    rep["rejected_media"] = plan["rejected_media"]
    return rep


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--queue", help="jobs.json written by the GUI")
    p.add_argument("--scenes")
    p.add_argument("--audio")
    p.add_argument("--out", default="ProStudio.mp4")
    p.add_argument("--script", default="")
    p.add_argument("--instructor", default="",
                   help="visual-editor file (per-scene narration + on-screen text)")
    p.add_argument("--format", default="auto",
                   help="F1..F10 name, 'auto' (rotate) or 'random'")
    p.add_argument("--language", default="en")
    p.add_argument("--niche", default="Movie Essay",
                   choices=list(NICHE_BASE))
    p.add_argument("--no-keyword-colors", action="store_true")
    p.add_argument("--text", action="store_true",
                   help="add on-screen text (OPTIONAL; off by default)")
    p.add_argument("--no-text", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--resolution", default="4K", choices=list(RESOLUTIONS))
    p.add_argument("--whisper-model", default="base")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--resume", action="store_true",
                   help="skip finished videos and continue partly-rendered "
                        "ones from where they stopped")
    a = p.parse_args(argv)

    jobs = []
    if a.queue:
        data = json.load(open(a.queue, encoding="utf-8"))
        for j in data["jobs"]:
            jobs.append(Job(
                scenes_dir=j["scenes"], audio=j["audio"],
                out_path=j["out"], script=j.get("script", ""),
                instructor=j.get("instructor", ""),
                format_choice=j.get("format", "auto"),
                language=j.get("language", "en"),
                niche=j.get("niche", "Movie Essay"),
                keyword_colors=j.get("keyword_colors", True),
                text=j.get("text", False),
                resolution=j.get("resolution", data.get("resolution", "4K")),
                whisper_model=j.get("whisper_model", "base"),
                seed=j.get("seed", 0)))
    else:
        if not (a.scenes and a.audio):
            p.error("--scenes and --audio required (or --queue)")
        jobs.append(Job(scenes_dir=a.scenes, audio=a.audio, out_path=a.out,
                        script=a.script, instructor=a.instructor,
                        format_choice=a.format,
                        language=a.language, niche=a.niche,
                        keyword_colors=not a.no_keyword_colors,
                        text=a.text and not a.no_text,
                        resolution=a.resolution,
                        whisper_model=a.whisper_model, seed=a.seed))

    ok = fail = skip = 0
    for i, job in enumerate(jobs):
        # on --resume, a video that already finished (has its report) is skipped
        report = os.path.splitext(job.out_path)[0] + "_report.json"
        if a.resume and os.path.isfile(report) and os.path.isfile(job.out_path):
            print(f"[{i+1}/{len(jobs)}] SKIP {job.out_path} (already done)")
            skip += 1
            continue
        try:
            rep = run_job(job, i, resume=a.resume)
            print(f"[{i+1}/{len(jobs)}] OK {rep['output']} "
                  f"({rep['seconds']}s video, {rep['render_minutes']} min render)")
            ok += 1
        except Exception as exc:
            print(f"[{i+1}/{len(jobs)}] FAILED {job.out_path}: {exc}")
            fail += 1
    print(f"QUEUE DONE: {ok} ok, {fail} failed, {skip} skipped")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
