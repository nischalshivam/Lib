"""Run by Task Scheduler at logon and every 10 min: keeps `workers` library workers alive and writes status.txt.
Rebuilds queue.json when a newer scan_all.json exists; starts a library rescan once a day (new uploads)."""
import json, os, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

PYW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
if not os.path.exists(PYW):
    PYW = sys.executable
FLAGS = 0x00000008 | 0x00000200 | 0x01000000            # DETACHED | NEW_PROCESS_GROUP | BREAKAWAY_FROM_JOB
SLOG = open(os.path.join(C.LOGS, "supervisor.log"), "a", encoding="utf-8", buffering=1)


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, file=SLOG, flush=True)


def spawn(args, out=None):
    """Start through WMI: the new process belongs to no job object (Task Scheduler / Claude cannot take it down)."""
    cmd = " ".join(f'"{a}"' for a in args).replace("'", "''")
    try:                                      # a WMI call can hang under load (11:00 2026-10-10): never block the
        r = subprocess.run(["powershell", "-NoProfile", "-Command",       # supervisor - it is the stall guard too
                            f"(Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
                            f"@{{CommandLine='{cmd}'; CurrentDirectory='{C.HERE}'}}).ProcessId"],
                           capture_output=True, text=True, creationflags=0x08000000, timeout=120)
        pid = int(r.stdout.strip().splitlines()[-1])
    except subprocess.TimeoutExpired:
        log(f"spawn timed out (WMI busy): {args[-1]}")
        pid = 0
    except Exception:
        pid = 0
    if not pid:                               # PowerShell itself can take > 2 min to start when every core is busy
        try:                                  # decoding video: start it directly, outside this task's job object
            pid = subprocess.Popen(args, cwd=C.HERE, creationflags=FLAGS | 0x08000000, close_fds=True,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).pid
            log(f"spawned directly instead: {args[-1]} pid {pid}")
        except Exception as e:
            log(f"direct spawn failed too: {e}")

    class P:
        pass
    p = P(); p.pid = pid
    return p


def alive(name, must):
    p = os.path.join(C.HERE, name)
    try:
        return C.pid_alive(int(open(p).read().strip()), must)
    except Exception:
        return False


# 1) queue from the newest scan
scan = os.path.join(C.HERE, "scan_all.json")
if os.path.exists(scan) and (not os.path.exists(C.QUEUE) or os.path.getmtime(scan) > os.path.getmtime(C.QUEUE)):
    subprocess.run([sys.executable, os.path.join(C.HERE, "build_queue.py")], cwd=C.HERE)
    log("queue rebuilt from scan")

# 2) daily rescan for new uploads
stamp = os.path.join(C.HERE, "last_scan.txt")
last = float(open(stamp).read()) if os.path.exists(stamp) else 0
if time.time() - last > 24 * 3600 and not alive("scan.pid", "scan_all.py"):
    p = spawn([PYW, os.path.join(C.HERE, "scan_all.py")])
    open(os.path.join(C.HERE, "scan.pid"), "w").write(str(p.pid))
    open(stamp, "w").write(str(time.time()))
    log(f"daily library rescan started (pid {p.pid})")

# 2b) subtitles: (re)run the matcher when the user added files or it was interrupted
SUBS = r"C:\Users\Dell\Desktop\Subtitles"
newest = max((os.path.getmtime(os.path.join(dp, f)) for dp, dn, fn in os.walk(SUBS) for f in fn), default=0) if os.path.isdir(SUBS) else 0
done_flag = os.path.join(C.HERE, "subs_done.txt")
if os.path.isdir(SUBS) and not alive("subs.pid", "subs_install.py") and \
        (not os.path.exists(done_flag) or newest > os.path.getmtime(done_flag)):
    p = spawn([PYW, os.path.join(C.HERE, "subs_install.py")])
    log(f"subtitle matcher started (pid {p.pid})")

