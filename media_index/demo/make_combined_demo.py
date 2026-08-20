"""Generate a season pack: several episodes muxed into ONE file.

This is the shape a lot of downloads arrive in — "S03_COMBINED.mkv", 3.5 GB,
every episode back to back. The tool must not mistake it for episode 1.

The file carries chapter markers (one per episode), which is how a timestamp
deep inside a seven-hour blob gets attributed to the right episode.

Run:  python -m media_index.demo.make_combined_demo out.mkv
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

from ..probe import require_ffmpeg
from . import make_demo_video as dv
from .make_demo_library import srt

EPISODE_SECONDS = dv.DURATION            # each episode is one demo video

# One dialogue set per episode. Deliberately includes a line repeated in two
# episodes — a recap, which is exactly what season packs are full of.
EPISODES = [
    ("E01 Pilot", [
        (5_000,   8_000,  "We start where nobody is watching."),
        (22_000,  25_000, "You said this town was finished."),
        (52_000,  54_500, "I never wanted the harvest."),
        (54_800,  57_500, "I wanted the land it grew on."),
        (85_000,  88_000, "Then we burn the field."),
        (106_000, 109_000, "Nobody walks out of Kessler County clean."),
    ]),
    ("E02 The Auction", [
        (5_000,   8_000,  "Previously: I never wanted the harvest."),   # recap!
        (22_000,  25_000, "The bank called again this morning."),
        (52_000,  55_000, "You brought a gun to a courthouse."),
        (63_000,  66_000, "I brought an argument."),
        (95_000,  98_000, "A debt is not a rope. It is a road."),
    ]),
    ("E03 Frost Line", [
        (5_000,   8_000,  "Three winters, and the ground still will not take."),
        (31_000,  34_500, "You came back for the land."),
        (52_000,  55_000, "I came back for the people on it."),
        (74_000,  77_000, "Sign it, and the valley is yours."),
        (106_000, 109_000, "The cold does not negotiate."),
    ]),
]


def episode_bounds() -> list[tuple]:
    """[(title, start_s, end_s)] — the ground truth for the tests."""
    return [(t, i * EPISODE_SECONDS, (i + 1) * EPISODE_SECONDS)
            for i, (t, _) in enumerate(EPISODES)]


def combined_cues() -> list[tuple]:
    """Every episode's cues, shifted onto the combined timeline."""
    out = []
    for i, (_, cues) in enumerate(EPISODES):
        shift = int(i * EPISODE_SECONDS * 1000)
        out += [(a + shift, b + shift, t) for a, b, t in cues]
    return out


def _metadata_file(path: str) -> str:
    lines = [";FFMETADATA1"]
    for title, start, end in episode_bounds():
        lines += ["[CHAPTER]", "TIMEBASE=1/1000",
                  f"START={int(start * 1000)}", f"END={int(end * 1000)}",
                  f"title={title}"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def build(out_path: str, with_chapters=True, write_srt=True,
          log=print) -> str:
    """Render the pack (+ one .srt covering the whole file)."""
    tmp = tempfile.mkdtemp(prefix="combined_")
    parts = []
    try:
        for i, (title, cues) in enumerate(EPISODES):
            p = os.path.join(tmp, f"ep{i}.mkv")
            dv.build(p, cues=cues, write_srt=False, log=lambda *a: None)
            parts.append(p)
            log(f"  rendered {title}")

        listing = os.path.join(tmp, "list.txt")
        with open(listing, "w", encoding="utf-8") as f:
            for p in parts:
                f.write(f"file '{p}'\n")

        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".",
                    exist_ok=True)
        cmd = [require_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
               "-f", "concat", "-safe", "0", "-i", listing]
        if with_chapters:
            cmd += ["-i", _metadata_file(os.path.join(tmp, "meta.txt")),
                    "-map_metadata", "1"]
        cmd += ["-c", "copy", out_path]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if r.returncode != 0 or not os.path.exists(out_path):
            raise RuntimeError(f"ffmpeg concat failed: {r.stderr[-600:]}")

        if write_srt:
            sub = os.path.splitext(out_path)[0] + ".srt"
            with open(sub, "w", encoding="utf-8") as f:
                f.write(srt(combined_cues()))
            log(f"  wrote {os.path.basename(sub)} "
                f"({len(combined_cues())} cues across the whole file)")
        return out_path
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "Iron_Harvest_S01_COMBINED.mkv"
    build(out)
    print("episode bounds:", episode_bounds())
