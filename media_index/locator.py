"""Where does every show / film live? — the tool's map of the user's drives.

Footage is spread over several drives: the external SSD (E:\\Movies) and the
Google-Drive mounts (G:, H:, I:, J: ...\\My Drive\\Movies DATA). A build that only
looks in one of them silently gets NO footage for a show kept elsewhere and then
fills it with other shows' clips. This module:

  * DISCOVERS library roots — folders named like Movies / Films / Series / Shows /
    TV (at most two levels below a drive) that actually hold `*.catalog.json`;
  * REMEMBERS them in `library_roots.json` (plus any the user adds by hand), so
    discovery is a one-time cost;
  * LOCATES a script: every show + episode it pins is checked against the
    catalogues in ALL roots — and for anything missing it says whether the show
    is on disk but not indexed yet, or not on any drive at all.

The gate (libcheck) and the build (studio -> makevideo) use exactly these roots,
so "ready" in the UI means ready in the render.
"""
from __future__ import annotations

import json
import os
import re
import string
import time

from . import libcheck

REGISTRY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "library_roots.json")
_ROOT_NAME = re.compile(r"movie|film|series|\bshows?\b|sitcom|\btv\b|cinema", re.I)
_SKIP = {"windows", "program files", "program files (x86)", "programdata",
         "$recycle.bin", "system volume information", "appdata", "recovery",
         "perflogs", "msocache", "config.msi", "$windows.~ws", "$winreagent", "ssd data"}
_VID = (".mkv", ".mp4", ".avi", ".m4v", ".mov", ".webm")


# --------------------------------------------------------------------------- #
#  helpers                                                                     #
# --------------------------------------------------------------------------- #
# Footage ONLY lives on these drives (the user's rule): the SSD + the Google-Drive
# mounts that hold real libraries. Anything else (e.g. a G: backup copy of BB/GOT)
# is never searched, so no duplicates and no slow cloud copies. Change in
# library_roots.json -> "drives", or env MI_DRIVES="E,I,J".
DEFAULT_DRIVES = ["E", "I", "J"]
_last_rediscover = 0.0


def _vol_label(letter: str) -> str:
    from .paths import _label                                # noqa: PLC0415
    return _label(letter)


def _letter_for_label(label: str) -> str:
    for c in string.ascii_uppercase:
        if os.path.isdir(f"{c}:\\") and _vol_label(c) == label:
            return c
    return ""


def _relocate(path: str, labels: dict) -> str:
    """A stored root on a drive whose LETTER changed (Google Drive remounted
    I: as K:) -> the same folder on the drive that now carries that label."""
    if not path or len(path) < 2 or path[1] != ":":
        return path
    L = path[0].upper()
    lab = labels.get(L, "")
    if lab and _vol_label(L) != lab:
        X = _letter_for_label(lab)
        if X:
            return X + path[1:]
    return path


def allowed_drives() -> list:
    """The drive letters footage may live on — remembered by VOLUME LABEL too
    (Google Drive's label carries the account e-mail), so a remount under a
    new letter is followed automatically."""
    try:                                   # identity-based (label/serial/account) - letters move between PCs
        import sys as _s
        _s.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from libjobs import volumes as _v
        m = _v.mapping()
        if m:
            return sorted(set(m.values()))
    except Exception:
        pass
    env = os.environ.get("MI_DRIVES")
    if env:
        return [d.strip().upper()[:1] for d in env.split(",") if d.strip()]
    reg = _load()
    letters = [d.upper()[:1] for d in (reg.get("drives") or DEFAULT_DRIVES)]
    labels = reg.get("drive_labels") or {}
    out = []
    for L in letters:
        lab = labels.get(L, "")
        if lab and _vol_label(L) != lab:
            L = _letter_for_label(lab) or L
        out.append(L)
    return out


def _on_allowed_drive(path: str) -> bool:
    return bool(path) and path[:1].upper() in allowed_drives()


def _drives() -> list:
    return [f"{c}:\\" for c in allowed_drives() if os.path.isdir(f"{c}:\\")]


