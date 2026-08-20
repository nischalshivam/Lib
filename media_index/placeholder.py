"""The card that says a moment is missing, instead of hiding that it is.

An unresolved beat used to be covered by holding a neighbouring clip across
it. That is the single worst thing this tool did, because it is invisible:
the video plays, nothing is obviously broken, and somebody else's footage
sits under a sentence it has nothing to do with. Four scenes of one real
build went that way and every one of them was visible on screen — just not
labelled as a fault.

A card is the opposite. It cannot be missed, it holds the exact duration the
narration needs, and it says what was wanted and why it was not found. It
turns "somewhere in this video there is a wrong clip" into "here are the six
places that need you".

Not a black frame, either. A black frame in a preview looks like a render
bug, and an editor scrubbing past it at speed will skip it.

## How it is drawn

ffmpeg's `drawtext`, and nothing else — no Pillow, no font bundling, no new
dependency for a package whose whole point is that it installs from a zip.

Text comes from a FILE rather than the filter string. `drawtext=text=...`
requires escaping colons, quotes, backslashes and percent signs, and the
text here is full of timecodes and quoted narration. `textfile=` has no
escaping rules at all, and it renders newlines properly, which is what a
card is.
"""
from __future__ import annotations

import os
import subprocess
import tempfile

from .probe import ProbeError, require_ffmpeg

WIDTH, HEIGHT = 1920, 1080
BACKGROUND = "0x14161a"
# Amber, not red. Red reads as "this broke"; this is ordinary work that the
# tool is handing over deliberately.
INK = "0xf0b429"
BODY = "0xdfe3e8"

# Fonts that exist on a machine that has not been prepared. Windows first,
# because that is where this runs.
FONTS = (
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/consola.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)


def font_file() -> str:
    for path in FONTS:
        if os.path.isfile(path):
            return path
    return ""


_HAS_TEXT = None


def can_write_text() -> bool:
    """Does this ffmpeg have `drawtext`?

    Not every build does. It needs libfreetype, and the minimal builds some
    package managers ship leave it out — including the one this was first
    tested on, which failed with "No such filter: 'drawtext'". The Windows
    builds people actually download (gyan.dev, BtbN) all have it.

    Asked once and remembered, because the answer cannot change while the
    program is running and a build makes one of these per unresolved beat.
    """
    global _HAS_TEXT
    if _HAS_TEXT is None:
        try:
            got = subprocess.run([require_ffmpeg(), "-v", "quiet",
                                  "-filters"], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=30)
            _HAS_TEXT = b" drawtext " in (got.stdout or b"")
        except (OSError, subprocess.SubprocessError, ProbeError):
            _HAS_TEXT = False
    return _HAS_TEXT


def _stripes() -> str:
    """A card nobody could mistake for footage, without any text at all.

    The fallback when `drawtext` is missing. It carries none of the detail,
    and it does not need to: the manifest and the editor hold every word.
    What it has to do is be impossible to scroll past, and diagonal hazard
    bars across a 1920x1080 frame are that.
    """
    bars = [f"drawbox=x={x}:y=0:w=90:h={HEIGHT}:color={INK}@0.85:t=fill"
            for x in range(-HEIGHT, WIDTH + HEIGHT, 260)]
    return ",".join(bars)


def _escape(path: str) -> str:
    """A path as ffmpeg's filter parser wants to read it.

    Windows paths carry a colon after the drive letter, and a colon
    separates filter options. Without this every card on Windows fails with
    a filter syntax error and the build loses its placeholders — which is
    exactly the material a person needs.
    """
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def lines_for(request: dict) -> list:
    """What the card says, in the order somebody reads it."""
    out = ["NEEDS VISUAL", ""]
    where = request.get("scene")
    if where:
        out.append(f"Scene {where}   ·   {request.get('seconds', 0):.1f} sec")
    narration = (request.get("narration") or "").strip()
    if narration:
        out.append("")
        out.append(_wrap(narration, 52))
    episode = (request.get("episode") or "").strip()
    if episode:
        out.append("")
        out.append(f"Chahiye tha:  {episode}")
    why = (request.get("why") or "").strip()
    if why:
        out.append(f"Nahi mila:    {why}")
    people = request.get("must_show") or []
    if people:
        out.append(f"Dikhna tha:   {', '.join(people)}")
    options = int(request.get("options") or 0)
    if options:
        out.append("")
        out.append(f"{options} option editor me maujood hain")
    return out


def _wrap(text: str, width: int) -> str:
    words, line, out = text.split(), "", []
    for word in words:
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return "\n".join(out[:4])          # a card, not an essay


def card(out_path: str, request: dict) -> str:
    """Write the PNG. Never raises — a missing card must not stop a build.

    Returns the path written, or "" if it could not be drawn. A build with
    no card is worse than one with a card, but far better than one that
    stopped, so this fails quietly and the caller falls back.
    """
    ff = require_ffmpeg()
    font = font_file()
    text = "\n".join(lines_for(request))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".",
                exist_ok=True)

    handle, txt = tempfile.mkstemp(suffix=".txt", text=True)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as f:
            f.write(text)
        chain = [
            # A border, so a card is unmistakable even in a thumbnail strip.
            f"drawbox=x=0:y=0:w={WIDTH}:h={HEIGHT}:color={INK}@0.9:t=10",
        ]
        if font and can_write_text():
            chain.append(
                f"drawtext=fontfile='{_escape(font)}':"
                f"textfile='{_escape(txt)}':"
                f"fontcolor={BODY}:fontsize=38:line_spacing=16:"
                "x=(w-text_w)/2:y=(h-text_h)/2")
        else:
            chain.append(_stripes())
        cmd = [ff, "-y", "-v", "error", "-f", "lavfi",
               "-i", f"color=c={BACKGROUND}:s={WIDTH}x{HEIGHT}",
               "-vf", ",".join(chain), "-frames:v", "1", out_path]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=60, check=True)
        except (OSError, subprocess.SubprocessError):
            return ""
    finally:
        try:
            os.remove(txt)
        except OSError:
            pass
    return out_path if os.path.isfile(out_path) else ""


def available() -> tuple:
    """(can a card be drawn, why not). Checked before a build, not during."""
    try:
        require_ffmpeg()
    except ProbeError as exc:
        return False, str(exc)
    if not font_file():
        return False, ("koi font nahi mila — card bina likhe banega "
                       "(patti wala). Windows par ye kabhi nahi hona "
                       "chahiye.")
    if not can_write_text():
        return False, ("is ffmpeg me drawtext nahi hai — card bina likhe "
                       "banega. Poora text manifest aur editor me rahega.")
    return True, ""
