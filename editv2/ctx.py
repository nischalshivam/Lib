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


CHANNELS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "channels.json")
_STOP = {"the", "and", "mr", "mr.", "mrs", "mrs.", "ms", "dr", "dr.", "jr", "jr.", "sr", "officer", "detective",
         "sergeant", "lieutenant", "captain", "major", "agent", "doctor", "uncle", "aunt", "little", "big", "old", "young",
         "king", "queen", "lord", "lady", "sir", "father", "mother", "brother", "sister", "deputy", "sheriff", "judge"}


def channel_of(clean):
    """(channel folder, allowed show keys) for D:\\Movies\\English\\<channel>\\<topic>\\script.txt from channels.json:
    a list, '*' (cross-franchise) or None (channel not listed)."""
    try:
        m = {k: v for k, v in json.load(open(CHANNELS, encoding="utf-8")).items() if not k.startswith("_")}
    except Exception:
        return None, None
    low = {k.lower(): k for k in m}
    for part in reversed(os.path.normpath(os.path.abspath(clean)).split(os.sep)[:-1]):
        if part.lower() in low:
            k = low[part.lower()]
            return k, m[k]
    return None, None


def _cast_names(root):
    """character names of a show: its Cast folder (also 'Cast\\<Show> Cast\\<names>'), else what the indexer uses
    (Desktop\\Cast, the cached film cast) for one of its videos."""
    cd = _cast_dir(root)
    if cd:
        subs = [x for x in os.listdir(cd) if os.path.isdir(os.path.join(cd, x)) and not x.startswith(("_", "."))]
        if len(subs) == 1 or (subs and all(x.lower().endswith(" cast") for x in subs)):
            inner = os.path.join(cd, subs[0])
            deeper = [x for x in os.listdir(inner) if os.path.isdir(os.path.join(inner, x)) and not x.startswith(("_", "."))]
            if len(deeper) >= 3:
                subs = deeper
        if len(subs) >= 3:
            return sorted(subs)
    try:
        from libjobs import common as LC
        for dp, dn, fn in os.walk(root):
            v = next((f for f in fn if f.lower().endswith((".mkv", ".mp4", ".avi", ".m4v"))), None)
            if v:
                return LC.cast_of(os.path.join(dp, v), allow_llm=True)    # a film: cached forever after one ask
    except Exception:
        pass
    return []


def evidence(text, name, cast, also=()):
    """What in the script itself names this show: its title (with or without a leading 'The', or its library folder
    name) and/or its Cast characters (full name or a distinctive first/last name, case-sensitive, whole words)."""
    def has(t):
        return re.search(r"(?<![\w'])" + re.escape(t) + r"(?![\w])", text) is not None
    flat = " " + re.sub(r"[^a-z0-9]+", " ", text.lower()) + " "

    def has_title(n):
        w = re.sub(r"[^A-Za-z0-9]+", " ", n).split()
        if not w:
            return False
        if len(w) <= 2:                               # 'Saw', 'Get Out', 'Alien': case-sensitive, never the verb 'saw'
            return re.search(r"(?<![\w'])" + r"[^A-Za-z0-9]+".join(map(re.escape, w)) + r"(?![\w])", text) is not None
        return " " + " ".join(w).lower() + " " in flat
    names = set()
    for n in [name, *also]:
        n = (n or "").strip()
        if len(n) < 3:
            continue
        names.add(n)
        w = re.sub(r"[^A-Za-z0-9]+", " ", n).split()
        if w and w[0].lower() == "the" and len(w) >= 3:
            names.add(" ".join(w[1:]))                # 'the Hunger Games' / 'Hunger Games'
        core = [x for x in w if x.lower() not in ("the", "a", "an")]
        if len(core) >= 4 and (len(core[0]) >= 5 or any(c.isdigit() for c in core[1])):
            names.add(" ".join(core[:2]) + " ~")      # 'Terminator 2 Judgment Day' -> 'Terminator 2', 'Mad Max'
    title = any(has_title(n[:-2]) if n.endswith(" ~") else has_title(n) for n in names)
    chars = []
    for c in cast:
        toks = [c] + [t for t in re.split(r"[\s\-]+", c) if len(t) >= 3 and t[:1].isupper() and t.lower() not in _STOP]
        if any(has(t) for t in toks):
            chars.append(c)
    return title, chars


def guard_shows(clean, text, picked, folders, log=print):
    """The script detector (an LLM) once put Breaking Bad + Sopranos clips into a Malcolm video. Two locks:
    1) the channel's own shows only (channels.json), 2) cross-franchise / unlisted channels: a show stays only if the
    script names it (its title or >= 2 of its characters). Nothing confirmed -> stop, never guess."""
    ch, allow = channel_of(clean)
    if isinstance(allow, list):
        keep = [s for s in picked if s["key"] in allow]
        drop = [s["name"] for s in picked if s["key"] not in allow]
        if drop:
            log(f"  ! channel '{ch}' may only use {allow} - dropped {drop} (picked by the script detector)")
        if not keep:
            keep = [dict(key=k, name=_clean_name(folders[k])) for k in allow if k in folders]
            log(f"  channel '{ch}': using its own show(s) {[s['name'] for s in keep]}")
        if not keep:
            raise RuntimeError(f"channel '{ch}' lists {allow} in editv2/channels.json but none of them is in the library")
        return keep
    if ch is None:
        log("  ! this script's channel folder is not in editv2/channels.json - shows are confirmed from the script only")
    ok, weak = [], []
    for s in picked:
        title, chars = evidence(text, s["name"], _cast_names(folders[s["key"]]), also=(_clean_name(folders[s["key"]]),))
        if title or len(chars) >= 2:
            ok.append(s)
            log(f"  confirmed {s['name']}: " + ("title named" if title else "") + (f"{', ' if title else ''}"
                f"characters {chars[:5]}" if chars else ""))
        else:
            weak.append((s["name"], chars))
    if weak:
        log(f"  ! dropped - the script never names them: {weak}")
    if not ok:
        raise RuntimeError(f"could not confirm which shows/films this script is about (the detector picked "
                           f"{[s['name'] for s in picked]}, the script names none of them) - check the script or "
                           "add its channel to editv2/channels.json")
    return ok


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
                 "LIBRARY (key = folder name):\n" + "\n".join(names) + "\n\nSCRIPT (start):\n" + text[:6000], tag="ctx")
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
    picked = shows or guard_shows(clean, text, detect_shows(text, folders, log), folders, log)
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
