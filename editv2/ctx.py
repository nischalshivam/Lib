"""Per-video context (ctx.json): which shows/films the script uses, where their
footage and Cast folders are, their typography, the language and the look."""
from __future__ import annotations

import glob, json, os, re, sys

sys.path.insert(0, r"D:\Lib")
from media_index import locator  # noqa: E402

from .common import llm  # noqa: E402
from . import styles  # noqa: E402

_ROOTS = None


def _clean_name(folder):
    n = os.path.basename(folder)
    n = re.sub(r"\[.*?\]|\(.*?\)", "", n)
    n = re.sub(r"\b(19|20)\d\d\b.*$", "", n)
    return re.sub(r"[._]+", " ", n).strip(" -")


def _cast_dir(root):
    for dp, dn, fn in os.walk(root):
        if dp[len(root):].count(os.sep) > 3:
            dn[:] = []
            continue
        for d in dn:
            if d.lower() == "cast":
                return os.path.join(dp, d)
    return ""


def _has_catalogs(root):
    for dp, dn, fn in os.walk(root):
        if any(f.endswith(".catalog.json") for f in fn):
            return True
    return False


def detect_shows(text: str, folders: dict, log=print) -> list:
    """agy Claude reads the script and names the shows/films it actually uses."""
    names = sorted({f"{k} = {_clean_name(p)}" for k, p in folders.items()})
    res, _ = llm("You map a video-essay script to the TV shows / films it discusses. Choose ONLY from the given "
                 "library list. Reply ONLY JSON {\"shows\":[{\"key\":\"<library key>\",\"name\":\"<official title>\"}]}",
                 "LIBRARY (key = folder name):\n" + "\n".join(names) + "\n\nSCRIPT (start):\n" + text[:6000])
    out = [s for s in res.get("shows", []) if s.get("key") in folders]
    log(f"  shows in script: {[s['name'] for s in out]}")
    return out


def build(clean: str, build_dir: str, language: str = "en", look: str = "auto", shows=None, log=print) -> dict:
    os.makedirs(build_dir, exist_ok=True)
    p = os.path.join(build_dir, "ctx.json")
    if os.path.exists(p):
        ctx = json.load(open(p, encoding="utf-8"))
        if ctx.get("clean") == os.path.abspath(clean):
            return ctx
    text = open(clean, encoding="utf-8-sig").read()
    global _ROOTS
    if _ROOTS is None:                     # new Drive folders (Movies DATA 3 ...) appear over time: look again once per run
        _ROOTS = locator.roots(refresh=True)
    folders = locator.show_folders(_ROOTS)
    picked = shows or detect_shows(text, folders, log)
    S = []
    for s in picked:
        root = folders[s["key"]]
        cast_dir = _cast_dir(root)
        cast = sorted(d for d in os.listdir(cast_dir) if os.path.isdir(os.path.join(cast_dir, d))) if cast_dir else []
        st = styles.style(s["key"], s["name"], log)
        S.append(dict(key=s["key"], name=s["name"], root=root, cast_dir=cast_dir, cast=cast,
                      first_names=sorted({c.split()[0] for c in cast if c}), indexed=_has_catalogs(root), style=st))
        if not S[-1]["indexed"]:
            log(f"  ! {s['name']} has NO catalogs yet — its moments cannot be grounded")
    ctx = dict(clean=os.path.abspath(clean), build=os.path.abspath(build_dir), language=language, look=look, shows=S)
    json.dump(ctx, open(p, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    return ctx