def _dirs(p: str) -> list:
    try:
        return [e.path for e in os.scandir(p)
                if e.is_dir(follow_symlinks=False)
                and not e.name.startswith((".", "$"))
                and e.name.lower() not in _SKIP]
    except OSError:
        return []


def _has(path: str, exts: tuple, depth: int) -> bool:
    """Is there any file ending in `exts` within `depth` levels? (early exit)"""
    try:
        with os.scandir(path) as it:
            sub = []
            for e in it:
                if e.is_file(follow_symlinks=False) and e.name.lower().endswith(exts):
                    return True
                if e.is_dir(follow_symlinks=False) and not e.name.startswith((".", "$")):
                    sub.append(e.path)
    except OSError:
        return False
    return depth > 0 and any(_has(s, exts, depth - 1) for s in sub)


def _key(name: str) -> str:
    """Comparable show key: no bracketed year, no leading 'the', no punctuation."""
    from .subtitles import show_key                      # noqa: PLC0415
    return show_key(name)


# --------------------------------------------------------------------------- #
#  registry                                                                    #
# --------------------------------------------------------------------------- #
def _load() -> dict:
    for i in range(20):                    # another build may be replacing it this very moment (Windows: WinError 32)
        try:
            with open(REGISTRY, encoding="utf-8") as f:
                d = json.load(f)
                return d if isinstance(d, dict) else {}
        except PermissionError:
            time.sleep(0.1 + 0.05 * i)
        except (OSError, ValueError):
            return {}
    return {}


