"""Start the next Movie Editor batch by itself once the running one (the GUI's) is finished: no 'editv2 build' process
for QUIET minutes. Then the given channel folders run exactly like the GUI does (studio.build_batch, the settings of the
GUI's last batch), skipping every topic whose video is already in that channel's Final Videos (and topic 01 by request).
Each video goes to ITS channel's own FINAL Videos folder.
usage: py tools/autostart_after.py <log> "<channel folder>" ["<channel folder>" ...]"""
import glob, json, os, subprocess, sys, time
sys.path.insert(0, r"D:\Lib")
log_file, channels = sys.argv[1], sys.argv[2:]
QUIET = 10
L = open(log_file, "a", encoding="utf-8", buffering=1)


def log(*a):
    L.write(time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a) + "\n")


def builds_running():
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "@(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object "
                          "{ $_.CommandLine -match 'editv2 build' -or $_.CommandLine -match 'editv2\" build' }).Count"],
                         capture_output=True, text=True, creationflags=0x08000000).stdout.strip()
    try:
        return int(out or 0)
    except ValueError:
        return 1


log(f"### waiting for the running batch to finish (no build process for {QUIET} min), then: {channels}")
quiet_since = None
while True:
    n = builds_running()
    if n:
        quiet_since = None
    else:
        quiet_since = quiet_since or time.time()
        if time.time() - quiet_since >= QUIET * 60:
            break
    time.sleep(60)
log("### the previous batch is finished - starting")

import studio
tpl = json.load(open(r"D:\Lib\editv2\cache\last_batch.json", encoding="utf-8"))["specs"][0]
jobs, names = [], []
for ch in channels:
    final = next((d for d in glob.glob(os.path.join(glob.escape(ch), "*")) if os.path.basename(d).lower() == "final videos"),
                 os.path.join(ch, "FINAL Videos"))
    made = {os.path.splitext(f)[0].lower() for f in os.listdir(final)} if os.path.isdir(final) else set()
    for topic in sorted(d for d in glob.glob(os.path.join(glob.escape(ch), "*")) if os.path.isdir(d) and d != final):
        name = os.path.basename(topic)
        if name.startswith("01 ") or name.lower() in made:
            log(f"  skip (already made): {name}")
            continue
        script = os.path.join(topic, "Script.txt")
        js = [f for f in glob.glob(os.path.join(glob.escape(topic), "*.json"))]
        if not os.path.exists(script) or len(js) != 1:
            log(f"  !! skip {name}: Script.txt={os.path.exists(script)}, json files={len(js)}")
            continue
        s = dict(tpl, clean=script, clue=js[0], save_dir=final)
        jobs.append(studio.Job(
            clean=s["clean"], clue=s["clue"], audio=s.get("audio", ""), save_dir=s["save_dir"],
            fmt=s.get("fmt", "auto"), resolution=s.get("res", "1080p"), language=s.get("lang", "en"),
            text=bool(s.get("text")), verify=bool(s.get("verify")), verify_intro_min=int(s.get("verify_min", 0) or 0),
            cold_open=bool(s.get("cold_open")), intro_punch=bool(s.get("intro_punch")), ken_burns=bool(s.get("ken_burns")),
            frame=bool(s.get("frame")), kinetic_text=bool(s.get("kinetic_text")), text_file=s.get("text_file", ""),
            auto_voice=bool(s.get("auto_voice")), voice_ref=s.get("voice_ref", ""), index=len(jobs),
            engine=s.get("engine", "classic"), look=s.get("look", "auto")))
        names.append(f"{os.path.basename(ch)}/{name}")
log(f"### batch of {len(jobs)}: " + " | ".join(names))
studio.build_batch(jobs, log=log, on_status=lambda j, st: log(f"STATUS {'ALL' if j is None else names[j.index]}: {st}"))
log("### finished: " + ", ".join(f"{n[:12]}={j.status}" for n, j in zip(names, jobs)))
