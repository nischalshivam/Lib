"""Run Movie Editor jobs exactly the way the GUI does (movie_editor.Api._run -> studio.build_batch), headless, with the
settings of the GUI's last batch (editv2/cache/last_batch.json) for the given topic folders.
usage: py tools/run_batch.py <log file> "<topic folder>" ["<topic folder>" ...]"""
import json, os, sys, time
sys.path.insert(0, r"D:\Lib")
import studio

log_file, folders = sys.argv[1], sys.argv[2:]
specs = json.load(open(r"D:\Lib\editv2\cache\last_batch.json", encoding="utf-8"))["specs"]
L = open(log_file, "a", encoding="utf-8", buffering=1)


def log(*a):
    L.write(time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a) + "\n")


jobs = []
for i, f in enumerate(folders):
    clean = os.path.join(f, "Script.txt")
    s = next((x for x in specs if os.path.normcase(x["clean"]) == os.path.normcase(clean)), None)
    if not s:
        sys.exit(f"no GUI spec for {clean}")
    jobs.append(studio.Job(
        clean=s.get("clean", ""), clue=s.get("clue", ""), audio=s.get("audio", ""), save_dir=s.get("save_dir", ""),
        fmt=s.get("fmt", "auto"), resolution=s.get("res", "1080p"), language=s.get("lang", "en"),
        text=bool(s.get("text")), verify=bool(s.get("verify")), verify_intro_min=int(s.get("verify_min", 0) or 0),
        cold_open=bool(s.get("cold_open")), intro_punch=bool(s.get("intro_punch")), ken_burns=bool(s.get("ken_burns")),
        frame=bool(s.get("frame")), kinetic_text=bool(s.get("kinetic_text")), text_file=s.get("text_file", ""),
        auto_voice=bool(s.get("auto_voice")), voice_ref=s.get("voice_ref", ""), index=i,
        engine=s.get("engine", "classic"), look=s.get("look", "auto")))
log(f"### batch of {len(jobs)}: " + " | ".join(os.path.basename(f) for f in folders))
studio.build_batch(jobs, log=log, on_status=lambda j, st: log(f"STATUS {'ALL' if j is None else j.index + 1}: {st}"))
log("### finished: " + ", ".join(f"{os.path.basename(f)[:2]}={j.status}" for f, j in zip(folders, jobs)))
