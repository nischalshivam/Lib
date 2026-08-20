"""The tool in a browser.

A terminal menu can ask questions and print numbers. It cannot show you a
picture, and every problem this tool has left is a problem you can only see:
a shot from the wrong scene, a still that sits too long, a beat with nothing
in it. Six builds were spent describing those to each other in text when a
single glance would have settled them.

So this serves the same data the menu already produces — the manifest, the
timeline, the library — as pages you can look at, with the actual frames on
screen.

## Why the standard library and nothing else

The tool installs from a zip on a Windows machine with no build tools. Every
dependency so far has been either optional (torch, faster-whisper) or a
single wheel (numpy). A web framework would be neither, and a browser page
that needs `pip install` before it opens is not a page anyone will use.
`http.server` is enough: this serves one machine, one person, over
localhost, and the heavy lifting is all in files that already exist on disk.

## What it will not do

It does not hold state. Every page reads the same folders the menu writes,
so the browser and the menu can be open at once and neither can confuse the
other. Nothing here is a second source of truth.
"""
from __future__ import annotations

import http.server
import json
import os
import posixpath
import socket
import socketserver
import threading
import urllib.parse
import webbrowser

from . import builds, cast, editor, jobs as jobs_mod, libraries, \
    library, sources, term

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "web_ui.html")
UI = os.path.join(HERE, "ui")
# The design lives one folder up from the package, beside the brief it was
# written from, because it is not code: it is the drawing the screens are
# built to match, and it gets replaced wholesale when it is redesigned.
DESIGN = os.path.join(os.path.dirname(HERE), "design", "Movie Editor.dc.html")
# Served by name, never by path. The page asks for /ui/app.js, not for a file.
ASSETS = {"app": ("app.html", "text/html; charset=utf-8"),
          "app.js": ("app.js", "text/javascript; charset=utf-8"),
          "dcx.js": ("dcx.js", "text/javascript; charset=utf-8"),
          "screens": ("screens.html", "text/html; charset=utf-8")}
DEFAULT_PORT = 8712
# Files the browser is allowed to ask for, by extension. A local server is
# still a server: it should never hand out a database or a script because a
# URL asked nicely.
SERVABLE = (".mp4", ".jpg", ".jpeg", ".png", ".m4a", ".mp3", ".txt")
TYPES = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".png": "image/png", ".m4a": "audio/mp4", ".mp3": "audio/mpeg",
         ".txt": "text/plain; charset=utf-8"}


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def build_folder(out: str) -> dict:
    """Everything one output folder knows about itself.

    The manifest says what was cut and where each asset came from; the
    timeline says when each one is on screen. Neither is complete on its
    own, and the browser needs both at once — so they are joined here rather
    than in the page, where a mistake would be invisible.
    """
    out = os.path.abspath(out)
    manifest = _read_json(os.path.join(out, "manifest.json")) or {}
    timeline = _read_json(os.path.join(out, "timeline.json")) or {}
    video = os.path.join(out, "video.mp4")

    facts = {}
    for scene in (manifest.get("scenes") or []):
        for a in (scene.get("assets") or []):
            facts[(scene.get("scene"), a.get("file"))] = a

    scenes = []
    for s in (timeline.get("scenes") or []):
        items = []
        for i in (s.get("items") or []):
            known = facts.get((s.get("scene"), i.get("file")), {})
            items.append({
                "file": i.get("file", ""),
                "kind": i.get("kind", "image"),
                "start": round(float(i.get("start") or 0.0), 2),
                "duration": round(float(i.get("duration") or 0.0), 2),
                "source": i.get("source") or known.get("source") or "",
                "source_start": i.get("source_start"),
                "placed_by": i.get("placed_by") or known.get("placed_by", ""),
                "confidence": i.get("confidence", ""),
                "url": f"/file?out={urllib.parse.quote(out)}&"
                       f"rel={urllib.parse.quote('scene_%03d/%s' % (s.get('scene') or 0, i.get('file','')))}",
            })
        scenes.append({
            "scene": s.get("scene"),
            "narration": s.get("narration", ""),
            "start": round(float(s.get("start") or 0.0), 2),
            "end": round(float(s.get("end") or 0.0), 2),
            "note": s.get("note", ""),
            "items": items,
        })

    counts: dict = {}
    for s in scenes:
        for i in s["items"]:
            key = i["placed_by"] or "unknown"
            counts[key] = counts.get(key, 0) + 1
    return {
        "out": out,
        "video": timeline.get("video", "") or manifest.get("video", ""),
        "audio": timeline.get("audio", ""),
        "total_seconds": round(float(timeline.get("total_seconds") or 0.0), 2),
        "pace": timeline.get("pace", ""),
        "scenes": scenes,
        "counts": counts,
        "empty": sum(1 for s in scenes if not s["items"]),
        "rendered": (f"/file?out={urllib.parse.quote(out)}&rel=video.mp4"
                     if os.path.isfile(video) else ""),
        "has_manifest": bool(manifest),
        "has_timeline": bool(timeline),
    }


