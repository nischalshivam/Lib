"""One library worker: takes the first queue item that is not done / not locked / not failed 3x and does the
next step for it (index -> refine -> dialogue upgrade). Waits (never fails) while the proxy or Drive is down.
Writes a heartbeat (heartbeat_<n>.json) at every job start so the supervisor can kill a worker stuck inside a hung
Drive read.        usage: pythonw worker.py <n>"""
import glob, json, os, shutil, subprocess, sys, tempfile, time, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

N = sys.argv[1] if len(sys.argv) > 1 else "0"
LOG = open(os.path.join(C.LOGS, f"worker_{N}.log"), "a", encoding="utf-8", buffering=1)
sys.stdout = sys.stderr = LOG
open(os.path.join(C.HERE, f"worker_{N}.pid"), "w").write(str(os.getpid()))
HEART = os.path.join(C.HERE, f"heartbeat_{N}.json")
try:                                      # below-normal CPU priority (ffmpeg children inherit it): the desktop, the
    import psutil                         # supervisor and its PowerShell spawns stay responsive while 8 jobs decode
    psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
except Exception:
    pass


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def beat(v="", st=""):
    try:
        json.dump(dict(pid=os.getpid(), t=time.time(), video=v, stage=st), open(HEART, "w", encoding="utf-8"))
    except OSError:
        pass


