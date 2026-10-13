"""editv2 — the whole pro edit for ONE script, resumable stage by stage:
ctx -> clue (agy Claude, grounded) -> voice -> words -> plan (Gemini re-anchor) -> cut
-> look (A/B/C/auto) -> HyperFrames render -> mix -> QA -> deliver.
Every stage writes its file into the build folder and is skipped when it exists."""
from __future__ import annotations

import json, os, re, shutil, subprocess, sys, time
from types import SimpleNamespace

from . import ctx as _ctx, clue as _clue, qa as _qa, mix as _mix
from .common import log_to

WHISPER = {"en": "small.en"}
RENDER_WORKERS = int(os.environ.get("HF_RENDER_WORKERS", "6"))     # Chrome capture workers (~256 MB RAM each)


def words(build, language, log=print):
    p = os.path.join(build, "words.json")
    if os.path.exists(p):
        return p
    from faster_whisper import WhisperModel
    m = WhisperModel(WHISPER.get(language, "small"), device="cpu", compute_type="int8", cpu_threads=16)
    segs, _ = m.transcribe(os.path.join(build, "audio.wav"), language=language, word_timestamps=True, vad_filter=False)
    W = [(round(w.start, 3), round(w.end, 3), w.word.strip()) for s in segs for w in s.words]
    json.dump(W, open(p, "w", encoding="utf-8"), ensure_ascii=False)
    log(f"  words: {len(W)} (whisper {WHISPER.get(language, 'small')}, {language})")
    return p


def voice(ctx, audio, voice_ref, log=print):
    out = os.path.join(ctx["build"], "audio.wav")
    if os.path.exists(out):
        return out
    if audio:
        # a ready voiceover must fit the script: a 45-s VOICE SAMPLE put in the Audio box (2026-10-12) became the whole
        # "voiceover" (153 words for a 298-line script). ~2.6 words/s is normal narration speed.
        try:
            secs = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", audio],
                                        capture_output=True, text=True).stdout.strip())
        except ValueError:
            secs = 0.0
        words = len(open(ctx["clean"], encoding="utf-8", errors="replace").read().split())
        if secs and words and secs < 0.4 * words / 2.6:
            raise RuntimeError(f"the Audio file is {secs:.0f} s but the script needs ~{words / 2.6 / 60:.0f} min of voiceover - "
                               "a voice SAMPLE goes in 'Voice sample' with Auto voiceover ON, and the Audio box stays empty")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", audio, "-ac", "1", "-ar", "48000", out], check=True)
        return out
    sys.path.insert(0, r"D:\Lib")
    import studio
    job = SimpleNamespace(clean=ctx["clean"], voice_ref=voice_ref, language=ctx["language"])
    return studio._generate_voiceover(job, out, log)


