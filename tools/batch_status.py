"""One-screen status of the Movie Editor batch (editv2/cache/last_batch.json): per video its GUI state, current
build stage, render attempts, QA verdict and any error lines; plus the PC (RAM / GPU) and the totals.
    py tools\\batch_status.py            table
    py tools\\batch_status.py --events   only problems (exit code 3 if any) - used by the watcher"""
import glob, json, os, re, subprocess, sys, time

B = r"D:\Lib\editv2\cache\last_batch.json"
BAD = re.compile(r"Traceback|FAILED|RuntimeError|Error:|attempt \d failed|render stalled|could not save|could not confirm"
                 r"|cannot start|none of the script's shows", re.I)


def lp(p):
    p = os.path.abspath(p)
    return "\\\\?\\" + p if len(p) >= 248 and not p.startswith("\\\\?\\") else p


def build_dir(clean):
    d = os.path.dirname(clean)
    return os.path.join(d, os.path.splitext(os.path.basename(clean))[0] + "_v2build")


def info(spec):
    bd = build_dir(spec["clean"])
    out = dict(stage="-", t="", qa="", errors=[], attempts=0)
    try:
        lines = open(lp(os.path.join(bd, "build.log")), encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return out
    for ln in lines:
        m = re.match(r"^\[(\d\d:\d\d:\d\d)\] (\w+)", ln)
        if m:
            out["t"], out["stage"] = m.group(1), m.group(2)
        if "QA PASS" in ln or "QA WARN" in ln or "QA FAIL" in ln:
            out["qa"] = "PASS" if "PASS" in ln else ("WARN" if "WARN" in ln else "FAIL")
        if "attempt" in ln and "failed" in ln:
            out["attempts"] += 1
        if BAD.search(ln):
            out["errors"].append(ln.strip()[:200])
    if lines and lines[-1].startswith("DONE "):
        out["stage"] = "DONE"
    return out


def main():
    b = json.load(open(B, encoding="utf-8"))
    specs, state = b.get("specs", []), {str(k): v for k, v in (b.get("state") or {}).items()}
    rows, n = [], {}
    for i, s in enumerate(specs):
        st = state.get(str(i), "pending")
        x = info(s)
        n[st] = n.get(st, 0) + 1
        rows.append((i, st, x, os.path.basename(os.path.dirname(s["clean"]))))
    if "--events" in sys.argv:
        ev = [(i, st, x, name) for i, st, x, name in rows if st == "failed" or x["errors"]]
        for i, st, x, name in ev:
            print(f"V{i + 1} {st} {name[:50]} | {x['errors'][-1] if x['errors'] else ''}")
        sys.exit(3 if ev else 0)
    print(time.strftime("%H:%M:%S"), "batch saved", b.get("saved", ""), "| states:", n)
    for i, st, x, name in rows:
        print(f"V{i + 1:<2} {st:<8} {x['stage']:<8} {x['t']:<8} QA {x['qa']:<4} {'attempts ' + str(x['attempts']) if x['attempts'] else '':<10}"
              f" {name[:58]}" + (f"\n      ! {x['errors'][-1]}" if x["errors"] else ""))
    try:
        import psutil
        vm = psutil.virtual_memory()
        g = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,temperature.gpu", "--format=csv,noheader"],
                           capture_output=True, text=True).stdout.strip()
        print(f"PC: RAM {vm.used / 1e9:.1f}/{vm.total / 1e9:.1f} GB, GPU {g}")
    except Exception:
        pass
    fv = r"D:\Movies\English\The Wire\Final Videos"
    vids = [f for f in os.listdir(fv) if f.endswith(".mp4")]
    print(f"Final Videos: {len(vids)} mp4")


if __name__ == "__main__":
    main()