# Folders and files a picker will show. A browser cannot open a native file
# dialog from a page, so the tool has to do the walking itself — and it
# should only ever offer the kinds of file the field is actually for.
PICK = {"script": (".json", ".txt"),
        "narration": (".txt", ".md", ".json"),
        "audio": (".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg"),
        "folder": ()}


def script_facts(path: str) -> dict:
    """What a script says about itself, read the moment it is chosen.

    This is the one number that tells someone the tool understood the file
    they just picked. Getting it after a forty-minute build is not the same
    information.
    """
    beats = jobs_mod.read_beats(path)
    shots = sum(len(b.get("shots") or []) for b in beats)
    reqs = sources.requirements(beats)
    # `episodes_declared` holds (season, episode) pairs. Handing those to a
    # page as-is puts "1,1, 1,3" on screen, which is not a thing anyone has
    # ever called an episode.
    episodes = sorted({se for r in reqs for se in r.episodes_declared})
    labelled = [f"S{int(s):02d}E{int(e):02d}" for s, e in episodes]
    summary, note = jobs_mod.script_extras(path)
    from . import characters as characters_mod
    cast_needed = characters_mod.needed(beats)
    return {"path": os.path.abspath(path), "beats": len(beats),
            "shots": shots, "titles": [r.title for r in reqs],
            "episodes": labelled[:24], "episodes_total": len(labelled),
            # Whose face the tool will need reference photos of, read straight
            # from the script — shown here so the user gathers those photos
            # before building, not after a wrong-person still ships.
            "cast_needed": cast_needed,
            # The timings box, already filled in from what the script said.
            # The script's ranges are the model's guesses and some of them
            # are ten minutes wide — which is exactly why they belong in an
            # editable box rather than being applied silently. Somebody can
            # see them, fix the two that matter, and build.
            "timings": timings_text(beats),
            "summary": summary, "note": note[:600]}


def timings_text(beats: list) -> str:
    """The script's own `scene_range` fields, as lines for the timings box.

    One line per run that declared one, longest run first — the run with
    eighty-five shots in it is the one worth checking, and it should not be
    third in the list because of the order the essay happens to visit
    episodes in.
    """
    from . import align, subtitles, timings as timings_mod

    seen, lines = set(), []
    for said in timings_mod.from_script(beats):
        if said.season is None or said.episode is None:
            continue
        key = f"S{said.season:02d}E{said.episode:02d}"
        if key in seen:
            continue
        seen.add(key)
        shots = sum(len(r.entries) for r in align.runs(beats)
                    if subtitles.episode_key(r.season_episode or "")
                    == (said.season, said.episode))
        lo, hi = said.lo, said.hi
        span = (f"{int(lo // 60)}:{int(lo % 60):02d}-"
                f"{int(hi // 60)}:{int(hi % 60):02d}" if hi > lo
                else f"{int(lo // 60)}:{int(lo % 60):02d}")
        lines.append((shots, f"{key} {span}"))
    lines.sort(key=lambda a: -a[0])
    return "\n".join(line for _n, line in lines)


