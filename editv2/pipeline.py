"""editv2 — the whole pro edit for ONE script, resumable stage by stage:
ctx -> clue (agy Claude, grounded) -> voice -> words -> plan (Gemini re-anchor) -> cut
-> look (A/B/C/auto) -> HyperFrames render -> mix -> QA -> deliver.
Every stage writes its file into the build folder and is skipped when it exists."""
from __future__ import annotations

import json, os, re, shutil, subprocess, sys, time
from types import SimpleNamespace

from . import ctx as _ctx, clue as _clue, qa as _qa, mix as _mix
from .common import log_to, FPS

WHISPER = {"en": "small.en"}
# Chrome capture workers. Measured 2026-10-14 on the 24-min Wire comp (RTX 5070 12 GB, 64 GB RAM): 2 = ~15 fps, 3 = ~16 fps,
# 4 = 19 fps but "Unable to capture screenshot" after 95 s (GPU), 6 = stuck. ~6 GB RAM per worker.
# 2026-10-15: 3 workers (~9 GB VRAM) + an OmniVoice voice -> GPU timeout (nvlddmkm 153, TDR), OmniVoice CUDA crash, the
# render hung. 2 workers leave the voice room; the 3rd only added ~20 % speed.
RENDER_WORKERS = int(os.environ.get("HF_RENDER_WORKERS", "2"))
# 2026-10-15: two GPU driver timeouts (nvlddmkm 153) in the Wire batch while renders ran on the GPU; software capture
# (--no-browser-gpu) measured 13.8 fps at 2 workers (as fast as GPU mode), CPU 19 %, frames identical (SSIM
# 0.995-0.997 vs the GPU render of the same comp) - and the GPU stays free for OmniVoice. EDITV2_RENDER_GPU=1 = old way.
# REVERTED the same day: on a FRESH comp the software capture hangs on its first frames (Runtime.callFunctionOn
# timed out after 5 min, V17 twice; the 13.8 fps test had a warm extract cache). GPU capture (2 workers, voices
# waiting while it captures) is what rendered 13 videos overnight. EDITV2_RENDER_GPU=0 = software.
RENDER_GPU_FLAG = " --no-browser-gpu" if os.environ.get("EDITV2_RENDER_GPU") == "0" else ""


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
    comp = os.path.join(build, "comp")
    if os.path.exists(out) and os.path.getmtime(out) > os.path.getmtime(os.path.join(comp, "index.html")):
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
    # 2026-10-15: a comp with 260-313 clips hangs Chrome (see parts.py) -> long comps render in ~5-min parts
    from . import parts as _parts
    plist = _parts.write(comp, FPS, log)
    if not plist:
        _render_target(hf, build, None, out, want, None, rlog, log)
    else:
        pdir = os.path.join(build, "parts")
        os.makedirs(pdir, exist_ok=True)
        idx_time = os.path.getmtime(os.path.join(comp, "index.html"))
        for k, (name, f0, f1) in enumerate(plist):
            pout = os.path.join(pdir, f"part_{k:02d}.mp4")
            tag = f"part {k + 1}/{len(plist)}"
            # a part rendered from this same comp is kept: a retry only renders what is still missing
            if os.path.exists(pout) and os.path.getmtime(pout) > idx_time and _frames(pout) == f1 - f0:
                log(f"  render {tag}: already rendered")
                continue
            log(f"  render {tag}: {f0 / FPS / 60:.1f}-{f1 / FPS / 60:.1f} min")
            _render_target(hf, build, name, pout, (f1 - f0) / FPS, f1 - f0, rlog, log, tag)
        _concat([os.path.join(pdir, f"part_{k:02d}.mp4") for k in range(len(plist))], out)
        if _frames(out) != plist[-1][2]:
            raise RuntimeError(f"joined render has {_frames(out)} frames, the comp {plist[-1][2]} (full log: {rlog})")
        shutil.rmtree(pdir, ignore_errors=True)    # ~2.4 GB for a 29-min video; silent.mp4 is the render now
    if not (os.path.exists(out) and _duration(out) >= (want - 1.0 if want else 1.0)):
        raise RuntimeError(f"HyperFrames render is {_duration(out):.0f}s, the voiceover {want:.0f}s (full log: {rlog})")
    log(f"  render: {time.time() - t:.0f}s, {_duration(out):.0f}s of video")
    return out


