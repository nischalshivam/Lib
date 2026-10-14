"""Pipeline test (2026-10-14): 3 The Wire videos through studio.build_batch exactly like the Movie Editor - 3 internet
phases at once, each video's render starting as soon as its internet phase is done. Also samples CPU / RAM / GPU /
network every 30 s into tools\\wire_batch_res.csv to find the real bottleneck.
    py tools\\wire_batch_test.py"""
import os, subprocess, sys, threading, time
sys.path.insert(0, r"D:\Lib")
import psutil
import studio

ROOT = r"D:\Movies\English\The Wire"
TOPICS = ["Avon correctly reading people, power, and street incentives that Stringer misses",
          "Slim Charles repeatedly giving the strategically correct advice",
          "Lester Freamon's best investigative deductions and breakthroughs"]
VOICE = r"D:\Imp Files\Audio Samples FINAL\Sheldon Cooper Audio.mp3"
HERE = os.path.dirname(os.path.abspath(__file__))
STOP = threading.Event()


def sampler():
    p = os.path.join(HERE, "wire_batch_res.csv")
    with open(p, "w", encoding="utf-8") as f:
        f.write("time,cpu,ram_used_gb,gpu_util,vram_mb,net_mbps,ffmpeg,chrome,python\n")
    last = psutil.net_io_counters().bytes_recv, time.time()
    while not STOP.is_set():
        cpu = psutil.cpu_percent(interval=5)
        g = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True).stdout.strip().replace(" ", "")
        b, t = psutil.net_io_counters().bytes_recv, time.time()
        mbps = (b - last[0]) * 8 / 1e6 / max(1e-3, t - last[1])
        last = b, t
        names = [x.info["name"] or "" for x in psutil.process_iter(["name"])]
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')},{cpu},{psutil.virtual_memory().used / 1e9:.1f},{g},{mbps:.1f},"
                    f"{names.count('ffmpeg.exe')},{names.count('chrome.exe')},{names.count('python.exe')}\n")
        STOP.wait(25)


def main():
    jobs = []
    for i, t in enumerate(TOPICS):
        jobs.append(studio.Job(clean=os.path.join(ROOT, t, t + ".txt"), clue="", audio="", save_dir=os.path.join(ROOT, "Final Videos"),
                               fmt="auto", resolution="1080p", language="en", text=False, verify=True, verify_intro_min=0,
                               cold_open=True, intro_punch=True, ken_burns=True, frame=True, kinetic_text=True, text_file="",
                               auto_voice=True, voice_ref=VOICE, index=i, engine="v2", look="auto"))
    threading.Thread(target=sampler, daemon=True).start()
    t0 = time.time()
    st = os.path.join(HERE, "wire_batch_status.txt")

    def on_status(j, s):
        line = f"{time.strftime('%H:%M:%S')} +{(time.time() - t0) / 60:.0f}m " + ("GREEN" if j is None else f"V{j.index + 1} {s}")
        print(line, flush=True)
        open(st, "a", encoding="utf-8").write(line + "\n")
    studio.build_batch(jobs, log=lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True), on_status=on_status)
    STOP.set()
    for j in jobs:
        on_status(j, f"FINAL {j.status}: {j.message}")
    open(st, "a", encoding="utf-8").write(f"{time.strftime('%H:%M:%S')} ALL DONE in {(time.time() - t0) / 60:.0f} min\n")


if __name__ == "__main__":
    main()
