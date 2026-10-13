"""Shared bits for the library job system (index -> refine -> dialogue upgrade), 2026-10-08.
Everything is resumable from the catalogue files themselves; this folder only keeps the queue, locks,
failure counters, logs and a status file."""
import hashlib, json, os, re, sys, time, urllib.request
# Background jobs run under pythonw (no console): every ffmpeg/ffprobe/powershell child would otherwise pop up
# its own Terminal window. Give every child process CREATE_NO_WINDOW.
import subprocess as _sp
if hasattr(_sp, "CREATE_NO_WINDOW") and not getattr(_sp.Popen, "_nowin", False):
    _orig_init = _sp.Popen.__init__

    def _nowin_init(self, *a, **kw):
        kw["creationflags"] = kw.get("creationflags", 0) | _sp.CREATE_NO_WINDOW
        _orig_init(self, *a, **kw)
    _sp.Popen.__init__ = _nowin_init
    _sp.Popen._nowin = True

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, r"D:\Lib\agy_rotation"); sys.path.insert(0, r"D:\Lib")
LOCKS, FAILS, LOGS, DONE = (os.path.join(HERE, d) for d in ("locks", "fails", "logs", "done"))
for d in (LOCKS, FAILS, LOGS, DONE):
    os.makedirs(d, exist_ok=True)
QUEUE = os.path.join(HERE, "queue.json")
CONFIG = os.path.join(HERE, "config.json")
MAX_FAILS = 3
PROXY = "http://127.0.0.1:8317/v1/models"


def cfg():
    try:
        return json.load(open(CONFIG, encoding="utf-8"))
    except Exception:
        return {"workers": 3}


def lp(p):
    if os.name == "nt" and p and len(os.path.abspath(p)) >= 250 and not p.startswith("\\\\?\\"):
        return "\\\\?\\" + os.path.abspath(p)
    return p


def key(v):
    return hashlib.md5(v.lower().encode("utf-8")).hexdigest()


