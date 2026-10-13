"""Which drives hold footage - by VOLUME IDENTITY, never by drive letter.

Letters move (a pendrive, another PC, a Google Drive remount: the new SSD came up as F: on 2026-10-12, and the backup
Drive could become F: tomorrow). Every footage volume is named by its label + serial (SSDs) or by the Google account in
its label (Drive mounts); the current letter is looked up each time.

    py -m libjobs.volumes      -> prints the current mapping and the footage roots
"""
import ctypes
import os
import string

# footage volumes: (id, how to recognise it, folders on it that hold the library)
FOOTAGE = [
    ("SSD",        dict(label="SSD", serial="FAD16543"),     [r"Movies"]),
    ("SSD 2",      dict(label="SSD 2", serial="6A882D55"),   []),   # user 2026-10-12: NOT indexed for now (later)
    ("Drive priyankamehta12350", dict(account="priyankamehta12350@gmail.com"),
     [r"My Drive\Movies DATA", r"My Drive\Movies DATA 2", r"My Drive\Movies DATA 3", r"My Drive\Cartoons DATA",
      r"My Drive\Season 7"]),
    ("Drive heypriyankamehta", dict(account="heypriyankamehta@gmail.com"), [r"My Drive\Movies", r"My Drive\Cartoons DATA"]),
]
# NEVER read, search or index (the user's rule): the backup Drive, the unused Drive, personal folders
NEVER_ACCOUNTS = ("shivamnischal1997@gmail.com", "shivnischal@gmail.com")
NEVER_FOLDERS = (r"SSD Data",)
CLOUD = ("Drive priyankamehta12350", "Drive heypriyankamehta")


def _info(letter):
    """-> (label, serial hex) of a mounted volume, or None."""
    root = f"{letter}:\\"
    if not os.path.isdir(root):
        return None
    name = ctypes.create_unicode_buffer(261)
    fs = ctypes.create_unicode_buffer(261)
    serial = ctypes.c_uint32()
    ok = ctypes.windll.kernel32.GetVolumeInformationW(root, name, 261, ctypes.byref(serial), None, None, fs, 261)
    if not ok:
        return None
    return name.value.strip(), f"{serial.value:08X}"


_CACHE = [0.0, {}]


def mapping(max_age=60):
    """-> {volume id: letter} for the footage volumes mounted now (a volume missing = not in the dict); cached 60 s."""
    import time
    if time.time() - _CACHE[0] < max_age and _CACHE[1]:
        return dict(_CACHE[1])
    out = {}
    for c in string.ascii_uppercase[2:]:                        # C: and later
        inf = _info(c)
        if not inf:
            continue
        label, serial = inf
        if any(a in label.lower() for a in NEVER_ACCOUNTS):
            continue
        for vid, how, _ in FOOTAGE:
            if "account" in how and how["account"] in label.lower():
                out.setdefault(vid, c)
            elif "serial" in how and serial == how["serial"].upper():
                out.setdefault(vid, c)
    _CACHE[:] = [time.time(), out]
    return dict(out)


def roots():
    """every existing footage root folder, with today's letters (SSDs first, then the Drive mounts)."""
    m, out = mapping(), []
    for vid, _, folders in FOOTAGE:
        if vid not in m:
            continue
        for f in folders:
            p = f"{m[vid]}:\\{f}"
            if os.path.isdir(p) and not any(n.lower() in p.lower() for n in NEVER_FOLDERS):
                out.append(p)
    return out


def volume_of(path):
    """-> the footage volume id a path lives on, or None (unknown / forbidden volume)."""
    if not path or len(path) < 2 or path[1] != ":":
        return None
    L = path[0].upper()
    return next((vid for vid, c in mapping().items() if c == L), None)


def is_cloud(path):
    """True if the path is on a Google Drive mount (reads can stall when the internet drops)."""
    return volume_of(path) in CLOUD


def allowed(path):
    """True only for paths on a footage volume and outside the never-touch folders."""
    return volume_of(path) is not None and not any(n.lower() in path.lower() for n in NEVER_FOLDERS)


if __name__ == "__main__":
    for c in string.ascii_uppercase[2:]:
        inf = _info(c)
        if inf:
            print(f"{c}:  label={inf[0]!r:45} serial={inf[1]}")
    print("footage volumes now:", mapping())
    print("roots:", *roots(), sep="\n  ")
