"""End-to-end job runner: 3 inputs -> finished MP4 + report.json."""
from __future__ import annotations

import os
import shutil
import tempfile

from . import align, compose, cues, parser, qc, render, scenes, styles, util, zones


def run_job(video: str, script: str, instructions: str, out: str,
            opts: dict | None = None, progress=None, log=print):
    """opts: niche, pack, accent (r,g,b), energy, text_scale, density,
    cut_protect, crf, preset. progress(stage:str, frac:0..1)."""
    opts = dict(opts or {})

    def prog(stage, frac):
        if progress:
            progress(stage, frac)

    prog("probe", 0.01)
    info = util.probe(video)
    W, H, fps = info["width"], info["height"], info["fps"]
    log(f"[video] {W}x{H} @ {fps:g}fps, {info['duration']:.1f}s, "
        f"audio={'yes' if info['has_audio'] else 'NO'}")
    if not info["has_audio"]:
        raise RuntimeError("Video has no audio track — VText syncs text to "
                           "narration, so an audio track is required.")

    prog("parse", 0.03)
    header, events, problems = parser.parse_instruction_file(instructions)
    for p in problems:
        log(f"[parse] WARNING: {p}")
    niche = (opts.get("niche") or header["NICHE"]).upper()
    script_text = open(script, encoding="utf-8-sig").read()

    tmp = tempfile.mkdtemp(prefix="vtext_")
    try:
        prog("audio", 0.06)
        wav = util.extract_audio(video, os.path.join(tmp, "audio.wav"))

        prog("align", 0.10)
        aligned, coverage = align.align_script(
            wav, script_text, header.get("LANGUAGE", "en"), log=log)

        prog("cues", 0.30)
        cues.resolve_cues(events, aligned, log=log)
        _enforce_density(events, opts.get("density", "file"), log)

        prog("scenes", 0.34)
        cuts = scenes.detect_cuts(video, info["duration"], log=log)

        prog("zones", 0.38)
        zone_data = {}
        renderable = [e for e in events if "t_start" in e
                      and e["EVENT_TYPE"] != "BREATHING_MOMENT"]
        for i, ev in enumerate(renderable):
            t0 = ev["t_start"]
            t1 = ev.get("t_cue_end", t0 + 1.5) + 1.2
            zone_data[ev["num"]] = zones.analyze_event_window(
                video, t0, t1, tmp, f"{ev['num']:03d}")
            prog("zones", 0.38 + 0.12 * (i + 1) / max(1, len(renderable)))

        prog("style", 0.51)
        niche_cfg = styles.load_niche(niche, opts)
        plans = styles.build_plans(events, niche_cfg, cuts, H, zone_data,
                                   opts, duration=info["duration"], log=log)
        if not plans:
            raise RuntimeError("No events could be scheduled — see report "
                               "warnings (script/audio mismatch?).")

        prog("render", 0.54)
        render.layout_plans(plans, W, H)
        ovdir = os.path.join(tmp, "overlay")
        playlist = render.render_windows(
            plans, W, H, fps, info["duration"], ovdir,
            progress=lambda f: prog("render", 0.54 + 0.26 * f))

        prog("compose", 0.82)
        compose.compose(video, playlist, out, info["has_audio"],
                        info["duration"], crf=int(opts.get("crf", 18)),
                        preset=opts.get("preset", "medium"),
                        progress=lambda f: prog("compose", 0.82 + 0.16 * f))

        prog("qc", 0.99)
        report = qc.write_report(os.path.splitext(out)[0] + ".report.json",
                                 header, events, plans, coverage, problems,
                                 {"path": video, "width": W, "height": H,
                                  "fps": fps, "duration": info["duration"]})
        prog("done", 1.0)
        log(f"[done] {out} — {report['events_rendered']} texts rendered")
        return report
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _enforce_density(events, mode, log):
    if mode not in ("medium", "light"):
        return
    min_gap = 4.0 if mode == "medium" else 8.0
    keep_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    last_t = -1e9
    dropped = 0
    for ev in sorted([e for e in events if "t_start" in e],
                     key=lambda e: e["t_start"]):
        if ev["EVENT_TYPE"] == "BREATHING_MOMENT":
            continue
        if ev["t_start"] - last_t < min_gap and keep_rank[ev["INTENSITY"]] > 0:
            del ev["t_start"]
            ev["skip_reason"] = f"density cap ({mode})"
            dropped += 1
        else:
            last_t = ev["t_start"]
    if dropped:
        log(f"[density] dropped {dropped} events for '{mode}' cap")
