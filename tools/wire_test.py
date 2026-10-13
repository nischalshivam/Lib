"""The Wire test video (user 2026-10-14): once the night's priority indexing queue is done, build ONE The Wire video
unattended, exactly the way the Movie Editor does it (studio.build_batch: internet phase -> green signal -> offline
phase; auto voiceover from the Sheldon Cooper sample; pro engine v2, look auto; GUI default switches). It is the full
test before the user queues the other 29 Wire videos.

Started once by libjobs\\supervisor.py (marker tools\\wire_test.started) through tools\\wire_test.cmd.
Progress: tools\\wire_test_status.txt (short) + tools\\wire_test.log (full). Result also on the Desktop.
    py tools\\wire_test.py            run now (still waits for indexing / another build to finish)
    py tools\\wire_test.py --check    only build the job and print it (no build)
"""
import os, subprocess, sys, time
sys.path.insert(0, r"D:\Lib")
import studio

ROOT = r"D:\Movies\English\The Wire"
TOPIC = "Stringer Bell's business model vs. Prop Joe's business model"      # S1-S5 characters, 4.4k words (~30 min)
VOICE = r"D:\Imp Files\Audio Samples FINAL\Sheldon Cooper Audio.mp3"
SAVE = os.path.join(ROOT, "Final Videos")
HERE = os.path.dirname(os.path.abspath(__file__))
STATUS = os.path.join(HERE, "wire_test_status.txt")
DESK = os.path.join(os.path.expanduser("~"), "Desktop", "WIRE TEST VIDEO - RESULT.txt")


def status(msg):
    line = time.strftime("%Y-%m-%d %H:%M ") + msg
    print(line, flush=True)
    with open(STATUS, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def busy():
    """indexing workers or another editv2 build still running -> wait (a render next to CUDA whisper = BSOD 0xD1)."""
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "
                          "'worker\\.py|-m editv2 build' } | ForEach-Object { $_.CommandLine }"],
                         capture_output=True, text=True, creationflags=0x08000000).stdout
    # the PowerShell query's own command line contains the pattern too - never count it
    return [ln for ln in out.splitlines() if ln.strip() and "wire_test" not in ln and "Get-CimInstance" not in ln]


def make_job():
    folder = os.path.join(ROOT, TOPIC)
    script = os.path.join(folder, TOPIC + ".txt")
    if not os.path.isfile(script):
        raise SystemExit(f"script missing: {script}")
    if not os.path.isfile(VOICE):
        raise SystemExit(f"voice sample missing: {VOICE}")
    os.makedirs(SAVE, exist_ok=True)
    # the same fields movie_editor._run fills from the GUI defaults (movie_editor_ui.html) + auto voice
    return studio.Job(clean=script, clue="", audio="", save_dir=SAVE, fmt="auto", resolution="1080p", language="en",
                      text=False, verify=True, verify_intro_min=0, cold_open=True, intro_punch=True, ken_burns=True,
                      frame=True, kinetic_text=True, text_file="", auto_voice=True, voice_ref=VOICE, index=0,
                      engine="v2", look="auto")


def main():
    job = make_job()
    if "--check" in sys.argv:
        print(job)
        print("busy now:", busy()[:3])
        return
    status(f"START wire test: {TOPIC}")
    t0 = time.time()
    while True:
        b = busy()
        if not b:
            break
        if time.time() - t0 > 6 * 3600:
            status("GAVE UP: indexing / another build still running after 6 h: " + b[0][:150])
            return
        time.sleep(60)
    if time.time() - t0 > 60:
        status(f"waited {(time.time() - t0) / 60:.0f} min for indexing / another build to finish")

    def on_status(j, st):
        status("green signal - internet work done, the rest is offline" if j is None else f"video {st}")

    studio.build_batch([job], log=lambda *a: print(*a, flush=True), on_proc=None, should_stop=lambda: False,
                       on_status=on_status)
    mins = (time.time() - t0) / 60
    res = f"{job.status.upper()}: {job.message}" + (f"\nvideo: {job.video}" if getattr(job, "video", "") else "")
    qa = os.path.join(job.out or "", "QA.txt")
    if job.out and os.path.isfile(qa):
        res += "\n\nQA:\n" + open(qa, encoding="utf-8", errors="replace").read()[:3000]
    if job.status != "done":
        res += "\n\nlast lines:\n" + "\n".join((getattr(job, "tail", None) or [])[-25:])
    status(f"END ({mins:.0f} min) {job.status}: {job.message}")
    with open(DESK, "w", encoding="utf-8") as f:
        f.write(f"The Wire test video - {TOPIC}\nfinished {time.strftime('%Y-%m-%d %H:%M')} after {mins:.0f} min\n\n"
                f"{res}\n\nfull log: D:\\Lib\\tools\\wire_test.log\nbuild folder: {job.out}\n")


if __name__ == "__main__":
    main()