def _render_target(hf, build, part, out, want, want_frames, rlog, log, tag="render"):
    """one HyperFrames render (the whole comp, or comp/<part> with -c) into `out`, retried up to 4 times."""
    tries, last_err = 4, ""
    nw = _render_workers(want or 600)
    comp = os.path.join(build, "comp")
    for attempt in range(tries):
        # explicit workers: 'auto' looks at the CPU load and, with the indexing workers running, picked ONE worker
        # (2026-10-12: ~1 frame/s -> ~12 h for a 23-min video); too many run the commit charge out -> sized to memory
        if attempt:
            nw = max(2, min(nw - 2, _render_workers(want or 600)))
        log(f"  {tag}: {nw} capture workers ({_commit_free_gb():.0f} GB memory free)")
        t_try = time.time()
        r = _hf_render(f'"{hf}" render "{comp}"' + (f' -c {part}' if part else "") +
                       f' -o "{out}" -f {FPS} -q looks -w {nw} --quiet{RENDER_GPU_FLAG}', t_try, log)
        msg = (r.stderr or "") + (r.stdout or "")
        with open(rlog, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"\n===== {tag} attempt {attempt + 1} {time.strftime('%Y-%m-%d %H:%M:%S')} rc={r.returncode}\n{msg}\n")
        got = _duration(out) if os.path.exists(out) and os.path.getmtime(out) >= t_try else 0.0
        # a part must have its exact frame count (the parts are joined back to back under one voiceover)
        if got and (_frames(out) == want_frames if want_frames else (not want or got >= want - 1.0)):
            return
        if got:                                   # a render that stopped early is not a render
            last_err = f"output is {got:.1f}s but should be {want:.1f}s"
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
            log(f"  {tag}: attempt {attempt + 1} failed ({last_err[:160]}) - retrying in 60 s (full log: render.log)")
            time.sleep(60)
            continue
        for f in bad:                              # a source Chrome can't seek -> re-encode with 1-s keyframes, retry
            src = os.path.join(comp, "assets", f)
            tmp = src + ".tmp.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                            "-g", str(FPS), "-keyint_min", str(FPS), "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart", tmp], check=True)
            os.remove(src)
            os.replace(tmp, src)
            log(f"  {tag}: re-encoded {f} (unseekable), retrying")
    raise RuntimeError(f"HyperFrames {tag} failed after {tries} attempts: {last_err} (full log: {rlog})")


def _frames(path):
    """exact video frame count (packets) - parts must join to exactly the comp's frames."""
    try:
        return int(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
                                   "stream=nb_read_packets", "-of", "csv=p=0", path],
                                  capture_output=True, text=True, timeout=300).stdout.strip())
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return -1


def _concat(files, out):
    """parts -> one video, no re-encode (same encoder settings). Relative names: the build paths are long and some
    topics contain quotes."""
    d = os.path.dirname(files[0])
    lst = os.path.join(d, "parts.txt")
    with open(lst, "w", encoding="utf-8") as f:
        f.writelines(f"file '{os.path.basename(p)}'\n" for p in files)
    tmp = os.path.join(d, "joined.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", "parts.txt", "-c", "copy",
                    "-movflags", "+faststart", "joined.mp4"], cwd=d, check=True, capture_output=True)
    os.replace(tmp, out)


RENDER_STALL_MIN = 20       # no new captured frame for this long (after the first frames came) = a hung render