def _save(d: dict) -> None:
    """Own temp file + retried replace: three Movie Editor builds that start together each rediscover and save
    (2026-10-14: the shared '.tmp' name + a reader holding the file crashed one build with WinError 32). The registry
    is only a cache, so a save that still cannot land is skipped, never fatal."""
    import threading
    tmp = f"{REGISTRY}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
        for i in range(20):
            try:
                os.replace(tmp, REGISTRY)
                return
            except PermissionError:
                time.sleep(0.1 + 0.05 * i)
    except OSError:
        pass
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def discover(log=lambda *a: None) -> list:
    """Scan every drive (two levels deep, by folder NAME) for library roots that
    hold catalogues. Saves and returns them. A few seconds, even on Google Drive."""
    found = []
    for d in _drives():
        for c1 in _dirs(d):
            cands = [c1] + _dirs(c1)
            for c in cands:
                if _ROOT_NAME.search(os.path.basename(c)) and \
                        _has(c, (".catalog.json",), 5):
                    found.append(os.path.normpath(c))
    # keep the OUTERMOST root when one sits inside another
    found = sorted(set(found), key=len)
    roots = [r for i, r in enumerate(found)
             if not any(r.lower().startswith(o.lower() + os.sep) for o in found[:i])]
    reg = _load()
    reg.setdefault("drives", list(DEFAULT_DRIVES))
    labels = reg.setdefault("drive_labels", {})
    for L in reg["drives"]:                       # remember each drive's label once
        if L not in labels and os.path.isdir(f"{L}:\\"):
            lab = _vol_label(L)
            if lab:
                labels[L] = lab
    reg["discovered"] = roots
    reg["discovered_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save(reg)
    log(f"  library roots found: {', '.join(roots) or '(none)'}")
    return roots


def roots(refresh: bool = False) -> list:
    """Every library root to search: the user's own + the discovered ones,
    existing only. Discovers once on first use."""
    reg = _load()
    if refresh or "discovered" not in reg:
        discover()
        reg = _load()
    labels = reg.get("drive_labels") or {}
    manual = [_relocate(r, labels) for r in reg.get("manual", [])]
    found = [_relocate(r, labels) for r in reg.get("discovered", [])]
    global _last_rediscover
    if not refresh and any(not os.path.isdir(r) for r in found) \
            and time.time() - _last_rediscover > 60:
        _last_rediscover = time.time()
        discover()                                # a root vanished: look again once
        reg = _load()
        found = [_relocate(r, labels) for r in reg.get("discovered", [])]
    out = []
    for r in manual + [r for r in found if _on_allowed_drive(r)]:
        if r and os.path.isdir(r) and r.lower() not in (x.lower() for x in out):
            out.append(r)
    return out


def add_root(path: str) -> list:
    """Remember a folder the user points at. If they pick one SHOW's folder,
    its parent (the folder of shows) is stored, so siblings are found too."""
    path = os.path.normpath(path or "")
    if not os.path.isdir(path):
        return roots()
    kids_with_cats = sum(1 for c in _dirs(path) if _has(c, (".catalog.json",), 5))
    if kids_with_cats == 0 and _has(path, (".catalog.json",) + _VID, 5):
        path = os.path.dirname(path)                 # a show folder -> its parent
    reg = _load()
    man = [m for m in reg.get("manual", []) if m.lower() != path.lower()]
    reg["manual"] = [path] + man
    _save(reg)
    libcheck.clear_cache()
    return roots()


def roots_str() -> str:
    return ";".join(roots())


# --------------------------------------------------------------------------- #
#  where is ONE show? / what does a script need?                               #
# --------------------------------------------------------------------------- #
def show_folders(root_list=None) -> dict:
    """{show_key: full folder path} for every show folder in every root."""
    out = {}
    for r in root_list or roots():
        for c in _dirs(r):
            out.setdefault(_key(os.path.basename(c)), c)
    return out


def find_on_disk(show: str) -> list:
    """Folders anywhere (3 levels under each drive) named like `show` — to tell
    'on disk but not indexed' apart from 'not on this PC at all'."""
    want = _key(show)
    if not want:
        return []
    hits = []
    for d in _drives():
        lvl = [d]
        for _ in range(3):
            nxt = []
            for p in lvl:
                for c in _dirs(p):
                    k = _key(os.path.basename(c))
                    if k == want or (k.startswith(want) and len(k) - len(want) <= 30
                                     and re.match(r"\d{4}|s\d|season|complete",
                                                  k[len(want):] or "x")):
                        hits.append(c)
                    else:
                        nxt.append(c)
            lvl = nxt
    return hits


def locate(clue_path: str) -> dict:
    """The import-time check for ONE video. Returns
    {ready, roots, shows:[{show, status, folder, present, missing, on_disk}], summary}
    status: ok | missing_episodes | not_indexed | not_found | fill."""
    rl = roots()
    res = libcheck.check(clue_path, ";".join(rl))
    folders = show_folders(rl)
    shows = []
    for s in res.get("shows", []):
        name = s.get("show", "")
        folder = folders.get(_key(s.get("matched_folder") or name), "")
        if s.get("fill"):
            status = "fill"
        elif s.get("not_indexed"):
            status = "not_indexed"
        elif s.get("missing") or s.get("incomplete"):
            status = "missing_episodes"
        else:
            status = "ok"
        on_disk = []
        if status == "not_indexed":
            on_disk = find_on_disk(name)
            status = "not_indexed" if on_disk else "not_found"
        shows.append({"show": name, "status": status, "folder": folder,
                      "present": len(s.get("present") or []),
                      "needed": len(s.get("needed") or []),
                      "missing": (s.get("missing") or []) + (s.get("incomplete") or []),
                      "on_disk": on_disk})
    bad = [x for x in shows if x["status"] in ("missing_episodes", "not_indexed", "not_found")]
    if not bad:
        summary = "Library OK - " + ", ".join(
            f"{x['show'].title()} ({x['present']}/{x['needed']})" for x in shows
            if x["status"] == "ok")
    else:
        parts = []
        for x in bad:
            if x["status"] == "not_found":
                parts.append(f"{x['show'].title()}: NOT on any drive")
            elif x["status"] == "not_indexed":
                parts.append(f"{x['show'].title()}: on disk but NOT indexed ({x['on_disk'][0]})")
            else:
                parts.append(f"{x['show'].title()}: missing {', '.join(x['missing'][:6])}")
        summary = " | ".join(parts)
    return {"ready": not bad, "roots": rl, "shows": shows, "summary": summary}