def _user_path():
    """the user PATH from the registry - a GUI launched from Explorer/a .bat can carry a stale or trimmed PATH."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return os.path.expandvars(winreg.QueryValueEx(k, "Path")[0]).split(os.pathsep)
    except OSError:
        return []


def _find_hyperframes():
    """npm-global hyperframes.cmd + node on PATH. 2026-10-12: the Movie Editor's child process did not find it even
    with an %APPDATA% fallback, so every known place is tried and the list is reported when nothing matches."""
    # ROOT CAUSE 2026-10-12: hyperframes was npm-installed from inside the Claude desktop app (an MSIX package), so
    # Windows wrote it into the package's private AppData (Packages\Claude_*\LocalCache\Roaming\npm) - visible to
    # Claude's own processes, INVISIBLE to the Movie Editor. The real copy lives on D:, which is never virtualized.
    dirs = [os.environ.get("HYPERFRAMES_DIR", r"D:\Tools\hyperframes")]
    dirs += [d for d in os.environ.get("PATH", "").split(os.pathsep) if d] + _user_path()
    for base in (os.environ.get("APPDATA"), os.environ.get("USERPROFILE") and os.path.join(os.environ["USERPROFILE"], "AppData", "Roaming"),
                 os.path.expanduser(r"~\AppData\Roaming"), r"C:\Users\Dell\AppData\Roaming"):
        if base:
            dirs.append(os.path.join(base, "npm"))
    dirs += [os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "nodejs"), r"C:\Program Files\nodejs"]
    seen, tried, hf = set(), [], None
    for d in dirs:
        key = os.path.normcase(os.path.abspath(d))
        if key in seen:
            continue
        seen.add(key)
        tried.append(d)
        if hf is None and os.path.isfile(os.path.join(d, "hyperframes.cmd")):
            hf = os.path.join(d, "hyperframes.cmd")
    if hf:                                        # hyperframes.cmd runs node: put node + the npm dir on PATH for the child
        extra = [os.path.dirname(hf)] + [d for d in dirs if os.path.isfile(os.path.join(d, "node.exe"))][:1]
        os.environ["PATH"] = os.pathsep.join(extra + [os.environ.get("PATH", "")])
    return hf, tried


def render(build, log=print):
    out = os.path.join(build, "silent.mp4")
    if os.path.exists(out) and os.path.getmtime(out) > os.path.getmtime(os.path.join(build, "comp", "index.html")):
        return out
    hf, tried = _find_hyperframes()
    if not hf:
        raise RuntimeError("HyperFrames CLI not found (npm i -g hyperframes) - looked in: " + "; ".join(tried))
    log(f"  render: HyperFrames = {hf}")
    if os.path.exists(out):                       # a stale render (older than the comp) must never count as this one
        os.remove(out)
    want = _duration(os.path.join(build, "audio.wav"))
    rlog = os.path.join(build, "render.log")
    t = time.time()
    tries, last_err = 4, ""
    nw = _render_workers(want or 600)
    for attempt in range(tries):
        # explicit workers: 'auto' looks at the CPU load and, with the indexing workers running, picked ONE worker
        # (2026-10-12: ~1 frame/s -> ~12 h for a 23-min video); too many run the commit charge out -> sized to memory
        if attempt:
            nw = max(2, min(nw - 2, _render_workers(want or 600)))
        log(f"  render: {nw} capture workers ({_commit_free_gb():.0f} GB memory free)")
        t_try = time.time()
        r = subprocess.run(f'"{hf}" render "{os.path.join(build, "comp")}" -o "{out}" -f 30 -q looks -w {nw} --quiet',
                           shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
        msg = (r.stderr or "") + (r.stdout or "")
        with open(rlog, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"\n===== attempt {attempt + 1} {time.strftime('%Y-%m-%d %H:%M:%S')} rc={r.returncode}\n{msg}\n")
        got = _duration(out) if os.path.exists(out) and os.path.getmtime(out) >= t_try else 0.0
        if got and (not want or got >= want - 1.0):
            break
        if got:                                   # a render that stopped early is not a render
            last_err = f"output is {got:.0f}s but the voiceover is {want:.0f}s"
            os.remove(out)
        else:
            # the real reason, not the last 800 chars of init chatter (2026-10-12 20:03: tail showed only 'fonts ready')
            keys = [ln.strip() for ln in msg.splitlines() if re.search(r"✗|error|fail|timeout|crash|unrenderable|ENOENT|EBUSY", ln, re.I)
                    and "Lint" not in ln and "Continuing render" not in ln]
            last_err = " | ".join(dict.fromkeys(keys))[-600:] or f"rc={r.returncode}, no output ({msg.strip()[-200:]})"
        bad = sorted(set(re.findall(r'assets/([\w.-]+\.mp4)', msg)))
        if not bad:
            if attempt == tries - 1:
                break
            # transient: frame extraction of valid clips under load (10:11 - 111 clips, fine 10 min later), a Chrome
            # worker dying, ... -> wait and render again
            log(f"  render: attempt {attempt + 1} failed ({last_err[:160]}) - retrying in 60 s (full log: render.log)")
            time.sleep(60)
            continue
        for f in bad:                              # a source Chrome can't seek -> re-encode with 1-s keyframes, retry
            src = os.path.join(build, "comp", "assets", f)
            tmp = src + ".tmp.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                            "-g", "30", "-keyint_min", "30", "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart", tmp], check=True)
            os.remove(src)
            os.replace(tmp, src)
            log(f"  render: re-encoded {f} (unseekable), retrying")
    if not (os.path.exists(out) and _duration(out) >= (want - 1.0 if want else 1.0)):
        raise RuntimeError(f"HyperFrames render failed after {tries} attempts: {last_err} (full log: {rlog})")
    log(f"  render: {time.time() - t:.0f}s, {_duration(out):.0f}s of video")
    return out


def _commit_free_gb():
    """free Windows commit charge (RAM + pagefile) in GB - what new allocations actually draw from."""
    import ctypes

    class MS(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    m = MS()
    m.dwLength = ctypes.sizeof(MS)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m.ullAvailPageFile / 1e9


def _render_workers(seconds):
    """Chrome capture workers that FIT: each committed ~12.5 GB for a 23-min comp (2026-10-12 20:25 - 6 workers +
    10 indexing workers + OmniVoice put the commit charge at 165.6/168.9 GB, where allocations start failing)."""
    per = max(4.0, 0.6 * seconds / 60)
    n = int((_commit_free_gb() - 12) / per)
    return max(2, min(RENDER_WORKERS, n))


def _duration(path):
    try:
        return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                                    capture_output=True, text=True, timeout=60).stdout.strip())
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return 0.0


def preflight(clean, out_dir, audio, voice_ref, save_dir):
    """Seconds-long check of everything the later stages need, BEFORE the 45+ min of clue/voice/plan/cut/look.
    2026-10-12: each missing piece (voice sample, OmniVoice, HyperFrames, ...) only showed up when its stage was reached.
    -> list of problems (empty = go)."""
    import glob, urllib.request
    bad = []
    try:
        if not open(clean, encoding="utf-8", errors="replace").read().strip():
            bad.append(f"the script is empty: {clean}")
    except OSError as e:
        bad.append(f"cannot read the script {clean}: {e}")
    have_audio = os.path.exists(os.path.join(out_dir, "audio.wav"))
    if audio:
        if not os.path.isfile(audio):
            bad.append(f"the Voiceover audio file does not exist: {audio}")
    elif not have_audio:
        if not voice_ref:
            bad.append("no voiceover: pick a Voice sample (Auto voiceover ON) or a finished Voiceover audio file")
        elif not os.path.isfile(voice_ref):
            bad.append(f"the Voice sample file does not exist: {voice_ref}")
        elif not os.path.isfile(os.path.join(os.environ.get("OMNIVOICE_DIR", r"D:\omnivoice-wrapper"), "run.bat")):
            bad.append(r"OmniVoice is not installed (D:\omnivoice-wrapper\run.bat missing) - needed for Auto voiceover")
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            bad.append(f"{tool} not found on PATH")
    hf, tried = _find_hyperframes()
    if not hf:
        bad.append(r"HyperFrames not found (expected D:\Tools\hyperframes\hyperframes.cmd)")
    if not shutil.which("node"):
        bad.append(r"node.js not found (C:\Program Files\nodejs)")
    if not glob.glob(os.path.expanduser(r"~\.cache\hyperframes\chrome\**\chrome-headless-shell.exe"), recursive=True):
        bad.append(r"HyperFrames' Chrome is missing (~\.cache\hyperframes\chrome) - run: hyperframes browser install")
    from .common import PROXY, KEY, FACE_MODEL
    if not all(os.path.exists(os.path.join(out_dir, f)) for f in ("clue_v2.json", "plan.json", "state.json")):
        try:                                      # only clue/plan/cut talk to the models; a render-only resume does not
            req = urllib.request.Request(PROXY.rsplit("/chat/", 1)[0] + "/models", headers={"Authorization": "Bearer " + KEY})
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as e:                                                   # noqa: BLE001
            bad.append(f"the model proxy (:8317, needed for clue/plan) does not answer: {type(e).__name__}")
    if not os.path.isfile(FACE_MODEL):
        bad.append(f"face model missing: {FACE_MODEL}")
    from .mix import BGM
    if not (glob.glob(os.path.join(BGM, "*.mp3")) + glob.glob(os.path.join(BGM, "*.wav"))):
        bad.append(f"no background music in {BGM}")
    for d in {os.path.splitdrive(os.path.abspath(out_dir))[0] + "\\",
              os.path.splitdrive(os.path.abspath(save_dir))[0] + "\\" if save_dir else None} - {None}:
        try:
            free = shutil.disk_usage(d).free / 1e9
            if free < 15:
                bad.append(f"only {free:.0f} GB free on {d} (a 25-min build needs ~15 GB)")
        except OSError as e:
            bad.append(f"drive {d} not reachable: {e}")
    return bad


def build(clean, out_dir="", audio="", language="en", look="auto", save_dir="", voice_ref="",
          shows=None, log=None, stop=None):
    """Returns the delivered video path. Raises on a hard failure (the caller reports it)."""
    stop = stop or (lambda: False)
    name = os.path.splitext(os.path.basename(clean))[0]
    out_dir = out_dir or os.path.join(os.path.dirname(os.path.abspath(clean)), name + "_v2build")
    os.makedirs(out_dir, exist_ok=True)
    log = log or log_to(os.path.join(out_dir, "build.log"))
    T = time.time()

    def stage(title):
        if stop():
            raise RuntimeError("stopped")
        log(f"\n[{time.strftime('%H:%M:%S')}] {title}")

    lock = _lock_build(out_dir)                   # held until this process ends (OS lock, freed if it dies)
    if lock is None:
        raise RuntimeError("this video is already being built by another run (a second Movie Editor window or a run "
                           "still going after its window was closed) - wait for it or stop it first")
    if (language or "").lower() in ("", "auto"):
        language = detect_language(clean)
        log(f"  language auto -> {language}")
    _invalidate_changed(out_dir, clean, look, language, audio, voice_ref, log)
    stage("preflight (tools, voice, proxy, disk)")
    bad = preflight(clean, out_dir, audio, voice_ref, save_dir)
    if bad:
        raise RuntimeError("cannot start - fix these first:\n  - " + "\n  - ".join(bad))
    log("  all checks passed")
    stage("context (shows, cast, typography)")
    ctx = _ctx.build(clean, out_dir, language, look, shows, log=log)
    if not any(s["indexed"] for s in ctx["shows"]):
        raise RuntimeError("none of the script's shows are indexed yet")
    stage("clue (agy Claude, grounded in the catalogs)")
    _clue.build(ctx, log)
    stage("voice")
    voice(ctx, audio, voice_ref, log)
    stage("words")
    words(out_dir, language, log)
    from .plan import Plan
    if not os.path.exists(os.path.join(out_dir, "plan.json")):
        stage("plan (scene grammar + Gemini re-anchor + sound-ups)")
        Plan(ctx).run(log)
    from .cut import Cutter
    if not os.path.exists(os.path.join(out_dir, "state.json")):
        stage("cut (real cuts, text zones, identity checks)")
        Cutter(ctx, log).run()
    from .look import Look
    if not os.path.exists(os.path.join(out_dir, "look.json")):
        stage("look (effects + typography)")
        Look(ctx, log).build()
    stage("render (HyperFrames)")
    silent = render(out_dir, log)
    stage("mix")
    final = os.path.join(out_dir, f"{name}.mp4")
    _mix.run(ctx, silent, final, log)
    stage("QA")
    R = _qa.run(ctx, final, log)
    if save_dir:
        final = deliver(final, save_dir, topic_name(clean), os.path.join(out_dir, "QA.txt"), R["result"], log)
    log(f"\nDONE {final}  ({(time.time() - T) / 60:.1f} min, QA {R['result']})")
    return final


def _lock_build(out_dir):
    """-> open handle holding an OS byte lock on <build>\\.building, or None if another process holds it."""
    import msvcrt
    f = open(os.path.join(out_dir, ".building"), "a+")
    try:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        return f
    except OSError:
        f.close()
        return None


STOP = {"en": "the and of to is that it you was for with this but his they have are not", "es": "el la de que y en los se del las por un una para con no es",
        "pt": "o a de que e do da em um para com não uma os no se na por mais", "fr": "le la de et les des en un une est que pour pas qui dans du",
        "de": "der die und den das ist nicht ein mit sich des auf für von dem zu", "it": "il la di che e un una per non del della sono con si gli"}


def detect_language(clean):
    """'auto' (a GUI choice that crashed the words stage): Devanagari -> hi, else the language whose stop words fit best."""
    t = open(clean, encoding="utf-8-sig", errors="replace").read()[:20000]
    letters = [c for c in t if c.isalpha()]
    if letters and sum(1 for c in letters if "ऀ" <= c <= "ॿ") / len(letters) > 0.2:
        return "hi"
    words = re.findall(r"[^\W\d_]+", t.lower())
    score = {k: sum(1 for w in words if w in set(v.split())) for k, v in STOP.items()}
    return max(score, key=score.get) if words else "en"


def _fp(path):
    try:
        s = os.stat(path)
        return [os.path.abspath(path).lower(), s.st_size, int(s.st_mtime)]
    except OSError:
        return [path, None, None]


def _invalidate_changed(out_dir, clean, look, language, audio, voice_ref, log):
    """Remember the settings a build was made with (settings.json). A re-run with another Look / Language / voiceover
    used to reuse the cached ctx.json, audio.wav, look.json and silent.mp4 and quietly deliver the OLD video. Now the
    stages that depend on a changed setting are rebuilt (their outputs removed); everything else is reused."""
    p = os.path.join(out_dir, "settings.json")
    cur = {"look": (look or "auto").upper(), "language": language,
           "voice": ["audio"] + _fp(audio) if audio else ["sample"] + _fp(voice_ref) if voice_ref else ["none"]}
    try:
        old = json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError):
        old = None
    if old and cur["voice"][0] == "none":         # a resume without voice args keeps the voice the build was made with
        cur["voice"] = old.get("voice", cur["voice"])
    if old and os.path.exists(os.path.join(out_dir, "audio.wav")):
        redo = []
        voice_changed = old.get("voice") != cur["voice"] and not (cur["voice"][0] == "none")
        if voice_changed or old.get("language") != cur["language"]:
            redo = ["audio.wav", "words.json", "plan.json", "state.json", "assets", "look.json", "comp", "silent.mp4"]
            why = "voiceover" if voice_changed else "language"
        elif old.get("look") != cur["look"]:
            redo = ["look.json", "comp", "silent.mp4"]
            why = "look"
        if redo:
            log(f"  {why} changed since the last run -> rebuilding: {', '.join(redo)}")
            for f in redo:
                q = os.path.join(out_dir, f)
                if os.path.isdir(q):
                    shutil.rmtree(q, ignore_errors=True)
                elif os.path.exists(q):
                    os.remove(q)
    ctxp = os.path.join(out_dir, "ctx.json")             # ctx.json caches look + language too
    if os.path.exists(ctxp):
        try:
            c = json.load(open(ctxp, encoding="utf-8"))
            if c.get("look") != (look or "auto") or c.get("language") != language:
                c["look"], c["language"] = (look or "auto"), language
                json.dump(c, open(ctxp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        except (OSError, ValueError):
            pass
    json.dump(cur, open(p, "w", encoding="utf-8"), indent=1)


GENERIC = {"script", "clean", "narration", "final", "script_final", "clean_script", "voiceover", "text"}


def topic_name(clean):
    """name of the VIDEO: every Reacher topic folder holds 'Script.txt', so the file name alone made every delivery
    'Script.mp4' (a batch saved to one folder overwrote itself) and gave every video the same 'auto' look."""
    base = os.path.splitext(os.path.basename(clean))[0]
    if base.strip().lower() in GENERIC:
        base = os.path.basename(os.path.dirname(os.path.abspath(clean))) or base
    return re.sub(r'[<>:"/\\|?*]+', " ", base).strip() or "video"


def deliver(final, save_dir, name, qa_txt, result, log=print):
    """copy the finished video to save_dir as '<topic>.mp4' via a .part file (a half copy never looks finished); a
    locked target (open in a player) does not fail a 1-2 h build - the video stays in the build folder and that is said."""
    os.makedirs(save_dir, exist_ok=True)
    dst = os.path.join(save_dir, name + ".mp4")
    qa_dst = os.path.join(save_dir, name + "_QA.txt")
    try:
        shutil.copy(final, dst + ".part")
        os.replace(dst + ".part", dst)
    except OSError as e:
        log(f"  ! could not save to {dst} ({type(e).__name__}: {e}) - the finished video is {final}")
        return final
    try:
        if result != "PASS":
            shutil.copy(qa_txt, qa_dst)
        elif os.path.exists(qa_dst):
            os.remove(qa_dst)                     # an old report must not sit next to the new, passing video
    except OSError:
        pass
    return dst


def prepare_clues(folder, language="en", log=print):
    """Batch: ctx + grounded clue for every clean .txt in a folder (no voice, no render).
    Run it once while scripts pile up; build() later reuses the clue."""
    done = []
    for f in sorted(os.listdir(folder)):
        if not f.lower().endswith(".txt") or f.endswith("_QA.txt"):
            continue
        clean = os.path.join(folder, f)
        bd = os.path.join(folder, os.path.splitext(f)[0] + "_v2build")
        try:
            log(f"\n== {f}")
            ctx = _ctx.build(clean, bd, language, "auto", log=log)
            _clue.build(ctx, log)
            st = json.load(open(os.path.join(bd, "clue_v2.json"), encoding="utf-8")).get("stats", {})
            done.append((f, "ok", st))
        except Exception as e:
            log(f"  ! {f}: {e}")
            done.append((f, "error", str(e)))
    return done