def narration_facts(path: str) -> dict:
    """How many words the narration script holds, and whether it is the one.

    Both numbers matter and the second one more: a narration script for a
    different video would sail through every other check and quietly retime
    the whole build.
    """
    from . import narration

    text = narration.read_clean(path)
    words = narration.normalise(text)
    return {"path": os.path.abspath(path), "words": len(words)}


def clue_facts(path: str) -> dict:
    """What a clue script offers, before any of it has been checked.

    Deliberately counts the two things separately: how many clues there are,
    and how many of them actually carry a line of dialogue. A 40-clue script
    where 30 clues remembered no line is worth less than a 12-clue one where
    every clue did, and only the second number says so. The subtitle lookup
    that decides which of those lines are real happens at Check — this is
    the two-second answer someone gets the moment they pick the file.
    """
    from . import clues as clues_mod

    found = clues_mod.read(path)                # raises ClueError
    with_line = [c for c in found if c.lines]
    return {
        "path": os.path.abspath(path),
        "clues": len(found),
        "with_dialogue": len(with_line),
        "lines": sum(len(c.lines) for c in found),
        "bracketed": len([c for c in found if c.before and c.after]),
        "episodes": len({c.episode for c in found if c.episode}),
        "people": sorted({n for c in found for n in c.on_screen})[:12],
        "silent": len([c for c in found if c.silent]),
    }


def audio_facts(path: str) -> dict:
    from .probe import ProbeError, probe
    try:
        info = probe(path)
    except ProbeError as exc:
        return {"path": os.path.abspath(path), "error": str(exc)[:200]}
    return {"path": os.path.abspath(path), "seconds": round(info.duration, 1)}


# The real Windows "Open file" box, asked for by name.
#
# A browser cannot open one: `<input type=file>` hands back a name and never
# a path, which is exactly the thing this tool needs. But the server IS the
# machine, so it can open the dialog itself and answer with the full path.
#
# In a subprocess on purpose. tkinter wants to own a thread's event loop and
# does not take kindly to being started inside a web server's worker; a
# separate short-lived process cannot destabilise anything, and if tkinter
# is missing altogether the process simply exits and the page falls back to
# the folder list it already has.
_DIALOG = r'''
import sys
try:
    import tkinter
    from tkinter import filedialog
except Exception:
    sys.exit(3)
kind, start = sys.argv[1], (sys.argv[2] or None)
root = tkinter.Tk()
root.withdraw()
try:
    root.attributes("-topmost", True)   # in front of the browser, not behind
except Exception:
    pass
if kind == "folder":
    got = filedialog.askdirectory(title="Folder chuno", initialdir=start,
                                  mustexist=False)
elif kind == "audio":
    got = filedialog.askopenfilename(
        title="Voiceover chuno", initialdir=start,
        filetypes=[("Audio", "*.m4a *.mp3 *.wav *.aac *.flac *.ogg *.mp4"),
                   ("All files", "*.*")])
else:
    got = filedialog.askopenfilename(
        title="Script chuno", initialdir=start,
        filetypes=[("Visual script", "*.json *.txt"),
                   ("All files", "*.*")])
sys.stdout.write(got or "")
'''
DIALOG_TIMEOUT_S = 600          # someone may go and look for the file


def native_pick(kind: str, start: str = "") -> dict:
    """Open the machine's own file dialog. Returns what was chosen."""
    import subprocess
    import sys
    try:
        done = subprocess.run(
            [sys.executable, "-c", _DIALOG, kind, start or ""],
            capture_output=True, timeout=DIALOG_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "path": "", "why": str(exc)[:200]}
    if done.returncode == 3:
        return {"available": False, "path": "",
                "why": "tkinter is not installed with this Python"}
    if done.returncode != 0:
        return {"available": False, "path": "",
                "why": (done.stderr or b"")[-200:].decode("utf-8", "replace")}
    chosen = done.stdout.decode("utf-8", "replace").strip()
    return {"available": True, "path": os.path.abspath(chosen) if chosen else "",
            "cancelled": not chosen}