# 2d) PROXY WATCHDOG. 2026-10-13: the internet dropped 01:56-01:58; every proxy account failed its OAuth token refresh
#     and CLIProxyAPI kept them 'auth_unavailable' after the net came back - indexing and the editor waited 7 h. The
#     keepalive task only checks that the port listens. If most workers report a vision outage for 15+ min while
#     Google is reachable and a tiny vision probe still fails, restart the proxy (at most once per 30 min).
def _proxy_watchdog():
    import glob, socket
    hbs = []
    for f in glob.glob(os.path.join(C.HERE, "heartbeat_*.json")):
        try:
            hbs.append(json.load(open(f, encoding="utf-8")))
        except Exception:
            pass
    fresh = [h for h in hbs if time.time() - h.get("t", 0) < 900]
    waiting = [h for h in fresh if "vision outage" in str(h.get("stage", ""))]
    mins = [int(m) for h in waiting for m in __import__("re").findall(r"\((\d+) min", str(h.get("stage", "")))]
    if not fresh or len(waiting) < max(1, len(fresh) // 2) or not mins or max(mins) < 15:
        return
    state = os.path.join(C.HERE, "proxy_restart.json")
    try:
        last_restart = json.load(open(state)).get("t", 0)
    except Exception:
        last_restart = 0
    if time.time() - last_restart < 1800:
        return
    try:
        socket.create_connection(("oauth2.googleapis.com", 443), timeout=8).close()
    except OSError:
        log("proxy watchdog: workers wait on vision but Google is unreachable (internet down) - not restarting")
        return
    try:
        sys.path.insert(0, os.path.dirname(C.HERE))
        import io
        from PIL import Image
        from editv2 import common as E
        b = io.BytesIO(); Image.new("RGB", (64, 36), (90, 120, 160)).save(b, "JPEG")
        if E.vision('What colour is this image? Reply ONLY JSON {"colour":"..."}', b.getvalue(), tries=1, timeout=60):
            return                                         # vision answers again: the workers' own probes will see it
    except Exception as e:
        log(f"proxy watchdog: probe error {type(e).__name__}: {str(e)[:120]}")
    log(f"proxy watchdog: {len(waiting)}/{len(fresh)} workers in a vision outage for {max(mins)} min, internet up, "
        "probe failed -> restarting the proxy (stuck auth after a network drop)")
    subprocess.run(["taskkill", "/IM", "cli-proxy-api.exe", "/F"], capture_output=True, creationflags=0x08000000)
    time.sleep(3)
    start = os.path.join(os.path.expanduser("~"), "cli-proxy-api", "start-proxy.ps1")
    spawn(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-File", start])
    json.dump({"t": time.time(), "why": f"{len(waiting)} workers waiting {max(mins)} min"}, open(state, "w"))


try:
    _proxy_watchdog()
except Exception as _e:
    log(f"proxy watchdog error: {type(_e).__name__}: {str(_e)[:150]}")

# 2c) STALL GUARD: a worker whose heartbeat is older than 60 min is stuck (e.g. inside a hung Google Drive read,
#     6 workers froze for 34 min on 2026-10-10). Kill it; if it will not die, restart Google Drive for desktop
#     (that is what released the frozen reads), then let step 3 start a fresh worker.
DRIVE_LAUNCH = r"C:\Program Files\Google\Drive File Stream\launch.bat"
stuck_alive = []
for hb in [f for f in os.listdir(C.HERE) if f.startswith("heartbeat_") and f.endswith(".json")]:
    try:
        h = json.load(open(os.path.join(C.HERE, hb), encoding="utf-8"))
    except Exception:
        continue
    n = hb[len("heartbeat_"):-len(".json")]
    if not C.pid_alive(h.get("pid", 0), "worker.py") or time.time() - h.get("t", 0) < 3600:
        continue
    log(f"STALL: worker {n} (pid {h['pid']}) silent for {(time.time() - h['t']) / 60:.0f} min on {h.get('video', '')[-80:]}"
        f" at '{h.get('stage', '')}' - killing")
    subprocess.run(["taskkill", "/PID", str(h["pid"]), "/T", "/F"], capture_output=True, creationflags=0x08000000)
    time.sleep(20)
    if C.pid_alive(h["pid"], "worker.py"):
        stuck_alive.append(h)                 # blocked inside Drive I/O: Drive's fault, the file is only parked
        if h.get("video"):
            C.defer(h["video"])
    elif h.get("video"):
        k = C.defer(h["video"])               # the file stalled a killable worker: 3 times = a real failure, no endless cycle
        if k >= 3:
            C.add_fail(h["video"], f"killed by the stall guard {k}x (last stage '{h.get('stage', '')}')")
            log(f"STALL: {h['video'][-80:]} stalled {k}x - counted as failed")
if stuck_alive and os.path.exists(DRIVE_LAUNCH):
    log(f"STALL: {len(stuck_alive)} worker(s) cannot be killed (blocked in Drive I/O) - restarting Google Drive for desktop")
    subprocess.run(["taskkill", "/IM", "GoogleDriveFS.exe", "/F"], capture_output=True, creationflags=0x08000000)
    time.sleep(10)
    subprocess.Popen(["cmd", "/c", DRIVE_LAUNCH], creationflags=0x08000000 | 0x00000008)
    for _ in range(18):                       # up to 3 min for the drive letters to come back
        time.sleep(10)
        if C.drive_ok():
            break
    log(f"STALL: Drive back: {C.drive_ok()}")

# 2e) STOP WHEN THE PRIORITY LIST IS DONE (user 2026-10-13: finish only.json's shows, then free the PC for the Movie
#     Editor). Lives here, not in a separate watcher: the PC crashed / is restarted and the supervisor is the one
#     thing Task Scheduler always brings back.
def _priority_done():
    if not os.path.exists(os.path.join(C.HERE, "only.json")):
        return False
    q = json.load(open(C.QUEUE, encoding="utf-8"))
    return bool(q) and all(C.is_done(v) or C.fails(v)["n"] >= C.MAX_FAILS for v in q)


try:
    if int(C.cfg().get("workers", 3)) > 0 and _priority_done():
        json.dump({"workers": 0}, open(os.path.join(C.HERE, "config.json"), "w"))
        import psutil
        for _p in psutil.process_iter(["cmdline"]):
            if "worker.py" in " ".join(_p.info["cmdline"] or []):
                try:
                    _p.kill()
                except Exception:
                    pass
        log("PRIORITY LIST DONE - indexing stopped (config workers=0); the PC is free for the Movie Editor")
        open(os.path.join(os.path.dirname(C.HERE), "tools", "indexing_stop.txt"), "a", encoding="utf-8").write(
            time.strftime("%Y-%m-%d %H:%M ") + "PRIORITY DONE - indexing stopped by the supervisor\n")
except Exception as _e:
    log(f"priority-stop check error: {type(_e).__name__}: {_e}")

# 3) workers
want = int(C.cfg().get("workers", 3))
for i in range(want):
    if not alive(f"worker_{i}.pid", "worker.py"):
        p = spawn([PYW, os.path.join(C.HERE, "worker.py"), str(i)])
        log(f"worker {i} (re)started pid {p.pid}")

# 4) status
def no_cast(v):
    try:
        return len(C.cast_of(v, allow_llm=False)) < 3
    except OSError:
        return False                          # unreadable right now: not reported as missing


try:
    q = json.load(open(C.QUEUE, encoding="utf-8"))
    fails = [json.load(open(os.path.join(C.FAILS, f), encoding="utf-8")) for f in os.listdir(C.FAILS)]
    hard = [f for f in fails if f["n"] >= C.MAX_FAILS]
    try:
        import watchdog
        wd = watchdog.run()
    except Exception as e:
        wd = f"watchdog error: {e}\n"
        log(wd.strip())
    with open(os.path.join(C.HERE, "status.txt"), "w", encoding="utf-8") as f:
        f.write(f"updated {time.strftime('%Y-%m-%d %H:%M')}\n{wd}queue: {len(q)} videos\n"
                f"workers wanted {want}; proxy {'OK' if C.proxy_ok() else 'DOWN'}; Drive {'OK' if C.drive_ok() else 'DOWN'}\n"
                f"done (current version): {sum(1 for f in os.listdir(C.DONE) if open(os.path.join(C.DONE, f)).read().strip() == str(__import__('media_index.catalog_refine', fromlist=['VERSION']).VERSION))}\n"
                f"waiting for a Cast folder (put it in Desktop\\Cast\\<show or film title>): "
                f"{sorted({C.show_name(v) if C.is_episode(v) else os.path.basename(v)[:50] for v in q if not C.is_done(v) and not C.held(v) and no_cast(v)})}\n"
                f"failed 3x (need a look): {len(hard)}\n" + "".join(f"  {x['video']}\n    {x['errors'][-1]}\n" for x in hard[:50]))
        rp = os.path.join(C.HERE, "subs_report.json")
        if os.path.exists(rp):
            sr = json.load(open(rp, encoding="utf-8"))
            from collections import Counter
            cnt = Counter(v["status"] for v in sr.values())
            f.write(f"\nSUBTITLES  installed {cnt.get('installed', 0)}, rejected {cnt.get('rejected', 0)}, "
                    f"no file {cnt.get('no-subtitle', 0)}, error {cnt.get('error', 0)}\n")
            for v, r in sr.items():
                if r["status"] in ("rejected", "no-subtitle"):
                    f.write(f"  {r['status']:11s} {r.get('ep', '')}  {v}\n")
except Exception as e:
    log(f"status failed: {e}")