def proxy_ok():
    try:
        k = open(os.path.expanduser("~/.model-router-proxy-key.txt")).read().strip()
        req = urllib.request.Request(PROXY, headers={"Authorization": f"Bearer {k}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception:
        return False


def _volumes():
    """libjobs.volumes, whether this module was imported as 'common' (workers) or 'libjobs.common' (tools)."""
    try:
        import volumes
    except ImportError:
        from libjobs import volumes
    return volumes


def drive_ok(v=None):
    """the Google Drive mount the video is on (or the main one) answers. Letters come from libjobs.volumes."""
    volumes = _volumes()
    m = volumes.mapping()
    vid = volumes.volume_of(v) if v else None
    letter = m.get(vid) if vid in volumes.CLOUD else m.get(volumes.CLOUD[0])
    try:
        if not letter or not os.listdir(letter + r":\My Drive"):
            return False
        if v:
            return os.path.isdir(lp(os.path.dirname(v)))
        return True
    except OSError:
        return False


def healthy(v=None):
    volumes = _volumes()
    return proxy_ok() and (not v or not volumes.is_cloud(v) or drive_ok(v))


def state(v):
    """index | refine | dialogue | done   (decided ONLY from the catalogue on disk; read ONCE)"""
    from media_index import subtitles
    cat = os.path.splitext(v)[0] + ".catalog.json"
    if not os.path.exists(lp(cat)):
        if not os.path.isdir(lp(os.path.dirname(v))):
            raise OSError("folder not readable (Drive?)")
        return "index"
    try:                                   # an existing catalogue that can't be read = Drive hiccup, NEVER re-index
        d = json.load(open(lp(cat), encoding="utf-8"))
    except Exception as e:
        raise OSError(f"catalogue not readable now: {e}")
    shots = d.get("shots") or []
    complete = bool(shots) and (d.get("complete") is True or
                                ("complete" not in d and all((s.get("description") or "").strip() for s in shots)))
    if not complete:
        return "index"                     # readable but half-done -> resume indexing
    from media_index.catalog_refine import VERSION
    if (d.get("refined") or {}).get("version", 0) < VERSION:
        return "refine"
    if str(d.get("dialogue_source", "")).startswith("whisper"):
        checked = {tuple(x) for x in (d.get("dialogue_info") or {}).get("checked", [])}
        for p in subtitles.sidecar_candidates(v):          # only a NEW / changed subtitle file reopens dialogue
            try:
                st = os.stat(lp(p))
            except OSError:
                continue
            if (os.path.basename(p), int(st.st_mtime), st.st_size) not in checked:
                return "dialogue"
    return "done"


def is_done(v):
    """done marker valid only for the CURRENT refine version (older markers are ignored and removed)."""
    p = os.path.join(DONE, key(v))
    if not os.path.exists(p):
        return False
    from media_index.catalog_refine import VERSION
    try:
        ok = open(p).read().strip() == str(VERSION)
    except OSError:
        ok = False
    if not ok:
        try:
            os.remove(p)
        except OSError:
            pass
    return ok


def mark_done(v):
    from media_index.catalog_refine import VERSION
    open(os.path.join(DONE, key(v)), "w").write(str(VERSION))


def reset_counters(v):
    for p in (os.path.join(FAILS, key(v) + ".json"), os.path.join(HERE, "deferred", key(v)),
              os.path.join(HERE, "unreadable", key(v))):
        try:
            os.remove(p)
        except OSError:
            pass


def note_unreadable(v):
    """state() raised (catalogue unreadable) - after 3 times in a row the video shows up in status.txt."""
    d = os.path.join(HERE, "unreadable")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, key(v))
    try:
        n = int(open(p).read().split()[0]) + 1
    except Exception:
        n = 1
    open(p, "w", encoding="utf-8").write(f"{n} {v}")


def fails(v):
    p = os.path.join(FAILS, key(v) + ".json")
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {"n": 0, "video": v, "errors": []}


def _sig(v):
    try:
        st = os.stat(lp(v))
        return [st.st_size, int(st.st_mtime)]
    except OSError:
        return None


def add_fail(v, err):
    f = fails(v)
    f["n"] += 1
    f["errors"] = (f["errors"] + [time.strftime("%Y-%m-%d %H:%M ") + err])[-5:]
    f["sig"] = _sig(v) or f.get("sig")        # size+mtime: a re-downloaded file gets a fresh start
    json.dump(f, open(os.path.join(FAILS, key(v) + ".json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def replaced_since_fail(v, f=None):
    """True when the file was replaced (size/mtime changed) after it was parked as failed."""
    f = f or fails(v)
    now = _sig(v)
    return bool(f.get("sig") and now and now != f["sig"])


def pid_alive(pid, must=""):
    try:
        import psutil
        p = psutil.Process(int(pid))
        return p.is_running() and (not must or must in " ".join(p.cmdline()))
    except Exception:
        return False


# Video locks are OS byte-range locks (msvcrt) held on an open handle for the whole job: Windows releases them the
# moment the holder exits or dies, so there is no "stale lock" guessing. The old pid-in-file scheme raced when 8
# workers started together (2026-10-10 17:54: a lock file is empty for an instant between create and write -> read
# as stale, deleted -> two workers refined Vikings S01E07 at once, writing the same <catalogue>.tmp).
_LOCK_FH = {}


def _byte_lock(fh):
    import msvcrt
    fh.seek(0)
    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)     # raises OSError at once if another process holds it


def locked(v):
    """True if another worker holds this video right now."""
    p = os.path.join(LOCKS, key(v) + ".lock")
    if p in _LOCK_FH:
        return True
    if not os.path.exists(p):
        return False
    try:
        fh = open(p, "a+")
    except OSError:
        return True
    try:
        _byte_lock(fh)
    except OSError:
        fh.close()
        return True
    import msvcrt
    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    fh.close()
    return False


def try_lock(v):
    """-> lock path if THIS process now holds the video, else None. Keep it until unlock(path)."""
    p = os.path.join(LOCKS, key(v) + ".lock")
    if p in _LOCK_FH:
        return None
    try:
        fh = open(p, "a+", encoding="utf-8")    # utf-8: a Cyrillic file name crashed 5 workers (cp1252) 2026-10-11
    except OSError:
        return None
    try:
        _byte_lock(fh)
    except OSError:
        fh.close()
        return None
    try:                                                    # who holds it - for people reading the folder only
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()} {v}")
        fh.flush()
    except Exception:                                       # informational only: never let it cost the lock
        pass
    _LOCK_FH[p] = fh
    return p


def unlock(p):
    import msvcrt
    fh = _LOCK_FH.pop(p, None)
    if fh is not None:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        fh.close()
    try:
        os.remove(p)
    except OSError:
        pass


ROOT_NAMES = ("Movies DATA", "Movies DATA 2", "Movies DATA 3", "Movies", "Cartoons DATA")
DESK_CAST = r"C:\Users\Dell\Desktop\Cast"


def show_root(v):
    parts = v.replace("/", "\\").split("\\")
    for i, p in enumerate(parts):
        if p in ROOT_NAMES and i + 1 < len(parts):
            return "\\".join(parts[: i + 2])
    return os.path.dirname(v)


def _names(c):
    r"""Character names = sub-folders of a Cast folder. Skips '_QA_REPORTS'-style folders, descends into a single
    nested folder ('Cast\Breaking Bad Cast\<names>'), Title-cases all-lowercase names ('hank' -> 'Hank')."""
    base = lp(c)
    subs = [x for x in os.listdir(base) if os.path.isdir(os.path.join(base, x)) and not x.startswith(("_", "."))]
    if len(subs) == 1 or (subs and all(x.lower().endswith(" cast") for x in subs)):
        inner = os.path.join(c, subs[0])
        deeper = [x for x in os.listdir(lp(inner)) if os.path.isdir(os.path.join(lp(inner), x)) and not x.startswith(("_", "."))]
        if len(deeper) >= 3:
            subs, c = deeper, inner
    return sorted(x.title() if x == x.lower() else x for x in subs)


_CAST_CACHE = {}


def is_episode(v):
    """TV episode (has a season/episode) vs film."""
    from media_index.catalog_refine import episode_of
    return episode_of(v) is not None


def cast_of(v, allow_llm=True):
    """cached 10 min - per show folder for TV episodes, per FILM for films (a folder of many films - Other Movies, the
    two Jack Reacher films - must never hand one film's cast to another). Fewer than 3 names is never cached, so a
    Drive hiccup cannot stick. A folder that cannot be listed raises OSError('cannot read ...'). Films without any
    Cast folder get their character names from agy Claude (auto_cast) unless allow_llm=False."""
    ck = show_root(v).lower() if is_episode(v) else v.lower()
    hit = _CAST_CACHE.get(ck)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        names = _cast_uncached(v, allow_llm)
    except OSError as e:
        raise OSError(f"cannot read: cast folder ({e})")
    if len(names) >= 3:
        _CAST_CACHE[ck] = (time.time(), names)
    return names


def _cast_dir_in(d):
    """the 'Cast' sub-folder of d (any case), or None."""
    for e in os.listdir(lp(d)):
        if e.lower() == "cast" and os.path.isdir(os.path.join(lp(d), e)):
            return os.path.join(d, e)
    return None


_DESK_DIRS = [0.0, []]


def _desk_dirs():
    r"""Show/film folders under Desktop\Cast, also grouped ones (Desktop\Cast\Star Wars\TV Shows\Andor\Cast) - up to 3
    levels, shallow first; 'Cast' folders and the character folders inside them are not candidates. Cached 10 min."""
    if time.time() - _DESK_DIRS[0] < 600:
        return _DESK_DIRS[1]
    out = []
    for dp, dn, fn in os.walk(DESK_CAST):
        depth = dp[len(DESK_CAST):].count(os.sep)
        keep = []
        for d in dn:
            if d.lower() == "cast" or d.startswith(("_", ".")):
                continue
            out.append((depth + 1, os.path.join(dp, d), re.sub(r"[^a-z0-9]", "", d.lower())))
            keep.append(d)
        dn[:] = keep if depth < 2 else []
    out.sort(key=lambda x: x[0])
    _DESK_DIRS[:] = [time.time(), out]
    return out


def _desk_cast(wants):
    if not os.path.isdir(DESK_CAST):
        return None
    for want in [w for w in wants if len(w) >= 4]:
        for depth, c, k in _desk_dirs():
            if len(k) >= 4 and (k == want or want.startswith(k)):
                inner = _cast_dir_in(c)
                if depth > 1 and not inner:
                    continue                      # deeper folders count only when they hold a 'Cast' folder
                names = _names(inner or c)
                if len(names) >= 3:
                    return names
    return None


AUTOCAST = os.path.join(HERE, "autocast")


def auto_cast(v, allow_llm=True):
    r"""A FILM with no Cast folder anywhere: its credited character names from agy Claude (the indexer only uses the
    names of a Cast folder, never its photos). Cached for good per file; [] if the model does not know the film.
    allow_llm=False only reads the cache (the supervisor's status must never wait on an LLM)."""
    os.makedirs(AUTOCAST, exist_ok=True)
    p = os.path.join(AUTOCAST, key(v) + ".json")
    try:
        return json.load(open(p, encoding="utf-8"))["names"]
    except Exception:
        pass
    if not allow_llm:
        return []
    base = os.path.basename(v)
    m = re.search(r"\b(19\d\d|20\d\d)\b", base) or re.search(r"\b(19\d\d|20\d\d)\b", os.path.basename(os.path.dirname(v)))
    title = re.sub(r"[._]+", " ", re.split(r"[\s._\-\[(]+(?:19|20)\d\d\b", os.path.splitext(base)[0])[0]).strip()
    sys.path.insert(0, r"D:\Lib")
    from editv2.common import llm
    try:
        r, model = llm("You are a film expert. Answer only from solid knowledge; never guess.",
                       f'Film: "{title}"{" (" + m.group(1) + ")" if m else ""}. Video file: "{base}", in folder '
                       f'"{os.path.basename(os.path.dirname(v))}". The title may be a transliteration (e.g. a Russian '
                       "release name). List the characters of this film as credited - the main and notable supporting roles, "
                       "up to 30 - by the character names an English-speaking viewer knows (never actor names). If you do "
                       'not know this exact film well, set known to false. Reply ONLY JSON {"known": true, "film": "<English '
                       'title (year)>", "names": ["...", ...]}', max_tokens=3000, tries=2, temperature=0)
    except Exception:
        return []                                 # not cached: asked again next time
    names = []
    if isinstance(r, dict) and r.get("known") is True and isinstance(r.get("names"), list):
        names = sorted({str(n).strip() for n in r["names"] if isinstance(n, str) and 2 <= len(n.strip()) <= 60})
    if len(names) < 3:
        names = []
    json.dump(dict(names=names, film=(r or {}).get("film") if isinstance(r, dict) else None, model=model,
                   t=time.time(), video=v), open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return names


def _title(x):                            # "A.Prophet.2009.1080p.BluRay..." -> "aprophet"
    x = re.split(r"[\s._\-\[(]+(19|20)\d\d\b", os.path.splitext(x)[0])[0]
    return re.sub(r"[^a-z0-9]", "", x.lower())


def _cast_uncached(v, allow_llm=True):
    r"""Cast of THIS show / THIS film only. Never a parent's or a sibling's Cast (that handed other shows' names to
    Gemini). Episode: a 'Cast' folder anywhere inside the show's own folder, else Desktop\Cast\<show>.
    Film: a 'Cast' folder on the film's own path below the collection folder, else Desktop\Cast\<film title>, else
    a 'Cast' directly in its collection folder, else Desktop\Cast\<collection>."""
    top = show_root(v)
    base = lp(top)

    def boom(e):
        raise e
    if is_episode(v):
        for dp, dn, fn in os.walk(base, onerror=boom):
            if dp[len(base):].count(os.sep) > 4:
                dn[:] = []
                continue
            for d in dn:
                if d.lower() == "cast":
                    return _names(os.path.join(dp, d))
        return _desk_cast([_title(os.path.basename(top))]) or []
    d = os.path.dirname(v)
    while len(os.path.normpath(d)) > len(os.path.normpath(top)):
        c = _cast_dir_in(d)
        if c:
            return _names(c)
        d = os.path.dirname(d)
    hit = _desk_cast([_title(os.path.basename(v)), _title(os.path.basename(os.path.dirname(v)))])
    if hit:
        return hit
    c = _cast_dir_in(top)
    if c:
        return _names(c)
    return _desk_cast([_title(os.path.basename(top))]) or auto_cast(v, allow_llm)


def show_name(v):
    import re
    parts = v.replace("/", "\\").split("\\")
    for i, p in enumerate(parts):
        if p in ("Movies DATA", "Movies DATA 2", "Movies DATA 3", "Movies", "Cartoons DATA") and i + 1 < len(parts):
            return re.sub(r"[._]+", " ", parts[i + 1])
    return os.path.basename(os.path.dirname(v))


HOLD = os.path.join(HERE, "hold.json")


def held(v):
    """True if this video's show is on the hold list: its finished catalogue is never refined/rewritten."""
    try:
        keys = json.load(open(HOLD, encoding="utf-8"))
    except Exception:
        return False
    low = v.lower()
    return any(k.lower() in low for k in keys)


DEFER = os.path.join(HERE, "deferred")
os.makedirs(DEFER, exist_ok=True)


def defer(v):
    p = os.path.join(DEFER, key(v))
    try:
        n = int(open(p).read().split()[0]) + 1
    except Exception:
        n = 1
    open(p, "w").write(f"{n} {time.time()}")
    return n


def deferred(v):
    p = os.path.join(DEFER, key(v))
    try:
        n, t = open(p).read().split()
        return time.time() - float(t) < 1800
    except Exception:
        return False


def drive_restarted_recently(v=None, minutes=10):
    """Google Drive for desktop restarts itself every few hours (server-pushed RESTART_APP_NOW); errors on a
    Drive-backed file inside that window are the restart, not the file. Local (E:) files never qualify."""
    volumes = _volumes()
    if v and not volumes.is_cloud(v):
        return False
    try:
        import psutil
        ts = [p.create_time() for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == "googledrivefs.exe"]
        return bool(ts) and time.time() - max(ts) < minutes * 60
    except Exception:
        return False


def bad_sources():
    out = []
    for f in os.listdir(FAILS):
        try:
            d = json.load(open(os.path.join(FAILS, f), encoding="utf-8"))
        except Exception:
            continue
        if any("bad source" in e for e in d.get("errors", [])):
            out.append(d)
    return out
