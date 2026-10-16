"""Start indexing again (libjobs config workers=16) once the Movie Editor batch is idle: no editv2 build process for
20 minutes in a row. Never next to a render. Log: tools\resume_indexing.log"""
import json, os, time
import psutil

LOG = r"D:\Lib\tools\resume_indexing.log"


def log(m):
    open(LOG, "a", encoding="utf-8").write(time.strftime("%Y-%m-%d %H:%M:%S ") + m + "\n")


def building():
    return any("editv2" in " ".join(p.info["cmdline"] or []) and "build" in (p.info["cmdline"] or [])
               for p in psutil.process_iter(["cmdline"]))


log("waiting for the Movie Editor batch to finish")
idle = 0
while idle < 20:
    time.sleep(60)
    idle = 0 if building() else idle + 1
json.dump({"workers": 16}, open(r"D:\Lib\libjobs\config.json", "w"))
log("batch idle 20 min - indexing started (config workers=16)")
