"""Find the movies drive by its VOLUME LABEL, not its letter.

Windows hands an external SSD/pen-drive whatever free letter is going that day —
it was E: yesterday, F: today, could be G: tomorrow whenever another drive is
plugged in first. catalog.json stores absolute paths, so a letter change would
break every library. The volume LABEL ("SSD") never changes, so we locate the
drive by label at runtime and remap any stored path onto wherever it lives now.
Set MOVIES_LABEL / MOVIES_MARKER once; everything else is automatic and relaxed.
"""
from __future__ import annotations

import os
import string

MOVIES_LABEL = "SSD"            # the movies SSD's volume label (stable)
MOVIES_MARKER = "Movies"        # the top folder that identifies it
_cache = {"drive": None}


def _label(drive: str) -> str:
    """Volume label of a drive letter, or '' — Windows only, never raises."""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(f"{drive}:\\"), buf, ctypes.sizeof(buf),
            None, None, None, None, 0)
        return buf.value if ok else ""
    except Exception:
        return ""


def movies_drive(refresh: bool = False) -> str:
    """The drive letter the movies SSD is mounted on right now (e.g. 'F'). Prefers
    the one whose label matches MOVIES_LABEL and holds a `Movies\\` folder; falls
    back to any drive that has `Movies\\`. Cached; pass refresh after a replug."""
    if not refresh and _cache["drive"] and os.path.isdir(f"{_cache['drive']}:\\{MOVIES_MARKER}"):
        return _cache["drive"]
    have = [d for d in string.ascii_uppercase
            if os.path.isdir(f"{d}:\\{MOVIES_MARKER}")]
    best = next((d for d in have if _label(d).strip().lower() == MOVIES_LABEL.lower()), "")
    drive = best or (have[0] if have else "")
    _cache["drive"] = drive
    return drive


def movies_root() -> str:
    """`<drive>:\\Movies` for the currently-mounted SSD, or '' if not found."""
    d = movies_drive()
    return f"{d}:\\{MOVIES_MARKER}" if d else ""


def resolve(path: str) -> str:
    """A usable path for a stored one, whatever letter the SSD is on now.

    If the stored path already exists, it's returned untouched. Otherwise, if it
    looks like `X:\\...`, the drive letter is swapped for the current movies drive
    and the remapped path is returned when it exists — so 'E:\\Movies\\...' keeps
    working after the SSD becomes F:, G:, anything.
    """
    if not path or os.path.exists(path):
        return path
    if len(path) >= 2 and path[1] == ":":
        d = movies_drive()
        if d:
            cand = d + path[1:]
            if os.path.exists(cand):
                return cand
    return path
