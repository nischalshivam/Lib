"""Library coverage scan (footage volumes from libjobs.volumes only - never the backup Drive / SSD Data). Per show/film folder: video files, complete catalogs, pending.
Episodes = SxxEyy / 1x01 names; films = video >1 GB without an episode code. Writes scan_all.json."""
import json, os, re, sys, time
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
sys.stdout = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "scan.log"), "a", encoding="utf-8", buffering=1)
sys.path.insert(0, r"D:\Lib\agy_rotation"); sys.path.insert(0, r"D:\Lib")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from proxy_videopass import _lp
from media_index import catalog

VID = (".mkv", ".mp4", ".avi", ".m4v")
# footage roots by VOLUME IDENTITY (label/serial/Google account), not drive letter - letters move (2026-10-12)
from libjobs import volumes
ROOTS = volumes.roots()
EP = re.compile(r"S\d\dE\d\d|\b\d{1,2}x\d{2}\b", re.I)
out = {}
t0 = time.time()
for root in ROOTS:
    if not os.path.isdir(root):
        continue
    for show in sorted(os.listdir(root)):
        top = os.path.join(root, show)
        if not os.path.isdir(top) or show.startswith("."):
            continue
        files = []
        for dp, dn, fn in os.walk(top):
            if os.sep + "Cast" in dp or "Featurettes" in dp or "Extras" in dp:
                continue
            for f in fn:
                if not f.lower().endswith(VID) or "sample" in f.lower() or "trailer" in f.lower():
                    continue
                p = os.path.join(dp, f)
                ep = bool(EP.search(f))
                try:
                    big = os.path.getsize(_lp(p)) > 700_000_000
                except OSError:
                    big = False
                if not ep and not big:
                    continue
                cat = os.path.splitext(p)[0] + ".catalog.json"
                try:
                    done = catalog._is_complete(_lp(cat))
                except Exception:
                    done = False
                files.append(dict(p=p, ep=ep, done=done))
        if files:
            out[top] = files
            n, d = len(files), sum(f["done"] for f in files)
            print(f"{d:4d}/{n:<4d} {'DONE ' if d == n else 'TODO '} {top}", flush=True)
json.dump(out, open(os.path.join(os.path.dirname(__file__), "scan_all.json"), "w", encoding="utf-8"), ensure_ascii=False)
print(f"scan done in {time.time() - t0:.0f}s: {sum(len(v) for v in out.values())} files, "
      f"{sum(f['done'] for v in out.values() for f in v)} indexed")