def _hf_render(cmd, t_start, log):
    """HyperFrames render with a stall guard. It ran with no timeout: a hung Chrome capture would have kept a whole
    night batch waiting forever (phase 2 renders one video after another). Progress = frames in its newest
    %TEMP%\\hf-render-* folder (captured-frames or capture-attempt-*\\worker-*). No new frame for RENDER_STALL_MIN minutes (or none at all within 45 min)
    -> the render's process tree is killed and render() retries it like any failed attempt."""
    import glob, tempfile
    from types import SimpleNamespace as NS
    tmp = tempfile.gettempdir()
    for d in glob.glob(os.path.join(tmp, "hf-render-*")):     # a killed / crashed render leaves its ~15 GB of frames
        try:                                                   # behind (a finished one cleans up); renders take < 2 h
            if time.time() - os.path.getmtime(d) > 6 * 3600:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass
    so, se = tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace"), \
        tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace")
    p = subprocess.Popen(cmd, shell=True, stdout=so, stderr=se, text=True, encoding="utf-8", errors="replace")
    last_n, last_t, stalled, cur, seen = -1, time.time(), "", None, False
    import psutil

    def tree_cpu():
        """CPU seconds of the render's whole process tree (node, Chrome, the final ffmpeg encode)."""
        s = 0.0
        try:
            root = psutil.Process(p.pid)
            for q in [root] + root.children(recursive=True):
                try:
                    c = q.cpu_times()
                    s += c.user + c.system
                except psutil.Error:
                    pass
        except psutil.Error:
            pass
        return s
    last_cpu = tree_cpu()
    while p.poll() is None:
        time.sleep(30)
        if p.poll() is not None:                 # finished during the wait: nothing to judge
            break
        dirs = [d for d in glob.glob(os.path.join(tmp, "hf-render-*")) if os.path.getctime(d) >= t_start - 5]
        n = 0
        if dirs:
            cur = max(dirs, key=os.path.getctime)
            # frames land in captured-frames\ or, with several workers, capture-attempt-<n>\worker-<k>\ - count them all
            for dp, dn, fn in os.walk(cur):
                n += sum(1 for f in fn if f.endswith((".jpg", ".png")))
        seen = seen or n > 0
        cpu = tree_cpu()
        # 2026-10-14: after the capture the frame count stands still for the merge + final encode (10-15 min on a busy
        # PC) - that is work, not a hang: a tree still burning CPU (> 5 s per 30 s check; a hung one ~0) counts as progress
        if n != last_n or cpu - last_cpu > 5:
            last_n, last_t, last_cpu = n, time.time(), cpu
            continue
        last_cpu = cpu
        idle = (time.time() - last_t) / 60
        # frames seen: stuck = no change for RENDER_STALL_MIN (the folder vanishing at the end is a change, so a
        # finishing render is never cut); never any frame: 45 min of setup is a hang
        if (seen and idle >= RENDER_STALL_MIN) or (not seen and (time.time() - t_start) / 60 >= 45):
            stalled = (f"render stalled: no new frame for {idle:.0f} min at {n} frames" if seen
                       else "render stalled: no frame captured in 45 min")
            log(f"  {stalled} - stopping it")
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
            p.wait()
            if cur:
                time.sleep(5)
                shutil.rmtree(cur, ignore_errors=True)
            break
    so.seek(0)
    se.seek(0)
    r = NS(returncode=p.returncode, stdout=so.read(), stderr=se.read() + ("\n" + stalled if stalled else ""))
    so.close()
    se.close()
    return r


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
    import psutil
    per_ram = max(4.0, 0.45 * seconds / 60)              # measured 10-11 GB private per Chrome worker, 23-min comp
    n_ram = int((psutil.virtual_memory().available / 1e9 - 8) / per_ram)
    per = max(4.0, 0.45 * seconds / 60)                  # commit: keeps a crowded PC (indexing + OmniVoice) safe
    n_commit = int((_commit_free_gb() - 12) / per) + 1
    return max(2, min(RENDER_WORKERS, n_ram, n_commit))


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
    # 2026-10-16: a channel's "Final Videos\<topic>_QA.txt" report was queued as a video's script (Reacher batch V18)
    if os.path.basename(clean).lower().endswith("_qa.txt") or \
            os.path.basename(os.path.dirname(os.path.abspath(clean))).strip().lower() in ("final videos", "final video"):
        bad.append(f"not a script - a QA report / finished-videos folder: {clean}")
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
    from .common import PROXY, KEY, FACE_MODEL, provider
    P = provider()
    if not all(os.path.exists(os.path.join(out_dir, f)) for f in ("clue_v2.json", "plan.json", "state.json")):
        if P:                                     # 2026-10-17: the paid Gemini-API provider replaces the agy proxy
            if not os.path.isfile(P.get("key_file", "")):
                bad.append(f"API key file missing: {P.get('key_file')} (see editv2/provider.json)")
        else:
            try:                                  # only clue/plan/cut talk to the models; a render-only resume does not
                req = urllib.request.Request(PROXY.rsplit("/chat/", 1)[0] + "/models", headers={"Authorization": "Bearer " + KEY})
                urllib.request.urlopen(req, timeout=15).read()
            except Exception as e:                                               # noqa: BLE001
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
          shows=None, log=None, stop=None, until=None):
    """Returns the delivered video path. Raises on a hard failure (the caller reports it).
    until="online": stop after the LOOK stage - every step that needs the internet (clue, plan, cut, look; footage on
    Google Drive) is then done and saved; render / mix / QA run later fully OFFLINE (user 2026-10-13: a 30-video night
    queue must not die on an internet drop). Returns 'ONLINE-DONE <build dir>'."""
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
    json.dump({"t": time.strftime("%Y-%m-%d %H:%M:%S")}, open(os.path.join(out_dir, "online_done.json"), "w"))
    if until == "online":
        log(f"\nONLINE PHASE DONE - everything that needs the internet is saved ({(time.time() - T) / 60:.1f} min); "
            "render / mix / QA can run offline")
        return f"ONLINE-DONE {out_dir}"
    stage("render (HyperFrames)")
    with _render_gpu_guard(out_dir, log):
        silent = render(out_dir, log)
    stage("mix")
    # short name inside the build: "<topic>_v2build\<topic>.mp4" passed Windows' 260-char limit for long topic names
    # (The Wire 2026-10-14: the copy to Final Videos failed and the batch marked a finished video "failed")
    final = os.path.join(out_dir, "final.mp4")
    _mix.run(ctx, silent, final, log)
    stage("QA")
    R = _qa.run(ctx, final, log)
    if save_dir:
        final = deliver(final, save_dir, topic_name(clean), os.path.join(out_dir, "QA.txt"), R["result"], log)
    log(f"\nDONE {final}  ({(time.time() - T) / 60:.1f} min, QA {R['result']})")
    return final