def browse(path: str, kind: str = "folder") -> dict:
    """One folder's worth of somewhere to go next.

    Deliberately not rooted anywhere: the whole point is choosing a script
    on D: and a library on E:. It only ever LISTS — nothing here opens,
    writes or deletes a thing, and the extensions offered are the field's.
    """
    here = os.path.abspath(path or os.path.expanduser("~"))
    # Walk up until something is really there. The remembered folder from
    # last session may have been deleted, and so may its parent — a picker
    # that opens on a folder that does not exist shows nothing and looks
    # broken, when all that happened is someone tidied their drive.
    while not os.path.isdir(here):
        parent = os.path.dirname(here)
        if parent == here:
            here = os.path.abspath(os.sep)
            break
        here = parent
    wanted = PICK.get(kind, ())
    folders, files = [], []
    try:
        # scandir, and every question about an entry wrapped: a Windows
        # user folder is full of junctions, OneDrive placeholders and
        # things the account cannot read, and one of them must not cost
        # the whole listing.
        for entry in sorted(os.scandir(here), key=lambda e: e.name.lower()):
            try:
                if entry.is_dir():
                    if not entry.name.startswith("."):
                        folders.append({"name": entry.name, "path": entry.path})
                elif wanted and os.path.splitext(entry.name)[1].lower() in wanted:
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        size = 0
                    files.append({"name": entry.name, "path": entry.path,
                                  "size": size})
            except OSError:
                continue                    # a permission wall is not an error
    except OSError as exc:
        return {"path": here, "error": str(exc)[:200],
                "folders": [], "files": [], "up": os.path.dirname(here)}
    up = os.path.dirname(here)
    return {"path": here, "up": up if up != here else "",
            "folders": folders, "files": files,
            "drives": _drives()}


def _drives() -> list:
    """Every drive letter that exists, on the machine this actually runs on.

    D: holds the footage and E: the libraries; a picker that cannot leave
    one of them is a picker nobody can use.
    """
    if os.name != "nt":
        return [{"name": "/", "path": os.path.abspath(os.sep)}]
    found = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\"
        if os.path.isdir(root):
            found.append({"name": f"{letter}:", "path": root})
    return found


def save_upload(spec: dict, folder: str) -> dict:
    """Keep a file that was dragged onto the page.

    A dropped file arrives as its NAME and its CONTENTS — never its path,
    which is the browser refusing on purpose. So the contents are written
    somewhere real and that path is used, which is the same outcome by a
    different road, and the one road that works when a file is somewhere
    awkward to navigate to.
    """
    import base64

    name = os.path.basename((spec.get("name") or "dropped").replace("\\", "/"))
    if not name or name in (".", ".."):
        return {"error": "that file has no usable name"}
    data = spec.get("data") or ""
    try:
        raw = base64.b64decode(data.split(",")[-1], validate=False)
    except Exception:
        return {"error": "the file could not be read"}
    if not raw:
        return {"error": "that file is empty"}
    here = os.path.abspath(folder or os.path.join(os.getcwd(), "dropped"))
    os.makedirs(here, exist_ok=True)
    target = os.path.join(here, name)
    try:
        with open(target, "wb") as f:
            f.write(raw)
    except OSError as exc:
        return {"error": str(exc)[:200]}
    return {"path": target, "name": name, "bytes": len(raw)}


def library_facts(db_path: str) -> dict:
    try:
        stats = library.stats(db_path)
    except Exception as exc:                # a missing database is an answer
        return {"error": str(exc)[:200], "db": os.path.abspath(db_path)}
    stats["db"] = os.path.abspath(db_path)
    return stats


def _render_work(out: str, audio: str):
    """Make the video from a folder that has already been edited."""
    def work(task, log):
        from . import render
        task.out = out
        task.stage = "rendering"
        res = render.render_folder(out, audio=audio, log=log)
        log(render.describe(res))
        if not res.ok:
            task.status = "failed"
            task.error = "; ".join(f"{w}: {why}" for w, why in res.failed[:3]) \
                or "the render produced no file"
            return
        task.video = res.path
        task.stage = f"done — {os.path.basename(res.path)}"
    return work


