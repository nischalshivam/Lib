"""Online-only resume (clue/voice/plan/cut/look, no render) for stopped videos of a channel folder; one after another.
usage: py tools/retry_online.py <log> "<channel folder>" "<part of the topic name>" ..."""
import glob, os, subprocess, sys, time

log, base, prefixes = sys.argv[1], sys.argv[2], sys.argv[3:]
VOICE = r"D:\Imp Files\Audio Samples FINAL\Sheldon Cooper Audio.mp3"
save = next((d for d in glob.glob(os.path.join(base, "*")) if os.path.basename(d).lower() == "final videos"), os.path.join(base, "Final Videos"))
L = open(log, "a", encoding="utf-8", buffering=1)
for pre in prefixes:
    topic = next(d for d in glob.glob(os.path.join(glob.escape(base), "*")) if pre.lower() in os.path.basename(d).lower())
    script = next(os.path.join(topic, f) for f in os.listdir(topic) if f.lower().endswith(".txt") and not f.lower().endswith("_qa.txt"))
    out = os.path.splitext(script)[0] + "_v2build"
    L.write(f"{time.strftime('%H:%M:%S')} ### online resume: {os.path.basename(topic)}\n")
    r = subprocess.run([sys.executable, "-X", "utf8", "-u", "-m", "editv2", "build", script, "--out", out, "--lang", "en",
                        "--look", "auto", "--voice-ref", VOICE, "--save", save, "--until", "online"],
                       cwd=r"D:\Lib", stdout=L, stderr=subprocess.STDOUT)
    L.write(f"{time.strftime('%H:%M:%S')} ### rc={r.returncode}: {os.path.basename(topic)}\n")
