#!/usr/bin/env python3
"""ProStudio Launcher — Clean + Clue + Audio  ->  finished, effects-edited video.

One window. For each video you add three files:

    * clean.txt    the narration script (the exact words the voice says)
    * clue.json    the visual/clue script (beats -> shots, from GPT/Genspark)
    * audio.wav    the voiceover (TTS of the clean script)

Press **Run** and the launcher does the whole pipeline, per video, by itself:

    1. find the movies SSD by its volume label  (drive letter can change freely)
    2. libcheck  — is every episode this script needs already catalogued?
                   if not, it STOPS this video and names the episodes to build.
    3. gemini    — confirm the API key is present (used to verify clip identity)
    4. makevideo — cut the right real clips per line, aligned to the voiceover
    5. prostudio — apply the pro effects (transitions, frame system, kinetic
                   text, first-2-min ramp) and render the final MP4. Each video
                   gets a DIFFERENT format so a batch never looks repeated.

**+ Add Video** queues more (20+), **Run** processes them one at a time (the
footage lives on a USB SSD that drops under parallel reads, so serial is
correct). Every finished video lands in its own build folder with the mp4.

No editor to fight with: the clip-picking tool and the effects engine are both
already built — this just wires them end to end and runs them in order.

CLI (headless / overnight), same pipeline, no window:

    python studio.py --clean clean.txt --clue clue.json --audio vo.wav [--out DIR]
    python studio.py --queue jobs.json          # [{clean,clue,audio,out?}, ...]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field

for _s in (sys.stdout, sys.stderr):      # UTF-8 console: never crash on a "→" path
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from media_index import libcheck                      # noqa: E402
from media_index import paths as _paths               # noqa: E402

# Editorial / interview / combined-show beats (a clue label with no dedicated
# footage, e.g. "… Steve Holland interview") fill from the shows already in scope
# instead of hard-blocking the whole video. Real, indexable episodes still gate.
# Override with MI_FILL_GAPS=0.
os.environ.setdefault("MI_FILL_GAPS", "1")

PROSTUDIO = os.path.join(HERE, "prostudio", "prostudio.py")
VTEXT = os.path.join(HERE, "vtext_tool", "vtext.py")   # kinetic-text finisher
# Ten formats rotate so a batch of videos never looks the same (variety.py).
_FORMATS = ["F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10"]


# --------------------------------------------------------------------------- #
#  the pipeline (pure, GUI-independent — the CLI and the GUI both call this)   #
# --------------------------------------------------------------------------- #
@dataclass
class Job:
    clean: str                       # clean narration .txt
    clue: str                        # clue/visual .json/.jsonl/.txt
    audio: str                       # voiceover .wav/.mp3
    out: str = ""                    # build folder (auto if blank)
    save_dir: str = ""               # where the FINISHED video is delivered
    movies_root: str = ""            # auto from SSD volume label if blank
    fmt: str = "auto"                # auto = rotate a fresh format per video
    resolution: str = "1080p"        # 1080p | 4K
    text: bool = False               # on-screen kinetic text (off by default)
    verify: bool = False             # re-check each clip with Gemini (costs API).
                                     # OFF by default: the library is already
                                     # accurate, so retrieval alone places clips
                                     # for FREE — and it avoids the over-rejection
                                     # that turns good clips into gaps.
    verify_intro_min: int = 0        # >0: verify ONLY the first N minutes (the
                                     # intro people actually watch), then free.
    language: str = "en"             # script/voiceover language (en/pt/fr/es/de/
                                     # auto). Library stays English — this only
                                     # tells whisper which model to listen with.
    intro_punch: bool = False        # first 3 min: on famous lines, narration
                                     # ducks and the ORIGINAL show voice plays
                                     # (with a breath each side), then resumes.
    cold_open: bool = False          # open the video on the script's first hook
                                     # line in the ORIGINAL voice (5-8s), then
                                     # the narration starts.
    ken_burns: bool = False          # slow zoom/pan on still frames so they are
                                     # never frozen (rotates in/out/pan).
    frame: bool = False              # premium 'framed' look: footage in a rounded
                                     # card on a textured background (one per video
                                     # from bg_folder). All effects work inside it.
    bg_folder: str = ""              # folder of background images (auto if blank)
    kinetic_text: bool = False       # competitor-style on-screen text (VText),
                                     # audio-synced, at important moments only.
    text_file: str = ""              # hand-made VText instruction file; blank +
                                     # kinetic_text on = auto-generate from script.
    auto_voice: bool = False         # no audio given -> generate the voiceover with
                                     # the local OmniVoice server (voice-cloned).
    voice_ref: str = ""              # a short sample of the narrator's voice to clone
    index: int = 0                   # position in the queue (drives format rotation)
    engine: str = "classic"          # classic | v2 (editv2: grounded agy clue, Gemini
                                     # re-anchor, HyperFrames looks, QA report)
    look: str = "auto"               # v2 only: auto | A (pop) | B (framed) | C (cinematic)
    # results
    status: str = "queued"           # queued|blocked|running|done|error
    message: str = ""
    video: str = ""


def _find_cast(show_folder: str) -> str:
    """A show's `Cast` folder, wherever it sits (some are nested a level deeper,
    e.g. `The Big Bang Theory\\The Big Bang Theory\\Cast`). The one with the most
    character subfolders wins."""
    best, best_n = "", 0
    for root, dirs, _files in os.walk(show_folder):
        if os.path.basename(root).lower() == "cast":
            n = sum(1 for d in dirs if os.path.isdir(os.path.join(root, d)))
            if n > best_n:
                best_n, best = n, root
            dirs[:] = []                       # don't descend into a cast folder
    return best


def _cast_for(movies_root: str, clue: str) -> str:
    """The cast (reference-photo) folder for the show this script mostly draws
    from — so makevideo can VERIFY character identity instead of guessing it.
    Picks the dominant show when a script spans more than one."""
    import re
    try:
        need = libcheck.needed_from_script(clue)      # {show_lower: {eps}}
    except Exception:
        return ""
    if not need:
        return ""
    have = []                                     # (root, show folder) across roots
    for root in _paths.split_roots(movies_root):
        if os.path.isdir(root):
            have += [(root, d) for d in os.listdir(root)
                     if os.path.isdir(os.path.join(root, d))]
    norm = lambda s: re.sub(r"[^a-z0-9]+", "", s.lower())
    for show, _eps in sorted(need.items(), key=lambda kv: -len(kv[1])):
        w = norm(show)
        for root, d in have:
            dd = norm(d)
            if dd == w or w in dd or dd in w:
                cast = _find_cast(os.path.join(root, d))
                if cast:
                    return cast
    return ""


def _default_out(job: Job) -> str:
    base = os.path.splitext(os.path.basename(job.clean or job.audio or "video"))[0]
    return os.path.join(os.path.dirname(os.path.abspath(job.audio)),
                        f"{base}__build")


def _format_for(job: Job) -> str:
    """A concrete format string. 'auto' rotates F1..F10 by queue position so a
    batch of videos each looks distinct; an explicit F-name is honoured.
    'No Filter' -> the effect-free F11 for long videos."""
    f = (job.fmt or "").strip().lower()
    if f in ("no filter", "nofilter", "f11", "no-filter"):
        return "F11_NoFilter"
    if f and f not in ("auto", "auto-rotate", "rotate"):
        return job.fmt
    return _FORMATS[job.index % len(_FORMATS)]


def preflight(job: Job, log=print) -> bool:
    """Validate inputs + library BEFORE the slow work. Sets job.status/message
    and returns True only when the video is safe to build."""
    for label, p in (("clean", job.clean), ("clue", job.clue), ("audio", job.audio)):
        if not p or not os.path.isfile(p):
            job.status, job.message = "error", f"{label} file missing: {p}"
            log("  [X] " + job.message)
            return False

    # every library root: the SSD's Movies folder + the Google-Drive shows
    # (Malcolm, EHC, Modern Family, The Office...). A single root silently hid
    # those shows from the build, which then filled them with other shows' clips.
    movies = job.movies_root or _paths.library_roots()
    if not movies or not any(os.path.isdir(r) for r in _paths.split_roots(movies)):
        job.status, job.message = "error", (
            "movies SSD not found - plug it in (found by volume label "
            f"'{_paths.MOVIES_LABEL}', so any drive letter is fine)")
        log("  [X] " + job.message)
        return False
    job.movies_root = movies
    log(f"  movies library: {movies}")

    # library gate — exactly which episodes the script needs, and are they built
    try:
        res = libcheck.check(job.clue, movies)
    except libcheck.ScriptUnreadable as exc:
        job.status, job.message = "error", f"clue script broken: {exc}"
        log("  [X] " + job.message)
        return False
    log("  " + libcheck.format_report(res).replace("\n", "\n  "))
    if not res["ready"]:
        need = []
        for s in res["shows"]:
            need += [f"{s['show']} {e}" for e in (s["missing"] + s["incomplete"])]
        job.status, job.message = "blocked", "build these first: " + ", ".join(need)
        return False

    # gemini key present? (identity verification during makevideo)
    key = ""
    sfile = os.path.join(HERE, "settings.txt")
    if os.path.isfile(sfile):
        for ln in open(sfile, encoding="utf-8", errors="replace"):
            if "gemini" in ln.lower() and "=" in ln:
                key = ln.split("=", 1)[1].strip()
    key = key or os.environ.get("GEMINI_API_KEY", "")
    if not key:
        log("  [!] gemini key not found in settings.txt - makevideo will run "
            "without identity re-verification (faster, a touch less accurate)")
    else:
        log("  gemini key: present")
    return True


def _child_env() -> dict:
    """Env for the child tools. Two things matter:
    * unbuffered stdout so the GUI/log streams live (else Python buffers ~8 KB);
    * if the Whisper model is already cached, force HuggingFace OFFLINE — its
      online etag re-check on a flaky connection can hang the render for minutes
      (a stalled HTTPS CloseWait). A short etag timeout covers the not-cached case.
    """
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    # UTF-8 stdout/stderr in every child. Windows defaults a piped child to the
    # locale codepage (cp1252), so a child printing a path with a non-Latin-1
    # char (e.g. a video folder named "... → ..." U+2192) dies with
    # UnicodeEncodeError right at the final "done" log — AFTER the render. Force
    # UTF-8 so any Unicode in a path or the script never crashes a subprocess.
    env["PYTHONUTF8"] = "1"
    env["OPENCV_LOG_LEVEL"] = "ERROR"    # YuNet prints '[ WARN:0@..] setPreferableTarget' per face check, flooding the GUI log
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("HF_HUB_ETAG_TIMEOUT", "5")
    cache = os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")
    try:
        if any(d.startswith("models--Systran--faster-whisper")
               for d in os.listdir(cache)):
            env["HF_HUB_OFFLINE"] = "1"
            env["TRANSFORMERS_OFFLINE"] = "1"
    except OSError:
        pass
    return env


def _run(cmd, log, on_proc=None) -> int:
    log("  $ " + " ".join(('"%s"' % c if " " in c else c) for c in cmd))
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)   # Windows: killable tree
    proc = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", bufsize=1,
                            env=_child_env(), creationflags=flags)
    if on_proc:
        on_proc(proc)                              # let the GUI hold it, to Stop
    for line in proc.stdout:                       # stream so the GUI stays live
        log("    " + line.rstrip())
    proc.wait()
    if on_proc:
        on_proc(None)
    return proc.returncode


def _default_bg_folder() -> str:
    """Where the user drops background textures. Desktop\\ProStudio\\backgrounds
    by default (works on OneDrive-redirected desktops too); created on demand."""
    for base in (os.path.join(os.path.expanduser("~"), "Desktop", "ProStudio"),
                 os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop",
                              "ProStudio")):
        if os.path.isdir(base):
            bg = os.path.join(base, "backgrounds")
            os.makedirs(bg, exist_ok=True)
            return bg
    bg = os.path.join(os.path.expanduser("~"), "Desktop", "ProStudio",
                      "backgrounds")
    os.makedirs(bg, exist_ok=True)
    return bg


def _mini_library(beats: list, movies_root: str) -> dict:
    """Just the catalogues for the episodes/films this clue references — enough
    for a punch-in/cold-open to resolve its source clip, without loading a whole
    show. TV episodes are matched by SxxExx; FILMS by title tokens (a movie shot
    carries season_episode 'unknown', so it has no episode marker to match)."""
    import glob
    from media_index import catalog, plan
    eps = set()
    movie_titles = []
    for b in beats:
        for s in (b.get("shots") or []):
            se = str(s.get("season_episode") or "").upper().replace(" ", "")
            if se and se != "UNKNOWN":
                eps.add(se)
            else:                                  # a film — resolve by title
                toks = plan._title_tokens(s.get("source") or "")
                if toks:
                    movie_titles.append(toks)

    def epof(path):
        m = re.search(r"s(\d{1,2})\s*e(\d{1,2})", os.path.basename(path), re.I)
        return f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}" if m else ""

    lib = {}
    # Only the show folders this script names (not every show on every drive:
    # the same SxxExx numbers exist in all of them, and each extra catalogue is
    # a slow Google-Drive read).
    from media_index import libcheck as _lc
    folders = {}
    for root in _paths.split_roots(movies_root):
        try:
            for d in os.listdir(root):
                if os.path.isdir(os.path.join(root, d)):
                    folders.setdefault(d.lower(), os.path.join(root, d))
        except OSError:
            continue
    want = {str(s.get("source") or "").strip() for b in beats
            for s in (b.get("shots") or []) if s.get("source")}
    picked = {folders[m] for w in want
              for m in [_lc._match_show(w, set(folders))] if m}
    search = sorted(picked) if picked else _paths.split_roots(movies_root)
    cats = [c for root in search
            for c in glob.glob(os.path.join(root, "**", "*.catalog.json"),
                               recursive=True)]
    for c in cats:
        take = epof(c) in eps
        if not take and movie_titles:             # films: title-token match
            ctoks = plan._title_tokens(os.path.basename(c))
            take = any(plan._tokens_contain(ctoks, w) for w in movie_titles)
        if take:
            catalog._merge_unique(lib, catalog.load_library(c), c)
    return lib


def _apply_intro_hooks(job: Job, final: str, log) -> None:
    """Put the cold-open and/or intro punch-ins onto the delivered final.mp4."""
    import json
    from media_index import jobs as mi_jobs, punchins
    beats = mi_jobs.read_build_beats(job.out, job.clue)
    library = _mini_library(beats, job.movies_root)
    if not library:
        log("  intro hooks: koi referenced-episode catalog nahi mila — skip")
        return
    scenes = []
    tlp = os.path.join(job.out, "timeline.json")
    if os.path.isfile(tlp):
        try:
            scenes = (json.load(open(tlp, encoding="utf-8")) or {}).get("scenes", [])
        except (OSError, ValueError):
            scenes = []
    tmp = final + ".hook.mp4"
    cold_spec = (punchins.find_cold_open(beats, library, log=log)
                 if job.cold_open else {})
    exclude = ({punchins._line_key(cold_spec["video"], cold_spec["line_start"])}
               if cold_spec else set())
    picks = []
    if job.intro_punch and scenes:
        picks = punchins.find_intro_punches(beats, scenes, library,
                                            exclude_lines=exclude, log=log)
        if not picks:
            log("  intro punch-ins: intro me koi resolvable hook line nahi — skip")
    elif job.intro_punch:
        log("  intro punch-ins: timeline scenes nahi mile — skip")
    if job.cold_open and not cold_spec:
        log("  cold-open: koi opening hook line resolve nahi hui — skip")
    # cold-open + punch-ins in ONE encode (the old stream-copy joins drifted the
    # picture 2-3 s off the voice)
    if cold_spec or picks:
        punchins.apply_hooks(final, cold_spec, picks, tmp, log=log)
        os.replace(tmp, final)


def _apply_kinetic_text(job: Job, final: str, log, on_proc=None) -> None:
    """Add VText kinetic text to `final`. Manual instruction file if provided,
    else auto-generated from the clean narration + clue."""
    inst = (job.text_file or "").strip()
    if inst and os.path.isfile(inst):
        log(f"  kinetic text: using your instruction file ({os.path.basename(inst)})")
    else:
        from media_index import autotext, jobs as mi_jobs
        clean = ""
        try:
            with open(job.clean, encoding="utf-8-sig") as f:
                clean = f.read()
        except OSError:
            pass
        beats = []
        try:
            beats = mi_jobs.read_build_beats(job.out, job.clue)
        except Exception:
            pass
        inst = os.path.join(job.out, "auto_text.txt")
        if not autotext.generate(clean, beats, inst, log=log):
            log("  kinetic text: narration se koi text moment nahi nikla — skip")
            return
    out = os.path.join(job.out, "final_texted.mp4")
    # Popen (not subprocess.run) + on_proc so a Stop click can kill the VText
    # pass too — on a long video its text re-encode runs many minutes. Inherit
    # stdout/env so VText's progress still shows and the launcher's VTEXT_FFMPEG
    # (NVENC-capable ffmpeg) is preserved.
    vproc = subprocess.Popen(
        [sys.executable, VTEXT, "--video", final, "--script", job.clean,
         "--instructions", inst, "--out", out],
        cwd=os.path.dirname(VTEXT), env=_child_env(),
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    if on_proc:
        on_proc(vproc)
    vproc.wait()
    if on_proc:
        on_proc(None)
    rc = vproc.returncode
    if rc == 0 and os.path.isfile(out):
        os.replace(out, final)
        log("  kinetic text: on-screen text composited")
    else:
        log(f"  kinetic text: vtext returncode {rc} — final kept as-is")


OMNIVOICE_DIR = os.environ.get("OMNIVOICE_DIR", r"D:\omnivoice-wrapper")
GPU_SLOT_DIR, GPU_SLOTS = r"D:\Lib\libjobs\gpu", 3          # the indexing workers' whisper slots (catalog_refine.gpu_slot)


class _GpuSlots:
    """Hold EVERY indexing whisper slot while OmniVoice speaks. 2026-10-12: 10 indexing workers + OmniVoice filled the
    12 GB card (0 MB free -> OmniVoice 'degraded', earlier a CUDA illegal-memory crash). Workers just wait ('waiting for
    GPU') and their whisper models are released when they leave a slot, so the VRAM frees within minutes."""

    def __init__(self, log):
        self.log, self.fhs = log, []

    def __enter__(self):
        import msvcrt, threading
        os.makedirs(GPU_SLOT_DIR, exist_ok=True)
        # 'reserved' stops the workers from TAKING new slots (they kept winning the race: 0/3 for 24 min); the ones
        # inside a slot finish their whisper pass and leave. Touched every minute so a crash leaves it stale (30 min).
        self.reserve = os.path.join(GPU_SLOT_DIR, "reserved")
        self.stop = threading.Event()

        def touch():
            while not self.stop.is_set():
                try:
                    with open(self.reserve, "w") as f:
                        f.write(str(os.getpid()))
                except OSError:
                    pass
                self.stop.wait(60)
        threading.Thread(target=touch, daemon=True).start()
        t0, last = time.time(), 0.0
        while len(self.fhs) < GPU_SLOTS and time.time() - t0 < 1800:
            for i in range(GPU_SLOTS):
                if any(getattr(f, "_slot", -1) == i for f in self.fhs):
                    continue
                f = open(os.path.join(GPU_SLOT_DIR, f"slot{i}.lock"), "a+")
                try:
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    f._slot = i
                    self.fhs.append(f)
                except OSError:
                    f.close()
            if len(self.fhs) < GPU_SLOTS:
                if time.time() - last > 60:
                    self.log(f"  [voiceover] pausing the indexing whisper for the voice: {len(self.fhs)}/{GPU_SLOTS} GPU slots free ...")
                    last = time.time()
                time.sleep(2)
        if len(self.fhs) < GPU_SLOTS:
            self.log(f"  [voiceover] indexing still holds {GPU_SLOTS - len(self.fhs)} GPU slot(s) after 30 min - "
                     "trying the voice anyway (OmniVoice waits for free VRAM itself)")
        return self

    def __exit__(self, *a):
        import msvcrt
        self.stop.set()
        try:
            os.remove(self.reserve)
        except OSError:
            pass
        for f in self.fhs:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            f.close()
        self.fhs = []


def _omnivoice_state(api):
    """-> (state, detail): 'ready' | 'busy_vram' (VRAM full, server fine) | 'broken' (CUDA error / generation failing) |
    'down' (nothing answering)."""
    import requests
    try:
        r = requests.get(api + "/api/ready", timeout=8)
    except Exception:
        return "down", "no answer"
    if r.status_code == 200:
        return "ready", ""
    try:
        j = r.json()
    except ValueError:
        return "broken", r.text[:200]
    if j.get("consecutive_failures") or (j.get("last_error") and not j.get("generation_ok", True)):
        return "broken", str(j.get("last_error") or j.get("reason"))[:200]
    return "busy_vram", str(j.get("reason") or j.get("detail"))[:200]


def _omnivoice_restart(log):
    """kill every OmniVoice app.py (and its run.bat console) and start run.bat again in its own window."""
    try:
        import psutil
        for p in psutil.process_iter(["cmdline"]):
            c = " ".join(p.info["cmdline"] or []).lower()
            if OMNIVOICE_DIR.lower() in c and ("app.py" in c or "run.bat" in c):
                try:
                    p.kill()
                except Exception:
                    pass
        time.sleep(5)
    except ImportError:
        pass
    bat = os.path.join(OMNIVOICE_DIR, "run.bat")
    if not os.path.isfile(bat):
        raise RuntimeError(f"OmniVoice not found ({bat})")
    log("  [voiceover] starting the OmniVoice server (model load ~1-3 min) ...")
    # 'start' hands the server to its own console and the cmd exits at once, so the server is NOT in the build's
    # process tree: the GUI's Stop (taskkill /T) must not kill a voice server other videos keep using.
    subprocess.Popen(f'cmd /c start "OmniVoice" /min cmd /c "{bat}"', cwd=OMNIVOICE_DIR,
                     env={**os.environ, "OMNIVOICE_OPEN_BROWSER": "0"},
                     creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)


def _omnivoice_ensure(api, log):
    """make the server answer 'ready': start it if down, restart it if broken, wait while the VRAM frees."""
    st, why = _omnivoice_state(api)
    if st in ("down", "broken"):
        if st == "broken":
            log(f"  [voiceover] OmniVoice server is in an error state ({why[:90]}) - restarting it")
        _omnivoice_restart(log)
    t0, last = time.time(), 0.0
    while st != "ready" and time.time() - t0 < 900:
        time.sleep(5)
        st, why = _omnivoice_state(api)
        if time.time() - last > 60 and st != "ready":
            log(f"  [voiceover] waiting for OmniVoice ({st}: {why[:80]})")
            last = time.time()
    if st != "ready":
        raise RuntimeError(f"OmniVoice voice server not ready after 15 min ({st}: {why}) - "
                           "open omnivoice-wrapper/run.bat and check its window")
    log(f"  [voiceover] OmniVoice ready")


def _generate_voiceover(job: "Job", out_wav: str, log=print) -> str:
    """No voiceover was provided -> generate one with the local OmniVoice server (voice-cloned from `job.voice_ref`,
    in `job.language`). OmniVoice runs in its OWN Python-3.12 env; we only talk to it over HTTP here. The server is
    started / restarted automatically (omnivoice-wrapper/run.bat) and the indexing workers' whisper is paused
    while it speaks. Override the address with the OMNIVOICE_API env var (default http://127.0.0.1:8001)."""
    api = (os.environ.get("OMNIVOICE_API") or "http://127.0.0.1:8001").rstrip("/")
    voice = (job.voice_ref or "").strip()
    if not voice or not os.path.isfile(voice):
        raise RuntimeError("auto-voiceover is on but no Voice sample was chosen"
                           + (f" (file not found: {voice})" if voice else "")
                           + " - pick the narrator sample in the 'Voice sample' row (keep 'Voiceover audio' empty).")
    # one voiceover at a time across parallel builds (batch phase 1 runs 2 videos): two builds each holding some of
    # the GPU slots would wait on each other; OmniVoice also speaks one clip at a time anyway
    import msvcrt
    os.makedirs(GPU_SLOT_DIR, exist_ok=True)
    with open(os.path.join(GPU_SLOT_DIR, "voice.lock"), "a+") as vl:
        t0, last = time.time(), 0.0
        while True:
            try:
                vl.seek(0)
                msvcrt.locking(vl.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.time() - last > 60:
                    log(f"  [voiceover] waiting for the other video's voiceover ({(time.time() - t0) / 60:.0f} min)")
                    last = time.time()
                time.sleep(3)
        try:
            with _GpuSlots(log):
                _omnivoice_ensure(api, log)
                return _generate_voiceover_now(job, out_wav, log, api)
        finally:
            try:
                vl.seek(0)
                msvcrt.locking(vl.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass


def _generate_voiceover_now(job: "Job", out_wav: str, log, api) -> str:
    import requests
    voice = (job.voice_ref or "").strip()
    if not voice or not os.path.isfile(voice):
        raise RuntimeError("auto-voiceover is on but no Voice sample was chosen"
                           + (f" (file not found: {voice})" if voice else "")
                           + " - pick the narrator sample in the 'Voice sample' row (keep 'Voiceover audio' empty).")
    try:
        with open(job.clean, encoding="utf-8-sig") as f:
            text = f.read().strip()
    except OSError as exc:
        raise RuntimeError(f"cannot read the clean script: {exc}")
    if not text:
        raise RuntimeError("the clean script is empty — nothing to speak.")
    if (job.language or "").lower() in ("hi", "hin", "hindi"):
        # Hindi/Hinglish: numbers spelled out, 32 steps, ~15 s pieces each
        # checked by OmniVoice's own transcriber and re-made if words drop.
        from media_index import hindi_voice
        return hindi_voice.generate(api, voice, text, out_wav, log=log)
    log(f"  [voiceover] OmniVoice: speaking {len(text.split())} words in "
        f"'{job.language}', voice-cloned from {os.path.basename(voice)} ...")
    t0 = time.time()
    with open(voice, "rb") as vf:
        r = requests.post(api + "/api/tts/async",
                          data={"text": text, "language": job.language},
                          files={"voice": (os.path.basename(voice), vf)}, timeout=120)
    r.raise_for_status()
    jid = r.json().get("job_id")
    if jid is None:
        raise RuntimeError(f"OmniVoice did not return a job id: {r.text[:200]}")
    info = {}
    while True:
        time.sleep(3)
        info = requests.get(f"{api}/api/jobs/{jid}", timeout=20).json()
        st = info.get("status")
        if st == "done":
            break
        if st in ("error", "failed"):
            raise RuntimeError(f"OmniVoice job failed: {info.get('info') or info}")
        if time.time() - t0 > 3600:
            raise RuntimeError("OmniVoice generation timed out (>60 min)")
    d = requests.get(f"{api}/api/jobs/{jid}/download", timeout=120)
    d.raise_for_status()
    os.makedirs(os.path.dirname(os.path.abspath(out_wav)) or ".", exist_ok=True)
    with open(out_wav, "wb") as f:
        f.write(d.content)
    if not os.path.isfile(out_wav) or os.path.getsize(out_wav) < 2000:
        raise RuntimeError("OmniVoice returned no audio")
    log(f"  [voiceover] done: {info.get('audio_sec', '?')}s of audio in "
        f"{time.time() - t0:.0f}s -> {os.path.basename(out_wav)}")
    return out_wav


def _build_v2(job: "Job", log, on_proc, stop, until=None) -> "Job":
    """Pro engine (editv2). Runs as a child process so Stop kills it; the build
    folder is resumable stage by stage. The clue is generated inside (agy Claude)
    — a clue file in the job is ignored."""
    t0 = time.time()
    name = os.path.splitext(os.path.basename(job.clean))[0]
    job.out = job.out or os.path.join(os.path.dirname(os.path.abspath(job.clean)), name + "_v2build")
    cmd = [sys.executable, "-u", "-m", "editv2", "build", job.clean, "--out", job.out,
           "--lang", (job.language or "en"), "--look", job.look or "auto"]
    if job.audio:
        cmd += ["--audio", job.audio]
    elif job.auto_voice:
        cmd += ["--voice-ref", job.voice_ref]
        if not os.path.exists(os.path.join(job.out, "audio.wav")):
            # warm the voice server up while the clue (~2-3 min) is being built; the voice stage itself still checks,
            # restarts a broken server and pauses the indexing whisper (_generate_voiceover)
            api = (os.environ.get("OMNIVOICE_API") or "http://127.0.0.1:8001").rstrip("/")
            try:
                if _omnivoice_state(api)[0] == "down":
                    _omnivoice_restart(log)
            except Exception as exc:
                log(f"  [voiceover] could not pre-start OmniVoice ({exc}) - the voice stage will retry")
    if job.save_dir:
        cmd += ["--save", job.save_dir]
    if until:
        cmd += ["--until", until]
    log(f"  [v2] pro engine, look {job.look}, language {job.language}" + (f" - {until} phase" if until else ""))
    proc = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace",
                            env={**_child_env(), "PYTHONIOENCODING": "utf-8"})   # HF offline when cached + UTF-8
    if on_proc:
        on_proc(proc)
    if stop():                                     # Stop pressed while the child was being created: it missed the kill
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    last, tail = "", []
    job.tail = tail
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            tail.append(line)
            del tail[:-40]
        # drop only Python/library warning noise; the old `"warn" not in line` also hid QA 'WARN' verdicts and reasons
        if line and not re.search(r"\b\w+Warning\b|warnings\.warn|^\s*warnings\.|^\[ ?WARN:\d|cv::dnn", line):
            log(line)
            last = line
    proc.wait()
    if on_proc:
        on_proc(None)
    if stop():
        job.status, job.message = "stopped", "stopped — Run again to resume"
    elif proc.returncode == 0 and last.startswith("ONLINE-DONE"):
        job.status, job.message = "online_done", f"internet work done in {(time.time() - t0) / 60:.0f} min"
    elif proc.returncode == 0 and last and os.path.isfile(last.strip()):
        job.status, job.video = "done", last.strip()
        qa = os.path.join(job.out, "QA.txt")
        verdict = open(qa, encoding="utf-8").readline().strip() if os.path.isfile(qa) else ""
        job.message = f"{os.path.basename(job.video)} in {(time.time() - t0) / 60:.0f} min — {verdict}"
    else:
        job.status, job.message = "error", f"v2 engine failed: {last[:300]}"
    return job


# ---------------------------------------------------------------------------------------------------------------
# Two-phase batch (user 2026-10-13): a 30-40 video night queue must not die on an internet drop. Phase 1 does every
# step that needs the internet (clue, plan, cut, look + footage from Google Drive) for ALL videos, 2 at a time,
# waiting out outages; then a GREEN SIGNAL; phase 2 renders / mixes / QAs everything fully offline.
NET_ERR = re.compile(r"LLM failed|HTTP Error 5\d\d|URLError|urlopen|proxy|Connection|timed out|getaddrinfo|"
                     r"incomplete read|cannot read|vision unavailable|RemoteDisconnected|10054|10060|11001|"
                     r"Temporary failure|Max retries|not readable", re.I)
GREEN_FILE = os.path.join(os.path.expanduser("~"), "Desktop", "MOVIE EDITOR - INTERNET KA KAAM POORA.txt")
BATCH_FILE = os.path.join(HERE, "editv2", "cache", "last_batch.json")


def internet_up() -> bool:
    import socket
    for host in ("www.google.com", "oauth2.googleapis.com"):
        try:
            socket.create_connection((host, 443), timeout=6).close()
            return True
        except OSError:
            pass
    return False


def build_batch(jobs, log=print, on_proc=None, should_stop=None, on_status=None, parallel=2, wait_hours=12):
    """Run a queue of jobs. v2 jobs go through the two phases above; others run as before. Never raises."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    stop = should_stop or (lambda: False)
    status = on_status or (lambda job, st: None)
    lock = threading.Lock()
    procs = {}

    def tlog(prefix):
        def f(*a):
            with lock:
                log(prefix + " ".join(str(x) for x in a))
        return f

    def reg(job):                                  # every child is tracked so Stop can kill all of them
        def f(p):
            with lock:
                if p is None:
                    procs.pop(id(job), None)
                else:
                    procs[id(job)] = p
            if on_proc:
                on_proc(p)
        return f

    def stopper():
        while not done_flag.is_set():
            if stop():
                with lock:
                    live = list(procs.values())
                for p in live:
                    subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
                return
            time.sleep(1.5)
    done_flag = threading.Event()
    threading.Thread(target=stopper, daemon=True).start()

    v2 = [j for j in jobs if (j.engine or "") == "v2"]
    other = [j for j in jobs if (j.engine or "") != "v2"]
    try:
        if os.path.exists(GREEN_FILE):
            os.remove(GREEN_FILE)
    except OSError:
        pass
    # ---- phase 1: everything that needs the internet ----
    if v2:
        log(f"\n=== PHASE 1 / 2 - internet work for {len(v2)} video(s) ({parallel} at a time): clue, plan, cut, look ===")
    pending, tries, t_start = list(v2), {id(j): 0 for j in v2}, time.time()
    while pending and not stop():
        if not internet_up():
            if time.time() - t_start > wait_hours * 3600:
                log(f"  internet down for too long - {len(pending)} video(s) left for the next Run")
                break
            log(f"  no internet - waiting (checked {time.strftime('%H:%M')}); {len(pending)} video(s) still need it")
            for _ in range(60):
                if stop():
                    break
                time.sleep(5)
            continue

        def one(job):
            status(job, "building")
            _build_v2(job, tlog(f"[V{job.index + 1}] "), reg(job), stop, until="online")
            return job
        with ThreadPoolExecutor(max(1, parallel)) as ex:
            ran = list(ex.map(one, pending))
        retry = []
        for j in ran:
            if j.status == "online_done":
                status(j, "online")
                log(f"  [V{j.index + 1}] internet work done ✓")
            elif j.status == "stopped" or stop():
                status(j, "pending")
            else:
                tries[id(j)] += 1
                why = " ".join((getattr(j, "tail", []) or [])[-15:]) + " " + (j.message or "")
                if NET_ERR.search(why) and tries[id(j)] < 6:
                    status(j, "pending")
                    log(f"  [V{j.index + 1}] stopped by an internet/proxy problem - will continue when it is back "
                        f"(try {tries[id(j)]}/5)")
                    retry.append(j)
                else:
                    status(j, "failed")
                    log(f"  [V{j.index + 1}] ✗ {j.message}")
        pending = retry
        if pending and not stop():
            log(f"  {len(pending)} video(s) to retry in 3 min")
            for _ in range(36):
                if stop():
                    break
                time.sleep(5)
    ready = [j for j in v2 if j.status == "online_done"]
    if v2 and not stop() and not pending:
        msg = (f"GREEN SIGNAL {time.strftime('%Y-%m-%d %H:%M')}: internet work is done for {len(ready)}/{len(v2)} "
               "video(s). From now on the rest (render, mix, QA) needs NO internet.")
        log("\n🟢 " + msg)
        try:
            with open(GREEN_FILE, "w", encoding="utf-8") as f:
                f.write(msg + "\n\n" + "\n".join(os.path.basename(os.path.dirname(j.clean)) for j in ready) + "\n")
        except OSError:
            pass
        status(None, "green")
    # ---- phase 2: offline ----
    if ready and not stop():
        log(f"\n=== PHASE 2 / 2 - render, mix, QA for {len(ready)} video(s) (offline) ===")
    for j in ready:
        if stop():
            break
        status(j, "building")
        _build_v2(j, tlog(f"[V{j.index + 1}] "), reg(j), stop)
        status(j, "done" if j.status == "done" else ("pending" if j.status == "stopped" else "failed"))
        log(f"  [V{j.index + 1}] {'✓' if j.status == 'done' else '✗'} {j.message}")
    for j in other:                                    # classic engine: unchanged, one by one
        if stop():
            break
        status(j, "building")
        build(j, log=log, on_proc=on_proc, should_stop=stop)
        status(j, "done" if j.status == "done" else "failed")
    done_flag.set()
    return jobs


def build(job: Job, log=print, on_proc=None, should_stop=None) -> Job:
    """The whole pipeline for ONE video. Never raises — reports via job.status.

    `on_proc(proc_or_None)` hands the GUI the running child so a Stop button can
    kill it; `should_stop()` is polled between stages so a stop lands cleanly. A
    stopped video keeps its half-done build folder, so the next Run resumes it
    (makevideo reuses cut clips, prostudio reuses rendered shots)."""
    _stop = should_stop or (lambda: False)
    t0 = time.time()
    job.status = "running"
    if job.engine == "v2":
        return _build_v2(job, log, on_proc, _stop)
    if _stop():
        job.status, job.message = "stopped", "stopped before it started"
        return job
    # Library gate FIRST: a script whose footage is missing is blocked before a
    # 2-3 min voiceover is generated for nothing.
    if not job.audio and job.auto_voice and job.clue and os.path.isfile(job.clue):
        try:
            _roots = job.movies_root or _paths.library_roots()
            _res = libcheck.check(job.clue, _roots)
            if not _res.get("ready"):
                job.status = "blocked"
                job.message = "library not ready (before voiceover): " + \
                    libcheck.format_report(_res).replace("\n", " | ")[:400]
                log("  [X] " + job.message)
                return job
        except Exception:
            pass                                   # the full preflight still runs later
    # auto-voiceover: no audio given -> generate it with OmniVoice, THEN preflight
    if not job.audio and job.auto_voice:
        job.out = job.out or _default_out(job)
        os.makedirs(job.out, exist_ok=True)
        wav = os.path.join(job.out, "audio.wav")
        try:
            _generate_voiceover(job, wav, log)
            job.audio = wav
        except Exception as exc:
            job.status, job.message = "error", f"auto-voiceover failed: {exc}"
            log("  [X] " + job.message)
            return job
    if not preflight(job, log):
        return job

    job.out = job.out or _default_out(job)
    os.makedirs(job.out, exist_ok=True)

    # ---- stage 4: pick the real clips + align to the voiceover -------------- #
    log("\n  [makevideo] cutting the right clips, aligning to the voiceover...")
    mv = [sys.executable, "-m", "media_index", "makevideo", job.clue,
          job.movies_root, job.audio, "--narration", job.clean, "--out", job.out,
          # prostudio renders the final from the scene folders below, so don't
          # waste ~10 min rendering makevideo's own video.mp4 (it's discarded).
          "--no-final-render"]
    if (job.language or "en").lower() not in ("en", "eng", "english"):
        mv += ["--language", job.language]
        log(f"  language: {job.language} — English library, {job.language} "
            "voiceover; whisper listens with the multilingual model")
    # Cold-open / intro punch-ins are NOT passed to makevideo: prostudio
    # re-renders from the scene folders, so anything makevideo splices into its
    # own video.mp4 is discarded. They are applied AFTER prostudio, on final.mp4
    # (see the intro-hooks stage below). Only log the intent here.
    if job.intro_punch:
        log("  intro punch-ins ON — first 3 min ke famous dialogues pe original "
            "awaaz (final video pe lagenge, prostudio ke baad)")
    if job.cold_open:
        log("  cold-open ON — video pehli famous line se khulegi (final pe, "
            "prostudio ke baad)")
    if job.ken_burns:
        mv += ["--ken-burns"]
        log("  Ken Burns ON — har still pe slow zoom/pan motion (static nahi)")
    # Clip selection never uses the paid Gemini API any more: clips are placed
    # from the library, then (if Verify is on) checked for FREE by agy below,
    # before the render.
    mv += ["--no-verify"]
    # Fresh build every time: stale scene folders / final.mp4 / prostudio's
    # _work cache from an earlier run would otherwise be REUSED (prostudio
    # --resume skips a video that has final_report.json), so a rebuilt video
    # could come out identical to the old wrong one. Keep only the voiceover.
    import shutil as _sh
    for n in os.listdir(job.out):
        p = os.path.join(job.out, n)
        if n.startswith("scene_") or n in ("final.mp4", "final_report.json",
                                            "final_work", "timeline.json",
                                            "manifest.json", "verify_report.json",
                                            "build_beats.json"):
            if os.path.isdir(p):
                _sh.rmtree(p, ignore_errors=True)
            else:
                try:
                    os.remove(p)
                except OSError:
                    pass
    rc = _run(mv, log, on_proc=on_proc)
    if _stop():
        job.status, job.message = "stopped", "stopped during clip cutting (re-Run to resume)"
        return job
    if rc != 0:
        job.status, job.message = "error", "makevideo failed (see log)"
        return job
    scenes = [d for d in os.listdir(job.out)
              if d.startswith("scene_") and os.path.isdir(os.path.join(job.out, d))]
    if not scenes:
        job.status, job.message = "error", "makevideo produced no scene folders"
        return job
    log(f"  makevideo done - {len(scenes)} scene folders built")

    # ---- stage 4b: FREE clip verification (agy Gemini rotation) ------------- #
    # Every placed clip is checked against its scene's narration + the show the
    # script expects BEFORE the render. Wrong-show / contradicting clips are set
    # aside; a video whose clips are largely from the wrong show is NOT rendered.
    if job.verify:
        from media_index import agyverify
        log("\n  [agy verify] checking every clip against the narration (free)...")
        try:
            agyverify.verify_build(job.out, job.clue, log=log)
        except agyverify.VerifyFailed as exc:
            job.status, job.message = "error", f"verify blocked this video: {exc}"
            log("  [X] " + job.message)
            return job
        except Exception as exc:                    # verify must never kill a build
            log(f"  agy verify skipped ({type(exc).__name__}: {exc})")
        if _stop():
            job.status, job.message = "stopped", "stopped after verify"
            return job

    # ---- stage 5: pro effects + final render -------------------------------- #
    fmt = _format_for(job)
    final = os.path.join(job.out, "final.mp4")
    log(f"\n  [prostudio] applying effects (format {fmt}, {job.resolution})...")
    ps = [sys.executable, PROSTUDIO, "--scenes", job.out, "--audio", job.audio,
          "--script", job.clean, "--out", final, "--format", fmt,
          "--resolution", job.resolution, "--resume"]   # reuse already-rendered
    ps += ["--text"] if job.text else ["--no-text"]
    # Premium frame is applied INSIDE prostudio's single final encode (not a
    # separate ~20-min re-encode pass), so all effects stay inside the card.
    if job.frame:
        from media_index import framing as _fr
        bgdir = job.bg_folder or _default_bg_folder()
        bg = _fr.pick_background(bgdir, os.path.basename(job.clean or job.clue))
        if bg:
            ps += ["--frame-bg", bg]
            log(f"  premium frame ON — background: {os.path.basename(bg)} "
                "(baked into the final render, one encode)")
        else:
            log(f"  frame: {bgdir} me koi background image nahi — skip")
    rc = _run(ps, log, on_proc=on_proc)
    if _stop():
        job.status, job.message = "stopped", "stopped during effects render (re-Run to resume)"
        return job
    if rc != 0:
        job.status, job.message = "error", "prostudio render failed (see log)"
        return job

    if not os.path.isfile(final):
        job.status, job.message = "error", "prostudio ran but no final.mp4"
        return job

    # (premium frame is now baked into prostudio's final encode above — no
    #  separate re-encode pass)

    # ---- stage 6: intro hooks on the FINAL video ---------------------------- #
    # prostudio just rebuilt final.mp4 from the scene folders, so the cold-open /
    # punch-ins makevideo put on video.mp4 are gone. Apply them HERE, on the file
    # the user actually receives. Best-effort: a failure keeps final.mp4 as-is.
    if job.cold_open or job.intro_punch:
        try:
            _apply_intro_hooks(job, final, log)
        except Exception as exc:
            log(f"  intro hooks skip ({type(exc).__name__}: {exc}) — final kept as-is")

    # ---- stage 7: kinetic on-screen text (VText) — the LAST layer ------------ #
    # Competitor-style text at important moments, audio-synced. Uses the user's
    # hand-made instruction file when given (best punch), else auto-generates one
    # from the narration. Runs last so text sits over the finished composition.
    if job.kinetic_text:
        try:
            _apply_kinetic_text(job, final, log, on_proc=on_proc)
        except Exception as exc:
            log(f"  kinetic text skip ({type(exc).__name__}: {exc}) — final kept as-is")

    # deliver the finished video to the folder the user chose (a friendly name,
    # not final.mp4), so a batch lands together where they want it.
    delivered = final
    if job.save_dir:
        try:
            os.makedirs(job.save_dir, exist_ok=True)
            name = os.path.splitext(os.path.basename(job.clean))[0] or "video"
            dest = os.path.join(job.save_dir, name + ".mp4")
            if os.path.abspath(dest) != os.path.abspath(final):
                import shutil
                shutil.copy2(final, dest)
            delivered = dest
        except OSError as exc:
            log(f"  ! could not save to {job.save_dir} ({exc}); kept it at {final}")

    job.status, job.video, job.message = "done", delivered, \
        f"finished in {int(time.time() - t0)}s  -  format {fmt}"
    log(f"\n  [OK] VIDEO READY: {delivered}")
    return job


def run_queue(jobs: list, log=print) -> list:
    """Process videos one at a time (serial — the footage SSD hates parallel)."""
    for i, job in enumerate(jobs):
        job.index = i
        log(f"\n{'='*66}\n  VIDEO {i+1}/{len(jobs)}  -  {os.path.basename(job.clean)}\n{'='*66}")
        build(job, log)
        log(f"  -> {job.status.upper()}: {job.message}")
    log(f"\n{'='*66}\n  QUEUE DONE  -  " +
        ", ".join(f"{s}={sum(1 for j in jobs if j.status==s)}"
                  for s in ("done", "blocked", "error")) + f"\n{'='*66}")
    return jobs


# --------------------------------------------------------------------------- #
#  CLI                                                                         #
# --------------------------------------------------------------------------- #
def _cli(argv=None):
    p = argparse.ArgumentParser(description="ProStudio Launcher (headless)")
    p.add_argument("--clean"); p.add_argument("--clue"); p.add_argument("--audio")
    p.add_argument("--out", default="")
    p.add_argument("--save-dir", default="", help="folder to deliver the finished video into")
    p.add_argument("--movies", default="", help="movies root (auto by SSD label if blank)")
    p.add_argument("--format", default="auto")
    p.add_argument("--resolution", default="1080p", choices=["1080p", "4K"])
    p.add_argument("--text", action="store_true")
    p.add_argument("--verify", action="store_true", help="re-check clips with Gemini (costs API; default off)")
    p.add_argument("--verify-intro-min", type=int, default=0, help="verify only the first N minutes")
    p.add_argument("--language", "--lang", default="en", dest="language",
                   help="script/voiceover language: en (default), pt, fr, es, de, ... or auto")
    p.add_argument("--intro-punch-ins", dest="intro_punch", action="store_true",
                   help="first 3 min: famous lines me original show audio bajao (hook boost)")
    p.add_argument("--cold-open", dest="cold_open", action="store_true",
                   help="video ko pehli famous line (original awaaz, 5-8s) se kholo")
    p.add_argument("--ken-burns", dest="ken_burns", action="store_true",
                   help="har still pe slow zoom/pan motion")
    p.add_argument("--frame", dest="frame", action="store_true",
                   help="premium framed look: footage ko rounded card + textured "
                        "background me daalo (bg auto Desktop\\ProStudio\\backgrounds se)")
    p.add_argument("--bg-folder", dest="bg_folder", default="",
                   help="background images folder (blank = Desktop\\ProStudio\\backgrounds)")
    p.add_argument("--kinetic-text", dest="kinetic_text", action="store_true",
                   help="competitor-style on-screen text (VText), audio-synced")
    p.add_argument("--text-file", dest="text_file", default="",
                   help="hand-made VText instruction file; blank = auto-generate from script")
    p.add_argument("--auto-voice", dest="auto_voice", action="store_true",
                   help="no --audio? generate the voiceover with the local OmniVoice "
                        "server (needs --voice-ref)")
    p.add_argument("--voice-ref", dest="voice_ref", default="",
                   help="a short sample of the narrator's voice to clone for --auto-voice")
    p.add_argument("--queue", help="jobs.json: [{clean,clue,audio,out?}, ...]")
    a = p.parse_args(argv)

    if a.queue:
        raw = json.load(open(a.queue, encoding="utf-8"))
        jobs = [Job(clean=j["clean"], clue=j["clue"], audio=j["audio"],
                    out=j.get("out", ""), save_dir=j.get("save_dir", a.save_dir),
                    movies_root=j.get("movies", a.movies),
                    fmt=j.get("format", a.format),
                    resolution=j.get("resolution", a.resolution),
                    text=j.get("text", a.text), verify=j.get("verify", a.verify), verify_intro_min=j.get("verify_intro_min", a.verify_intro_min),
                    language=j.get("language", getattr(a, "language", "en")),
                    intro_punch=j.get("intro_punch", getattr(a, "intro_punch", False)),
                    cold_open=j.get("cold_open", getattr(a, "cold_open", False)),
                    ken_burns=j.get("ken_burns", getattr(a, "ken_burns", False)),
                    frame=j.get("frame", getattr(a, "frame", False)),
                    bg_folder=j.get("bg_folder", getattr(a, "bg_folder", "")),
                    kinetic_text=j.get("kinetic_text", getattr(a, "kinetic_text", False)),
                    text_file=j.get("text_file", getattr(a, "text_file", "")),
                    auto_voice=j.get("auto_voice", getattr(a, "auto_voice", False)),
                    voice_ref=j.get("voice_ref", getattr(a, "voice_ref", ""))) for j in raw]
    elif a.clean and a.clue and (a.audio or a.auto_voice):
        jobs = [Job(clean=a.clean, clue=a.clue, audio=a.audio, out=a.out,
                    save_dir=a.save_dir, movies_root=a.movies, fmt=a.format,
                    resolution=a.resolution, text=a.text, verify=a.verify, verify_intro_min=a.verify_intro_min,
                    language=getattr(a, "language", "en"),
                    intro_punch=getattr(a, "intro_punch", False),
                    cold_open=getattr(a, "cold_open", False),
                    ken_burns=getattr(a, "ken_burns", False),
                    frame=getattr(a, "frame", False),
                    bg_folder=getattr(a, "bg_folder", ""),
                    kinetic_text=getattr(a, "kinetic_text", False),
                    text_file=getattr(a, "text_file", ""),
                    auto_voice=getattr(a, "auto_voice", False),
                    voice_ref=getattr(a, "voice_ref", ""))]
    else:
        p.error("give --clean --clue --audio (or --auto-voice --voice-ref), or --queue jobs.json")

    run_queue(jobs)
    return 0 if all(j.status == "done" for j in jobs) else 1


if __name__ == "__main__":
    if "--gui" in sys.argv or len(sys.argv) == 1:
        try:
            from studio_gui import main as gui_main
            sys.exit(gui_main())
        except Exception as exc:               # no display / tk missing -> CLI help
            print(f"(GUI unavailable: {exc})\n")
            _cli(["-h"])
    else:
        sys.exit(_cli())
