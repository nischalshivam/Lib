"""Resume the Movie Editor's last batch WITHOUT the window - exactly what its 'Resume last batch' button does
(movie_editor.Api.resume_batch -> _run -> studio.build_batch): the unfinished videos of editv2/cache/last_batch.json,
each with its own language / voice sample / clue / save folder; every build folder continues from its last stage.
Stop: create the file D:\\Lib\\tools\\STOP_BATCH (the running child processes are killed, everything resumes later).
usage: py tools/resume_last_batch.py <log file>"""
import json, os, sys, time
sys.path.insert(0, r"D:\Lib")
import studio

log_file = sys.argv[1]
STOP = r"D:\Lib\tools\STOP_BATCH"
L = open(log_file, "a", encoding="utf-8", buffering=1)


def log(*a):
    L.write(time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a) + "\n")


b = json.load(open(studio.BATCH_FILE, encoding="utf-8"))
specs = b["specs"]
todo = [(i, s) for i, s in enumerate(specs) if b["state"].get(str(i)) != "done"]
if os.path.exists(STOP):
    os.remove(STOP)
jobs = []
for n, (i, s) in enumerate(todo):
    jobs.append(studio.Job(
        clean=s.get("clean", ""), clue=s.get("clue", ""), audio=s.get("audio", ""), save_dir=s.get("save_dir", ""),
        fmt=s.get("fmt", "auto"), resolution=s.get("res", "1080p"), language=s.get("lang", "en"),
        text=bool(s.get("text")), verify=bool(s.get("verify")), verify_intro_min=int(s.get("verify_min", 0) or 0),
        cold_open=bool(s.get("cold_open")), intro_punch=bool(s.get("intro_punch")), ken_burns=bool(s.get("ken_burns")),
        frame=bool(s.get("frame")), kinetic_text=bool(s.get("kinetic_text")), text_file=s.get("text_file", ""),
        auto_voice=bool(s.get("auto_voice")), voice_ref=s.get("voice_ref", ""), index=n,
        engine=s.get("engine", "classic"), look=s.get("look", "auto")))
orig = {n: i for n, (i, _) in enumerate(todo)}


def on_status(job, st):
    if job is None:
        log(f"STATUS ALL: {st}")
        return
    log(f"STATUS {os.path.basename(os.path.dirname(job.clean))}: {st}")
    if st in ("done", "failed", "online"):          # keep last_batch.json current so the GUI / a later resume skips done ones
        try:
            cur = json.load(open(studio.BATCH_FILE, encoding="utf-8"))
            if cur.get("specs") == specs:
                cur["state"][str(orig[job.index])] = st
                json.dump(cur, open(studio.BATCH_FILE + ".tmp", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                os.replace(studio.BATCH_FILE + ".tmp", studio.BATCH_FILE)
        except Exception as e:                       # noqa: BLE001
            log(f"  (state not saved: {e})")


log(f"### resume of the last batch ({b.get('saved')}): {len(jobs)} of {len(specs)} videos")
studio.build_batch(jobs, log=log, should_stop=lambda: os.path.exists(STOP), on_status=on_status)
log("### finished: " + ", ".join(f"{os.path.basename(os.path.dirname(j.clean))[:20]}={j.status}" for j in jobs))
