"""Hold some of the indexing GPU slots so fewer whisper passes run at once - without restarting any worker.

2026-10-14 04:40: The Middle has no subtitles, so every episode runs the LARGE whisper model (beam 5); 4 of them plus
16 workers' CUDA contexts overflowed the 12 GB VRAM -> WDDM paged to system RAM: GPU '100 %' at 58 W, a 22-min
episode took 20+ min of whisper and 12 workers queued 40 min for a slot. catalog_refine.gpu_slot() takes the first
free slot<i>.lock (OS byte lock), so holding slot2 + slot3 here leaves 2 for the workers.

Exits by itself (locks are released when the process ends) when indexing is stopped (config workers=0), when the
file tools\\gpu_hold.stop exists, or after --hours.
    pythonw tools\\gpu_hold.py --slots 2 3 --hours 14
"""
import argparse, json, msvcrt, os, time

GPU = r"D:\Lib\libjobs\gpu"
CONFIG = r"D:\Lib\libjobs\config.json"
STOP = r"D:\Lib\tools\gpu_hold.stop"
LOG = r"D:\Lib\tools\gpu_hold.log"


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def workers_wanted():
    try:
        return int(json.load(open(CONFIG, encoding="utf-8")).get("workers", 1))
    except Exception:
        return 1                                   # unreadable for a moment: keep holding


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slots", type=int, nargs="+", default=[2, 3])
    ap.add_argument("--hours", type=float, default=14)
    a = ap.parse_args()
    end = time.time() + a.hours * 3600
    held = {}
    log(f"start pid {os.getpid()}: holding slots {a.slots}")
    while time.time() < end and not os.path.exists(STOP) and workers_wanted() > 0:
        for i in a.slots:
            if i in held:
                continue
            f = open(os.path.join(GPU, f"slot{i}.lock"), "a+")
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                held[i] = f
                log(f"holding slot{i}")
            except OSError:
                f.close()
        time.sleep(0.1 if len(held) < len(a.slots) else 10)
    log(f"exit (stop file {os.path.exists(STOP)}, workers {workers_wanted()}) - releasing {sorted(held)}")


if __name__ == "__main__":
    main()
