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
import subprocess
import sys
import time
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from media_index import libcheck                      # noqa: E402
from media_index import paths as _paths               # noqa: E402

PROSTUDIO = os.path.join(HERE, "prostudio", "prostudio.py")
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
    index: int = 0                   # position in the queue (drives format rotation)
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
    have = [d for d in os.listdir(movies_root)
            if os.path.isdir(os.path.join(movies_root, d))]
    norm = lambda s: re.sub(r"[^a-z0-9]+", "", s.lower())
    for show, _eps in sorted(need.items(), key=lambda kv: -len(kv[1])):
        w = norm(show)
        for d in have:
            dd = norm(d)
            if dd == w or w in dd or dd in w:
                cast = _find_cast(os.path.join(movies_root, d))
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

    movies = job.movies_root or _paths.movies_root()
    if not movies or not os.path.isdir(movies):
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


def build(job: Job, log=print, on_proc=None, should_stop=None) -> Job:
    """The whole pipeline for ONE video. Never raises — reports via job.status.

    `on_proc(proc_or_None)` hands the GUI the running child so a Stop button can
    kill it; `should_stop()` is polled between stages so a stop lands cleanly. A
    stopped video keeps its half-done build folder, so the next Run resumes it
    (makevideo reuses cut clips, prostudio reuses rendered shots)."""
    _stop = should_stop or (lambda: False)
    t0 = time.time()
    job.status = "running"
    if _stop():
        job.status, job.message = "stopped", "stopped before it started"
        return job
    if not preflight(job, log):
        return job

    job.out = job.out or _default_out(job)
    os.makedirs(job.out, exist_ok=True)

    # ---- stage 4: pick the real clips + align to the voiceover -------------- #
    log("\n  [makevideo] cutting the right clips, aligning to the voiceover...")
    mv = [sys.executable, "-m", "media_index", "makevideo", job.clue,
          job.movies_root, job.audio, "--narration", job.clean, "--out", job.out]
    cast = _cast_for(job.movies_root, job.clue)
    if cast:
        mv += ["--cast", cast]
        log(f"  cast (identity refs): {cast}")
    else:
        log("  ! no cast folder found — character identity will be a guess "
            "(add a Cast\\ folder under the show for verified identity)")
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

    # ---- stage 5: pro effects + final render -------------------------------- #
    fmt = _format_for(job)
    final = os.path.join(job.out, "final.mp4")
    log(f"\n  [prostudio] applying effects (format {fmt}, {job.resolution})...")
    ps = [sys.executable, PROSTUDIO, "--scenes", job.out, "--audio", job.audio,
          "--script", job.clean, "--out", final, "--format", fmt,
          "--resolution", job.resolution, "--resume"]   # reuse already-rendered
    ps += ["--text"] if job.text else ["--no-text"]
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
    p.add_argument("--queue", help="jobs.json: [{clean,clue,audio,out?}, ...]")
    a = p.parse_args(argv)

    if a.queue:
        raw = json.load(open(a.queue, encoding="utf-8"))
        jobs = [Job(clean=j["clean"], clue=j["clue"], audio=j["audio"],
                    out=j.get("out", ""), save_dir=j.get("save_dir", a.save_dir),
                    movies_root=j.get("movies", a.movies),
                    fmt=j.get("format", a.format),
                    resolution=j.get("resolution", a.resolution),
                    text=j.get("text", a.text)) for j in raw]
    elif a.clean and a.clue and a.audio:
        jobs = [Job(clean=a.clean, clue=a.clue, audio=a.audio, out=a.out,
                    save_dir=a.save_dir, movies_root=a.movies, fmt=a.format,
                    resolution=a.resolution, text=a.text)]
    else:
        p.error("give --clean --clue --audio, or --queue jobs.json")

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