# One runner for the process. Builds are serialised inside it: two at once
# would fight over ffmpeg and the disk and finish slower than one after the
# other.
RUNNER = builds.Runner()


class Handler(http.server.SimpleHTTPRequestHandler):
    db_path = "library.db"
    out_path = ""
    libraries_root = ""
    uploads = ""

    def log_message(self, *_a):             # the terminal stays readable
        pass

    def _send(self, code: int, body: bytes, kind: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass                            # the tab was closed mid-download

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:               # noqa: N802 (stdlib spelling)
        self._guard(self._get)

    def do_POST(self) -> None:              # noqa: N802 (stdlib spelling)
        self._guard(self._post)

    def _guard(self, work) -> None:
        """Answer every request, even the ones that go wrong.

        An exception escaping a handler drops the connection, and a dropped
        connection reaches the page as "Failed to fetch" — a message that
        names nothing and looks like the tool being down. A 500 carrying the
        actual error is the difference between a bug you can read and a bug
        you have to guess at.
        """
        try:
            work()
        except (BrokenPipeError, ConnectionResetError):
            pass                            # the tab was closed mid-answer
        except Exception as exc:
            try:
                self._json({"error": f"{type(exc).__name__}: {exc}"[:400]}, 500)
            except Exception:
                pass                        # nothing left to answer down

    def _get(self) -> None:
        parts = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parts.query)
        route = parts.path

        if route in ("/", "/index.html"):
            self._serve_asset("app")
            return

        # The shot-by-shot page the tool has had since the sixth build. The
        # app has not replaced it yet, and taking away a page that works in
        # order to show one that is half built is not an upgrade.
        if route in ("/shots", "/shots.html"):
            try:
                with open(PAGE, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError as exc:
                self._send(500, str(exc).encode(), "text/plain")
            return

        if route == "/favicon.ico":
            self._send(204, b"", "image/x-icon")     # asked for by every tab
            return

        if route.startswith("/ui/"):
            self._serve_asset(route[4:])
            return

        if route == "/api/titles":
            self._json(libraries.catalogue(self.libraries_root, self.db_path))
            return

        if route == "/api/script":
            path = (query.get("path") or [""])[0].strip()
            if not os.path.isfile(path):
                self._json({"error": f"no such file: {path}"}, 404)
                return
            try:
                self._json(script_facts(path))
            except Exception as exc:
                # A script that will not parse is the single most common way
                # a build fails, and it fails here rather than forty minutes
                # in. The message is the parser's own, which names the line.
                self._json({"error": str(exc)[:300]}, 400)
            return

        if route == "/api/narration":
            path = (query.get("path") or [""])[0].strip()
            if not os.path.isfile(path):
                self._json({"error": f"no such file: {path}"}, 404)
                return
            self._json(narration_facts(path))
            return

        if route == "/api/clues":
            path = (query.get("path") or [""])[0].strip()
            if not os.path.isfile(path):
                self._json({"error": f"no such file: {path}"}, 404)
                return
            try:
                self._json(clue_facts(path))
            except Exception as exc:
                # Almost always typographic quotes copied out of a chat
                # window — `clues.read` straightens those itself, so if it
                # still failed the file is something else entirely.
                self._json({"error": str(exc)[:300]}, 400)
            return

        if route == "/api/cast":
            # Counting folders and files only — no model, no encoding. A
            # misspelt character folder should surface the moment it is
            # chosen, not forty minutes into a build.
            path = (query.get("path") or [""])[0].strip()
            try:
                self._json({"people": cast.look(path)})
            except cast.CastError as exc:
                self._json({"error": str(exc)}, 400)
            return

        if route == "/api/audio":
            path = (query.get("path") or [""])[0].strip()
            if not os.path.isfile(path):
                self._json({"error": f"no such file: {path}"}, 404)
                return
            self._json(audio_facts(path))
            return

        if route == "/api/browse":
            self._json(browse((query.get("path") or [""])[0],
                              (query.get("kind") or ["folder"])[0]))
            return

        if route == "/api/pick":
            self._json(native_pick((query.get("kind") or ["folder"])[0],
                                   (query.get("path") or [""])[0]))
            return

        if route == "/api/tasks":
            self._json({"tasks": RUNNER.all()})
            return

        if route == "/api/summary":
            out = (query.get("out") or [""])[0].strip()
            if not os.path.isdir(out):
                self._json({"error": f"no such folder: {out}"}, 404)
                return
            self._json(editor.summary(out))
            return

        if route == "/api/task":
            task = RUNNER.get((query.get("id") or [""])[0])
            if not task:
                self._json({"error": "no such task"}, 404)
                return
            self._json(task.as_dict())
            return

        if route == "/api/tasks":
            self._json({"tasks": RUNNER.all()})
            return

        if route == "/api/start":
            self._json({"db": os.path.abspath(self.db_path),
                        "out": self.out_path})
            return

        if route == "/api/library":
            self._json(library_facts(self.db_path))
            return

        if route == "/api/build":
            out = (query.get("out") or [""])[0].strip()
            if not out:
                self._json({"error": "no folder given"}, 400)
                return
            if not os.path.isdir(out):
                self._json({"error": f"no such folder: {out}"}, 404)
                return
            self._json(build_folder(out))
            return

        if route == "/file":
            self._serve_file(query)
            return

        self._send(404, b"not found", "text/plain")

    def _post(self) -> None:
        route = urllib.parse.urlsplit(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            spec = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError) as exc:
            self._json({"error": f"unreadable request: {exc}"}, 400)
            return
        if not isinstance(spec, dict):
            self._json({"error": "expected an object"}, 400)
            return

        if route == "/api/check":
            self._json(builds.check(RUNNER, spec, self.db_path).as_dict())
            return
        if route == "/api/build":
            self._json(builds.build(RUNNER, spec, self.db_path).as_dict())
            return
        if route == "/api/library/look":
            root = (spec.get("root") or "").strip()
            if not os.path.isdir(root):
                self._json({"error": f"no such folder: {root}"}, 404)
                return
            self._json(builds.look_at_folder(root))
            return

        if route == "/api/library/index":
            root = (spec.get("root") or "").strip()
            if not os.path.isdir(root):
                self._json({"error": f"no such folder: {root}"}, 404)
                return
            self._json(builds.index_title(
                RUNNER, root, spec.get("db") or self.db_path,
                pictures=spec.get("pictures", True),
                force=bool(spec.get("force"))).as_dict())
            return

        if route == "/api/upload":
            self._json(save_upload(spec, self.uploads))
            return

        if route in ("/api/alternatives", "/api/replace", "/api/edit",
                     "/api/render"):
            self._edit(route, spec)
            return
        self._send(404, b"not found", "text/plain")

    def _edit(self, route: str, spec: dict) -> None:
        """One shot changed, or the video made. Editing is deliberately
        synchronous except for the render: swapping a frame is seconds, and a
        task id for something that fast is ceremony nobody benefits from."""
        out = (spec.get("out") or "").strip()
        if not os.path.isdir(out):
            self._json({"error": f"no such folder: {out}"}, 404)
            return
        db = spec.get("db") or self.db_path
        scene = int(spec.get("scene") or 0)
        name = spec.get("file") or ""
        try:
            if route == "/api/alternatives":
                self._json(editor.alternatives(out, db, scene, name,
                                               spec.get("query") or ""))
            elif route == "/api/replace":
                self._json(editor.replace(out, db, scene, name,
                                          float(spec.get("at") or 0.0)))
            elif route == "/api/edit":
                if spec.get("remove"):
                    self._json(editor.remove(out, scene, name))
                else:
                    self._json(editor.set_duration(
                        out, scene, name, float(spec.get("duration") or 0.0)))
            else:
                self._json(RUNNER.start(
                    "render", os.path.basename(out),
                    _render_work(out, spec.get("audio") or "")).as_dict())
        except editor.EditError as exc:
            # An edit that cannot be done is an answer, not a fault: the
            # episode is not indexed, or the scene has one shot left.
            self._json({"error": str(exc)}, 400)

    def _serve_asset(self, name: str) -> None:
        """The app's own files, by name from a fixed list.

        Never by path. `/ui/` reaches into the package itself, and a route
        that took a file name from the URL would be a way to read the source
        — or anything else on the machine — from a browser tab.
        """
        if name == "design":
            path, kind = DESIGN, "text/html; charset=utf-8"
        elif name in ASSETS:
            path, kind = os.path.join(UI, ASSETS[name][0]), ASSETS[name][1]
        else:
            self._send(404, b"no such asset", "text/plain")
            return
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError as exc:
            self._send(404, str(exc).encode(), "text/plain")
            return
        self._send(200, body, kind)

    def _serve_file(self, query) -> None:
        out = (query.get("out") or [""])[0]
        rel = (query.get("rel") or [""])[0]
        root = os.path.abspath(out)
        # posixpath.normpath on the RELATIVE part only, then join: a `rel` of
        # "../../library.db" cannot escape, because the result is checked to
        # still live under the folder the page asked for.
        target = os.path.abspath(os.path.join(root, *posixpath.normpath(rel).split("/")))
        if not target.startswith(root + os.sep):
            self._send(403, b"outside the output folder", "text/plain")
            return
        if os.path.splitext(target)[1].lower() not in SERVABLE:
            self._send(403, b"not a servable file", "text/plain")
            return
        if not os.path.isfile(target):
            self._send(404, b"no such file", "text/plain")
            return
        kind = TYPES.get(os.path.splitext(target)[1].lower(),
                         "application/octet-stream")
        try:
            self._send_range(target, kind)
        except OSError as exc:
            self._send(500, str(exc).encode(), "text/plain")

    def _send_range(self, target: str, kind: str) -> None:
        """Serve a file, honouring Range — which video is not optional about.

        A browser will not play a `<video>` from a server that answers the
        whole file to a range request: Chromium reports MEDIA_ERR_SRC_NOT_
        SUPPORTED and shows a grey rectangle, which looks exactly like a
        broken clip rather than a missing feature. Every thumbnail in the
        editor and the finished video itself go through here.
        """
        size = os.path.getsize(target)
        asked = (self.headers.get("Range") or "").strip()
        start, end = 0, size - 1
        partial = False
        if asked.lower().startswith("bytes=") and size:
            first, _, last = asked[6:].partition("-")
            try:
                if first:
                    start = int(first)
                    end = int(last) if last else size - 1
                else:                       # "bytes=-500": the last 500 bytes
                    start = max(0, size - int(last))
                partial = True
            except ValueError:
                partial = False             # unreadable range: send it all
            if partial and (start >= size or start > end):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end = min(end, size - 1)

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", kind)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with open(target, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass                            # scrubbed away mid-download


class Server(socketserver.ThreadingTCPServer):
    """Threaded: a page holding a video open must not block the next click."""
    allow_reuse_address = True
    daemon_threads = True


def free_port(start: int = DEFAULT_PORT, tries: int = 20) -> int:
    """The first port nothing else is on. A second copy of the tool should
    open rather than crash with 'address already in use'."""
    for port in range(start, start + tries):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


def serve(db_path: str = "library.db", out: str = "", port: int = 0,
          open_browser: bool = True, log=print, libraries_root: str = "") -> None:
    """Run until interrupted. Localhost only — never the network."""
    Handler.db_path = db_path
    Handler.out_path = os.path.abspath(out) if out else ""
    Handler.libraries_root = libraries_root
    # Dropped files land beside the database, which is a folder the tool
    # already owns and already backs up.
    Handler.uploads = os.path.join(
        os.path.dirname(os.path.abspath(db_path)) or ".", "dropped")
    port = port or free_port()
    url = f"http://127.0.0.1:{port}/"
    d = term.sym("dot")
    with Server(("127.0.0.1", port), Handler) as httpd:
        log(f"  the tool is open at {url}")
        log(f"  library {d} {os.path.abspath(db_path)}")
        if Handler.out_path:
            log(f"  video   {d} {Handler.out_path}")
        log("  leave this window open while you use it; Ctrl+C closes it")
        if open_browser:
            threading.Timer(0.4, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            log("\n  closed")
