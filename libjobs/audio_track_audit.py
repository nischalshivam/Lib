"""Which finished catalogues were built from the wrong audio track (old rule: first English track; new rule:
pick_audio = most channels, never commentary). --redo re-queues them (done marker removed, refined.version set to 4)."""
import json, os, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from media_index import catalog_refine as R

redo = "--redo" in sys.argv
q = json.load(open(C.QUEUE, encoding="utf-8"))
bad = []
for v in q:
    if not C.is_done(v):
        continue
    cat = os.path.splitext(v)[0] + ".catalog.json"
    try:
        d = json.load(open(C.lp(cat), encoding="utf-8"))
        meta = R.probe(v)
    except Exception as e:
        print("skip", os.path.basename(v), e)
        continue
    used, want = d.get("audio_track"), R.pick_audio(meta)
    if len(meta["audio"]) > 1 and used != want:
        bad.append(v)
        print(f"WRONG TRACK used {used} -> should be {want}  {meta['audio']}  {os.path.basename(v)}")
        if redo:
            d["refined"]["version"] = 4
            tmp = cat + ".tmp"
            json.dump(d, open(C.lp(tmp), "w", encoding="utf-8"), ensure_ascii=False)
            os.replace(C.lp(tmp), C.lp(cat))
            try:
                os.remove(os.path.join(C.DONE, C.key(v)))
            except OSError:
                pass
print(f"{len(bad)} finished catalogue(s) used the wrong audio track" + (" - re-queued" if redo else ""))
