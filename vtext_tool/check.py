"""Post-setup check: is VText ready to run on this machine?"""
import importlib

core = ["PIL", "numpy", "imageio_ffmpeg"]


def _ok(mod):
    try:
        importlib.import_module(mod)
        return True
    except Exception:
        return False


missing = [m for m in core if not _ok(m)]
cv = _ok("cv2")
whisper = _ok("faster_whisper")
sphinx = _ok("pocketsphinx")

if missing:
    print(f"NOT READY - missing core packages: {missing}")
    print("Re-run setup.bat; if it keeps failing, install Python 3.13 "
          "from python.org and run setup.bat again with it.")
elif whisper or sphinx:
    engine = "faster-whisper" if whisper else "pocketsphinx (offline fallback)"
    print(f"READY  - speech engine: {engine}"
          + ("" if cv else "  (face detection off: opencv missing)"))
    print("Run run.bat to open the GUI.")
else:
    print("NOT READY - no speech engine installed.")
    print("Try:  python -m pip install faster-whisper")
    print("If that fails on Python 3.14, install Python 3.13 from "
          "python.org, then re-run setup.bat.")
