#!/usr/bin/env python3
"""VText CLI — script-driven text overlays for finished videos.

Single video:
  python vtext.py --video final.mp4 --script clean.txt \
      --instructions overlay.txt --out final_texted.mp4 [--niche MOVIE_ESSAY]

Queue (JSON list of jobs with the same keys):
  python vtext.py --queue jobs.json
"""
import argparse
import json
import sys

from engine.pipeline import run_job

STAGES = {"probe": "Probing video", "parse": "Reading instruction file",
          "audio": "Extracting audio", "align": "Aligning script to audio",
          "cues": "Resolving text moments", "scenes": "Detecting shot cuts",
          "zones": "Analyzing frames", "style": "Planning typography",
          "render": "Rendering text", "compose": "Compositing video",
          "qc": "Quality check", "done": "Done"}


def _progress(stage, frac):
    bar = "#" * int(frac * 30)
    sys.stdout.write(f"\r[{bar:<30}] {frac * 100:5.1f}%  "
                     f"{STAGES.get(stage, stage):<28}")
    sys.stdout.flush()
    if stage == "done":
        print()


def _opts(a):
    o = {"density": a.density, "text_scale": a.text_scale,
         "cut_protect": not a.no_cut_protect, "crf": a.crf,
         "preset": a.preset}
    if a.niche:
        o["niche"] = a.niche
    if a.pack:
        o["pack"] = a.pack
    if a.energy:
        o["energy"] = a.energy
    if a.accent:
        o["accent"] = tuple(int(a.accent.lstrip("#")[i:i + 2], 16)
                            for i in (0, 2, 4))
    return o


def main():
    ap = argparse.ArgumentParser(description="VText — final-touch text overlays")
    ap.add_argument("--video")
    ap.add_argument("--script")
    ap.add_argument("--instructions")
    ap.add_argument("--out")
    ap.add_argument("--queue", help="JSON file with a list of jobs")
    ap.add_argument("--niche", help="override instruction-file niche")
    ap.add_argument("--pack", help="typography pack override")
    ap.add_argument("--accent", help="accent color hex, e.g. #FFD60A")
    ap.add_argument("--energy", type=float, help="motion energy 1-5")
    ap.add_argument("--text-scale", default="auto",
                    choices=["small", "balanced", "large", "auto"])
    ap.add_argument("--density", default="file",
                    choices=["file", "medium", "light"])
    ap.add_argument("--no-cut-protect", action="store_true")
    ap.add_argument("--crf", type=int, default=18)
    ap.add_argument("--preset", default="medium")
    a = ap.parse_args()

    if a.queue:
        jobs = json.load(open(a.queue))
        for i, j in enumerate(jobs, 1):
            print(f"\n=== Job {i}/{len(jobs)}: {j['video']} ===")
            try:
                run_job(j["video"], j["script"], j["instructions"], j["out"],
                        opts={**_opts(a), **j.get("opts", {})},
                        progress=_progress)
            except Exception as e:
                print(f"\nJob failed: {e}", file=sys.stderr)
        return
    if not all([a.video, a.script, a.instructions, a.out]):
        ap.error("--video, --script, --instructions and --out are required "
                 "(or use --queue)")
    run_job(a.video, a.script, a.instructions, a.out, opts=_opts(a),
            progress=_progress)


if __name__ == "__main__":
    main()