def quick_probe(v):
    """the file answers ffprobe (and its last second decodes) right now - used to tell a Drive hiccup from a bad file."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", C.lp(v)],
                           capture_output=True, text=True, timeout=90)
        dur = float(r.stdout.strip())
        r2 = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, dur - 5):.1f}", "-i", C.lp(v), "-frames:v", "1",
                             "-f", "null", "-"], capture_output=True, text=True, timeout=120)
        return r2.returncode == 0
    except Exception:
        return False


def clean_stale_tmp():
    """work dirs of killed workers (refine_*, dlg_*) older than 6 h."""
    for d in glob.glob(os.path.join(tempfile.gettempdir(), "refine_*")) + glob.glob(os.path.join(tempfile.gettempdir(), "dlg_*")):
        try:
            if time.time() - os.path.getmtime(d) > 6 * 3600:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def next_job():
    try:
        q = json.load(open(C.QUEUE, encoding="utf-8"))
    except Exception:
        return None, None, None
    for v in q:
        if C.is_done(v):
            continue
        f = C.fails(v)
        if f["n"] >= C.MAX_FAILS:
            if not C.replaced_since_fail(v, f):
                continue
            C.reset_counters(v)                            # the file was re-downloaded: fresh start
            log(f"   file replaced since it failed - retrying: {v}")
        if C.locked(v) or C.deferred(v):
            continue
        try:
            st = C.state(v)
        except Exception:
            C.note_unreadable(v)
            continue
        if st == "done":
            C.mark_done(v)
            continue
        if st in ("refine", "dialogue") and C.held(v):      # held show: an existing catalogue is never touched
            continue
        try:
            if st in ("index", "refine") and len(C.cast_of(v)) < 3:   # no / broken Cast -> wait (names would be wrong)
                continue
        except OSError:
            C.note_unreadable(v)
            continue
        lk = C.try_lock(v)
        if lk:
            return v, st, lk
    return None, None, None


def cast_now(v):
    """the cast, re-read right before it is used; fewer than 3 names now (Drive hiccup) = temporary, never 'no cast'."""
    cast = C.cast_of(v)
    if len(cast) < 3:
        raise OSError(f"cannot read: cast has {len(cast)} names right now (Drive?)")
    return cast


def run(v, st):
    from media_index import catalog_refine, dialogue_fill
    if st == "index":
        import proxy_videopass
        out = os.path.splitext(v)[0] + ".catalog.json"
        proxy_videopass.build(v, out, cast_now(v), log=lambda *a: beat(v, "index"))   # a beat per chunk
        st = C.state(v)
        log(f"   indexed -> {st}")
    if st in ("refine", "dialogue") and C.held(v):
        log("   held show - catalogue left untouched")
        return
    if st == "refine":
        beat(v, "refine")
        r = catalog_refine.refine(v, cast_now(v), C.show_name(v), progress=lambda s: beat(v, "refine: " + s))
        log(f"   refined: {r}")
        st = C.state(v)
    if st == "dialogue":
        beat(v, "dialogue")
        log(f"   dialogue: {dialogue_fill.fill(v, progress=lambda s: beat(v, 'dialogue: ' + s))}")


log(f"### worker {N} up (pid {os.getpid()})")
clean_stale_tmp()
while True:
    beat()
    if not C.healthy():
        log("proxy/Drive not reachable - waiting")
        while not C.healthy():
            beat()
            time.sleep(60)
        log("back online")
    try:
        v, st, lk = next_job()
    except Exception:                                      # a bug while scanning the queue must not kill the worker
        log("   next_job error: " + traceback.format_exc().splitlines()[-1][:200])
        time.sleep(60)
        continue
    if not v:
        beat()
        time.sleep(600)                                    # queue exhausted: look again in 10 min
        continue
    t = time.time()
    beat(v, st)
    log(f"[{st}] {v}")
    try:
        run(v, st)
        st2 = C.state(v)
        if st2 == "done":
            C.mark_done(v)
        C.reset_counters(v)                               # success: earlier temporary errors no longer count
        if st2 == st:                                     # finished but nothing moved on: never pick it again at once
            C.defer(v)
            log(f"   state still '{st2}' after the run - parked 30 min")
        log(f"   ok ({time.time() - t:.0f}s)")
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:200]}"
        read_err = type(e).__name__ in ("BadSource", "ProbeError") or any(
            k in err for k in ("cannot read", "incomplete read", "not readable", "Errno", "timed out"))
        if read_err and C._volumes().is_cloud(v) and quick_probe(v):
            # 2026-10-13: 56 good Drive files had piled up fail counts (Drive app restart 09:59, the night's net drop);
            # the file reads fine right after the error -> it was Drive, not the file: not counted, retried later
            log(f"   Drive hiccup ({err[:70]}) - the file probes fine now: not counted, retry in 2 min")
            time.sleep(120)
        elif C.drive_restarted_recently(v, minutes=60) and (type(e).__name__ == "BadSource" or
                                              any(k in err for k in ("cannot read", "incomplete read", "not readable", "Errno"))):
            log(f"   Drive app just restarted ({err[:60]}) - waiting 3 min, not counted")
            time.sleep(180)
        elif not C.healthy():
            log(f"   interrupted by outage ({err}) - will redo, not counted")
            time.sleep(60)
        elif "vision unavailable" in err:                  # Gemini quota / proxy down for everyone: not this file
            # 2026-10-12: internet dropped 11:26-17:16 while WiFi stayed 'connected'; every 10 min each worker re-read
            # a whole episode just to fail at verify again. Now: tiny vision probe every 2 min, resume when it answers.
            log(f"   vision outage ({err[:80]}) - probing every 2 min, not counted")
            waited = 0
            while True:
                time.sleep(120)
                waited += 2
                beat(v, f"vision outage - waiting ({waited} min)")
                try:
                    from media_index import catalog_refine
                    if catalog_refine.vision_alive():
                        break
                except Exception:
                    pass
            log(f"   vision back after {waited} min")
        elif any(k in err for k in ("CUDA", "out of memory", "cuBLAS", "CUBLAS", "cudnn", "MemoryError")):
            log(f"   GPU/memory busy ({err[:80]}) - waiting 3 min, not counted")   # other workers' whisper passes
            time.sleep(180)
        elif type(e).__name__ == "BadSource" and C.healthy(v):   # unusable file: no retries, listed for re-download
            for _ in range(C.MAX_FAILS - C.fails(v)["n"]):
                C.add_fail(v, err)
            log(f"   BAD SOURCE (needs a new download): {err}")
        elif not C.healthy(v) or "cannot read" in err or type(e).__name__ == "BadSource":
            # Drive is up but THIS file can't be read right now: park it 30 min (no tight retry loop);
            # after 6 parkings (~3 h) it counts as a real failure
            n = C.defer(v)
            log(f"   file unreadable now ({err[:80]}) - parked 30 min ({n}/6)")
            if n >= 6:
                C.add_fail(v, "unreadable for 3 h: " + err)
        elif any(k in err for k in ("shots verified", "chunks failed", "incomplete read", "429", "Too Many", "timed out",
                                    "timeout")):
            # temporary - but never forever: the same error 4x on one file = a real problem, counted + parked
            n = C.defer(v)
            if n >= 4:
                C.add_fail(v, f"same temporary error {n}x: {err}")
                log(f"   FAILED after {n} temporary errors: {err}")
            else:
                log(f"   temporary ({err}) - file parked 30 min ({n}/4)")
        else:
            C.add_fail(v, err)
            log(f"   FAILED ({C.fails(v)['n']}/{C.MAX_FAILS}): {err}")
            log("   " + traceback.format_exc().splitlines()[-1][:200])
    finally:
        C.unlock(lk)
