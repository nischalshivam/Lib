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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from media_index import libcheck                      # noqa: E402
from media_index import paths as _paths               # noqa: E402

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
    """Just the catalogues for the episodes this clue references — enough for a
    punch-in/cold-open to resolve its source clip, without loading a whole show."""
    import glob
    from media_index import catalog
    eps = set()
    for b in beats:
        for s in (b.get("shots") or []):
            se = str(s.get("season_episode") or "").upper().replace(" ", "")
            if se:
                eps.add(se)

    def epof(path):
        m = re.search(r"s(\d{1,2})\s*e(\d{1,2})", os.path.basename(path), re.I)
        return f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}" if m else ""

    lib = {}
    for c in glob.glob(os.path.join(movies_root, "**", "*.catalog.json"),
                       recursive=True):
        if epof(c) in eps:
            lib.update(catalog.load_library(c))
    return lib


def _apply_intro_hooks(job: Job, final: str, log) -> None:
    """Put the cold-open and/or intro punch-ins onto the delivered final.mp4."""
    import json
    from media_index import jobs as mi_jobs, punchins
    beats = mi_jobs.read_beats(job.clue)
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
    # punch-ins first (spliced within the intro), then the cold-open in front.
    if job.intro_punch and scenes:
        picks = punchins.find_intro_punches(beats, scenes, library,
                                            exclude_lines=exclude, log=log)
        if picks:
            punchins.apply(final, picks, tmp, log=log)
            os.replace(tmp, final)
        else:
            log("  intro punch-ins: intro me koi resolvable hook line nahi — skip")
    elif job.intro_punch:
        log("  intro punch-ins: timeline scenes nahi mile — skip")
    if cold_spec:
        punchins.prepend_cold_open(final, cold_spec, tmp, log=log)
        os.replace(tmp, final)
    elif job.cold_open:
        log("  cold-open: koi opening hook line resolve nahi hui — skip")


def _apply_kinetic_text(job: Job, final: str, log) -> None:
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
            beats = mi_jobs.read_beats(job.clue)
        except Exception:
            pass
        inst = os.path.join(job.out, "auto_text.txt")
        if not autotext.generate(clean, beats, inst, log=log):
            log("  kinetic text: narration se koi text moment nahi nikla — skip")
            return
    out = os.path.join(job.out, "final_texted.mp4")
    rc = subprocess.run([sys.executable, VTEXT, "--video", final, "--script",
                         job.clean, "--instructions", inst, "--out", out],
                        cwd=os.path.dirname(VTEXT)).returncode
    if rc == 0 and os.path.isfile(out):
        os.replace(out, final)
        log("  kinetic text: on-screen text composited")
    else:
        log(f"  kinetic text: vtext returncode {rc} — final kept as-is")


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
    if job.verify or job.verify_intro_min > 0:
        cast = _cast_for(job.movies_root, job.clue)
        if cast:
            mv += ["--cast", cast]
        if job.verify_intro_min > 0 and not job.verify:
            mv += ["--verify-until", str(job.verify_intro_min * 60)]
            log(f"  verify ON for the first {job.verify_intro_min} min only "
                "(the intro), then free — cheap accuracy for a long video")
        else:
            log("  verify ON (Gemini) for every clip"
                + (f" · cast: {cast}" if cast else ""))
    else:
        mv += ["--no-verify"]
        log("  verify OFF — placing clips straight from the (already-accurate) "
            "library by character match: no Gemini, no API cost, fewer gaps")
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
            _apply_kinetic_text(job, final, log)
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
                    text_file=j.get("text_file", getattr(a, "text_file", ""))) for j in raw]
    elif a.clean and a.clue and a.audio:
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
                    text_file=getattr(a, "text_file", ""))]
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
