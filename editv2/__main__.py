"""python -m editv2 build  <clean.txt> [--audio a.wav] [--lang en] [--look auto|A|B|C] [--out dir] [--save dir] [--voice-ref ref.wav]
python -m editv2 clues  <folder of clean .txt> [--lang en]
python -m editv2 styles  (rebuild the show typography preview)"""
import argparse, sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from . import pipeline  # noqa: E402

ap = argparse.ArgumentParser(prog="editv2")
ap.add_argument("cmd", choices=["build", "clues", "styles"])
ap.add_argument("path", nargs="?")
ap.add_argument("--audio", default="")
ap.add_argument("--lang", default="en")
ap.add_argument("--look", default="auto")
ap.add_argument("--out", default="")
ap.add_argument("--save", default="")
ap.add_argument("--voice-ref", default="")
ap.add_argument("--until", default="", help="online = stop after look (all internet work done)")
a = ap.parse_args()
if a.cmd == "build" and a.until == "online":
    # 2026-10-14 pipelined batch: two internet phases (cut = 8 ffmpeg, words = whisper on 16 threads) beside a render
    # held the CPU at 50-95 % and the render's capture fell to ~8 fps. Internet phases now yield the CPU to the render
    # (their ffmpeg / whisper children inherit the class); they have slack, the render is the bottleneck.
    try:
        import psutil
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass
if a.cmd == "build":
    try:
        print(pipeline.build(a.path, a.out, a.audio, a.lang, a.look, a.save, a.voice_ref, until=a.until or None))
    except Exception as e:                       # the reason must also land in build.log, not only in the GUI window
        import os, time, traceback
        bd = a.out or os.path.join(os.path.dirname(os.path.abspath(a.path)), os.path.splitext(os.path.basename(a.path))[0] + "_v2build")
        try:
            with open(os.path.join(bd, "build.log"), "a", encoding="utf-8") as f:
                f.write(f"\n[{time.strftime('%H:%M:%S')}] FAILED: {type(e).__name__}: {e}\n{traceback.format_exc()}\n")
        except OSError:
            pass
        raise
elif a.cmd == "clues":
    for f, st, info in pipeline.prepare_clues(a.path, a.lang):
        print(f, st, info)
else:
    import json, os
    from . import styles
    d = json.load(open(os.path.join(os.path.dirname(__file__), "cache", "styles.json"), encoding="utf-8"))
    print(styles.preview([(k, v.get("show", k)) for k, v in d.items()], os.path.join(os.path.dirname(__file__), "cache", "STYLES_PREVIEW.jpg")))
