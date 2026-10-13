"""Undo refine for a show (or one video): put the original catalogue (.catalog.json.v1.bak) back.
usage: py restore_catalogs.py "<show folder or video path>" [--dry]
The refined file is kept as .catalog.json.v2.bak so nothing is ever lost."""
import glob, os, shutil, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

target, dry = sys.argv[1], "--dry" in sys.argv
baks = [target + ".catalog.json.v1.bak"] if os.path.isfile(target) else \
    glob.glob(os.path.join(target, "**", "*.catalog.json.v1.bak"), recursive=True)
n = 0
for b in baks:
    cat = b[: -len(".v1.bak")]
    if not os.path.exists(C.lp(b)):
        continue
    print(("would restore " if dry else "restored ") + os.path.basename(cat))
    if not dry:
        if os.path.exists(C.lp(cat)):
            shutil.copy(C.lp(cat), C.lp(cat + ".v2.bak"))
        shutil.copy(C.lp(b), C.lp(cat))
        try:
            os.remove(os.path.join(C.DONE, C.key(os.path.splitext(cat[: -len(".catalog.json")])[0])))
        except OSError:
            pass
    n += 1
print(f"{n} catalogue(s) {'would be ' if dry else ''}restored")
