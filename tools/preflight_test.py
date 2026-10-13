"""Exercise editv2.pipeline.preflight with a good and a bad job (run it outside Claude via tools\\run_outside.ps1)."""
import sys, time
sys.path.insert(0, r"D:\Lib")
from editv2.pipeline import preflight

d = r"D:\Movies\English\Reacher\01 Why Reacher Season 1 Still Feels Untouchable"
voice = r"D:\Imp Files\Audio Samples FINAL\Sheldon Cooper Audio.mp3"
for name, args in [
    ("good (fresh build, voice sample)", (d + r"\Script.txt", r"D:\Lib\tools\_pf_empty_build", "", voice, r"D:\Movies\English\Reacher\Final Videos")),
    ("bad (no voice sample, no audio)", (d + r"\Script.txt", r"D:\Lib\tools\_pf_empty_build", "", "", "")),
    ("bad (wrong audio path)", (d + r"\Script.txt", r"D:\Lib\tools\_pf_empty_build", r"D:\nope\x.wav", "", "")),
]:
    t = time.time()
    bad = preflight(*args)
    print(f"{name}: {time.time() - t:.1f}s -> {'PASS' if not bad else bad}")