def _render_gpu_guard(out_dir, log):
    """Chrome's GPU render next to the indexing workers' CUDA whisper crashed the PC (BSOD 0xD1 nvlddmkm, 2026-10-13).
    While this render runs, the libjobs 'reserved' file keeps the workers from starting a whisper pass
    (catalog_refine.gpu_slot honours it) and the render starts only once their running GPU children are gone.
    Several renders can hold it together: it is touched every 20 s and never deleted here - 5 min after the last
    render it is stale and indexing whisper resumes by itself. No indexing running = no wait."""
    import contextlib, threading

    @contextlib.contextmanager
    def guard():
        out = os.path.join(out_dir, "silent.mp4")
        comp = os.path.join(out_dir, "comp", "index.html")
        if os.path.exists(out) and os.path.exists(comp) and os.path.getmtime(out) > os.path.getmtime(comp):
            yield                                     # render() will return the finished file: nothing to guard
            return
        res = r"D:\Lib\libjobs\gpu\reserved"
        stop = threading.Event()

        def touch():
            while not stop.is_set():
                try:
                    os.makedirs(os.path.dirname(res), exist_ok=True)
                    with open(res, "w") as f:
                        f.write(str(os.getpid()))
                except OSError:
                    pass
                stop.wait(20)
        threading.Thread(target=touch, daemon=True).start()
        t0, quiet, said = time.time(), 0, False
        while time.time() - t0 < 600:
            n = subprocess.run(["powershell", "-NoProfile", "-Command",
                                "@(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object "
                                "{ $_.CommandLine -match 'gpu_pass' }).Count"],
                               capture_output=True, text=True, creationflags=0x08000000).stdout.strip()
            if n in ("", "0"):
                quiet += 1
                if quiet >= 2:
                    break
            else:
                quiet = 0
                if not said:
                    log(f"  render: waiting for {n} indexing whisper pass(es) to finish before using the GPU")
                    said = True
            time.sleep(5)
        # 2026-10-14 pipelined batch: OmniVoice (another video's voice stage) needs ~3.7 GB of VRAM that a running
        # render's Chrome workers take. A voice that found the GPU busy waits (studio: voice_waiting_<pid> marker,
        # voice.lock held while it waits / speaks) - let it speak BEFORE this render takes the GPU (max 15 min).
        import glob as _g, msvcrt
        gdir = os.path.dirname(res)
        t1, said = time.time(), False
        while time.time() - t1 < 900:
            waiting = [f for f in _g.glob(os.path.join(gdir, "voice_waiting_*")) if time.time() - os.path.getmtime(f) < 300]
            busy = False
            try:
                with open(os.path.join(gdir, "voice.lock"), "a+") as vl:
                    vl.seek(0)
                    msvcrt.locking(vl.fileno(), msvcrt.LK_NBLCK, 1)       # free = nobody speaking / waiting to
                    vl.seek(0)
                    msvcrt.locking(vl.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                busy = True
            if not waiting and not busy:
                break
            if not said:
                log("  render: another video's voiceover needs the GPU first - waiting for it (max 15 min)")
                said = True
            time.sleep(10)
        try:
            yield
        finally:
            stop.set()
    return guard()


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


def _lp(p):
    """Windows extended-length path for paths at/over the 260-char limit (long topic folder names)."""
    p = os.path.abspath(p)
    return "\\\\?\\" + p if os.name == "nt" and len(p) >= 248 and not p.startswith("\\\\?\\") else p


def deliver(final, save_dir, name, qa_txt, result, log=print):
    """copy the finished video to save_dir as '<topic>.mp4' via a .part file (a half copy never looks finished); a
    locked target (open in a player) does not fail a 1-2 h build - the video stays in the build folder and that is said."""
    os.makedirs(save_dir, exist_ok=True)
    dst = os.path.join(save_dir, name + ".mp4")
    qa_dst = os.path.join(save_dir, name + "_QA.txt")
    try:
        shutil.copy(_lp(final), _lp(dst + ".part"))         # long-path safe on both ends
        os.replace(_lp(dst + ".part"), _lp(dst))
    except OSError as e:
        log(f"  ! could not save to {dst} ({type(e).__name__}: {e}) - the finished video is {final}")
        return final
    try:
        if result != "PASS":
            shutil.copy(_lp(qa_txt), _lp(qa_dst))
        elif os.path.exists(_lp(qa_dst)):
            os.remove(_lp(qa_dst))                     # an old report must not sit next to the new, passing video
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
