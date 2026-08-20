"""Every still a job produced, on one page.

A twenty-minute essay needs well over a hundred images. Opening a hundred
folders to judge them is not review, it is the manual checking this tool was
built to end — and it is why bad footage kept reaching finished videos: nobody
looks at a hundred files one at a time, so nobody looked at all.

A contact sheet turns that into one glance. Wrong scenes, repeated frames,
black frames and motion blur are all obvious side by side and nearly invisible
one at a time.

Built with ffmpeg's `tile` and nothing else. `xstack` and `drawtext` would give
neater pages with captions burned into each cell, and both are missing from
minimal ffmpeg builds — including the one this was written on. Since a review
tool that fails to run is worth less than a plain one that always runs, the
captions live in a text file beside the sheet instead, numbered in the same
order as the tiles.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

from .probe import ProbeError, require_ffmpeg

TILE_WIDTH = 320
TILE_HEIGHT = 180
# tile builds the whole grid in memory, so a very tall sheet becomes pages.
MAX_ROWS = 12
IMAGE_EXT = (".jpg", ".jpeg", ".png")


def collect(folder: str) -> list:
    """[(scene, path)] of every still under a job output folder, in order."""
    out = []
    if not os.path.isdir(folder):
        return out
    for scene in sorted(os.listdir(folder)):
        scene_dir = os.path.join(folder, scene)
        if not (scene.startswith("scene_") and os.path.isdir(scene_dir)):
            continue
        for name in sorted(os.listdir(scene_dir)):
            if name.lower().endswith(IMAGE_EXT):
                out.append((scene, os.path.join(scene_dir, name)))
    return out


def _render(paths: list, out: str, columns: int, rows: int) -> None:
    """Tile a list of images into one page.

    `tile` reads successive frames of a single stream, so the images are first
    staged as a numbered sequence. Every cell is forced to the same size and
    letterboxed rather than stretched — a squashed frame is hard to judge.
    """
    staging = tempfile.mkdtemp(prefix="mi_sheet_")
    try:
        for i, path in enumerate(paths, 1):
            ext = os.path.splitext(path)[1].lower()
            shutil.copyfile(path, os.path.join(staging, f"{i:05d}{ext}"))
        pattern = os.path.join(staging, "%05d" +
                               os.path.splitext(paths[0])[1].lower())
        vf = (f"scale={TILE_WIDTH}:{TILE_HEIGHT}:"
              "force_original_aspect_ratio=decrease,"
              f"pad={TILE_WIDTH}:{TILE_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,"
              f"setsar=1,tile={columns}x{rows}:padding=2:color=0x202020")
        cmd = [require_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
               "-i", pattern, "-vf", vf, "-frames:v", "1", out]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           errors="replace")
        if r.returncode != 0 or not os.path.exists(out):
            raise ProbeError(f"contact sheet failed: {(r.stderr or '')[-300:]}")
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def build(folder: str, out: str, columns: int = 8,
          log=lambda *a: None) -> str | None:
    """Write the sheet (or pages) plus its index. Returns the first page."""
    shots = collect(folder)
    if not shots:
        return None

    per_page = columns * MAX_ROWS
    pages = [shots[i:i + per_page] for i in range(0, len(shots), per_page)]
    stem, ext = os.path.splitext(out)
    ext = ext or ".jpg"

    written, lines = [], []
    for page_no, page in enumerate(pages, 1):
        rows = (len(page) + columns - 1) // columns
        target = out if len(pages) == 1 else f"{stem}_{page_no}{ext}"
        _render([p for _s, p in page], target, columns, rows)
        written.append(target)
        log(f"  page {page_no}: {len(page)} image(s), {columns}x{rows}")
        for i, (scene, path) in enumerate(page, 1):
            col, row = (i - 1) % columns + 1, (i - 1) // columns + 1
            lines.append(f"page {page_no}  row {row:>2} col {col:>2}   "
                         f"{scene}/{os.path.basename(path)}")

    index = stem + "_index.txt"
    with open(index, "w", encoding="utf-8") as f:
        f.write("Tiles run left to right, top to bottom.\n\n")
        f.write("\n".join(lines) + "\n")
    log(f"  {index}")
    return written[0]
