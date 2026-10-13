"""Check every dependency of the Movie Editor (editv2 engine) the way the GUI sees it.

Run it OUTSIDE the Claude desktop app (tools\run_outside.ps1 launches it through WMI). Claude is an MSIX package: what
Claude's own processes install under AppData lands in Packages\\Claude_*\\LocalCache and is invisible to the GUI, so a
check run from inside Claude can pass while the GUI fails (2026-10-12: HyperFrames).
    py tools\\envcheck.py  ->  prints one line per check and writes tools\\envcheck.json
"""
import importlib, json, os, shutil, subprocess, sys

sys.path.insert(0, r"D:\Lib")
out = []


def check(name, fn):
    try:
        ok, info = fn()
    except Exception as e:                                   # noqa: BLE001
        ok, info = False, f"{type(e).__name__}: {e}"
    out.append({"check": name, "ok": bool(ok), "info": str(info)[:300]})
    print(("OK   " if ok else "FAIL ") + name + " - " + str(info)[:200], flush=True)


def run(cmd):
    r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=180)
    return r.returncode == 0, (r.stdout or r.stderr).strip().splitlines()[-1:] or [""]


def hf():
    from editv2.pipeline import _find_hyperframes
    p, tried = _find_hyperframes()
    if not p:
        return False, "not found in " + "; ".join(tried[:4])
    ok, v = run(f'"{p}" --version')
    return ok, f"{p} -> {v[0]}"


def importable(mod):
    return lambda: (importlib.import_module(mod) is not None, mod)


def exists(path):
    path = os.path.expanduser(path)
    return lambda: (os.path.exists(path), path)


def chrome():
    import glob
    hits = glob.glob(os.path.expanduser(r"~\.cache\hyperframes\chrome\**\chrome-headless-shell.exe"), recursive=True)
    return bool(hits), hits[0] if hits else "no chrome-headless-shell under ~/.cache/hyperframes"


def whisper_model():
    from faster_whisper import WhisperModel
    WhisperModel("small.en", device="cpu", compute_type="int8")
    return True, "small.en loads"


def proxy():
    from editv2 import common as E
    import urllib.request
    req = urllib.request.Request(E.PROXY.rsplit("/chat/", 1)[0] + "/models", headers={"Authorization": "Bearer " + E.KEY})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status == 200, f"{E.PROXY} {r.status}"


def omnivoice():
    import requests
    try:
        r = requests.get("http://127.0.0.1:8001/api/ready", timeout=8)
        return True, f"answers ({r.status_code}) - the tool starts/restarts it itself when needed"
    except Exception:
        return os.path.isfile(r"D:\omnivoice-wrapper\run.bat"), "not running; run.bat present -> tool auto-starts it"


check("python", lambda: (True, sys.executable + " " + sys.version.split()[0]))
check("hyperframes CLI", hf)
check("node", lambda: (shutil.which("node") or os.path.isfile(r"C:\Program Files\nodejs\node.exe"),
                       shutil.which("node") or r"C:\Program Files\nodejs\node.exe"))
check("chrome-headless-shell (render)", chrome)
for tool in ("ffmpeg", "ffprobe"):
    check(tool, lambda t=tool: (shutil.which(t) is not None, shutil.which(t)))
for mod in ("faster_whisper", "ctranslate2", "rembg", "onnxruntime", "cv2", "PIL", "numpy", "scipy", "requests", "psutil",
            "webview", "studio", "editv2.pipeline", "editv2.plan", "editv2.cut", "editv2.look", "editv2.mix", "editv2.qa"):
    check("import " + mod, importable(mod))
for p in (r"~\.model-router-proxy-key.txt", r"D:\Lib\models\face_detection_yunet_2023mar.onnx", r"~\.u2net",
          r"C:\Users\Dell\Desktop\ProStudio\BGM", r"C:\Users\Dell\Desktop\ProStudio\backgrounds",
          r"D:\Lib\hyperframes_lab\arya_demo\assets", r"D:\Lib\hyperframes_lab\demo", r"D:\Lib\editv2\fonts",
          r"D:\omnivoice-wrapper\venv\Scripts\python.exe", r"D:\Lib\secrets\serper.txt"):
    check("path " + p, exists(p))
check("agy CLI", lambda: (shutil.which("agy") is not None, shutil.which("agy")))
check("model proxy :8317", proxy)
check("OmniVoice", omnivoice)
check("whisper small.en model", whisper_model)
json.dump(out, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "envcheck.json"), "w"), indent=1)
bad = [c["check"] for c in out if not c["ok"]]
print("\nRESULT:", "ALL OK" if not bad else "FAILED: " + ", ".join(bad))
