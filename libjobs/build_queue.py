"""scan_all.json -> queue.json (priority order). Every indexed or indexable video of the library, except the
duplicate Two and a Half Men copy in Movies DATA 3 and the corrupt Lektor copy of The Boys.
P0 the 4 channel shows (House, B99, Reacher, TAHM) - P1 Other Movies - P2 the main show list + Andor/Supernatural
- P3 everything else - P4 cartoons."""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
SKIP = ["Movies DATA 3\\Two and a Half Men", "[ENG-Lektor PL]", "Cartoons DATA",   # cartoons: later
        "\\Vikings\\",                                                              # user 2026-10-10: not now
        "\\Supernatural",                                                           # user 2026-10-12: later (304 eps)
        "\\Movies\\Cartoon\\", "SSD Data"]                                          # SSD 2 cartoons later; personal data never
P0 = ["Jack Reacher", "Movies DATA 3\\House", "Movies DATA 3\\Brooklyn Nine-Nine", "Movies DATA\\Reacher", "Movies DATA\\Two and a Half Men"]
P1 = ["Other Movies"]
P2 = ["Malcolm in the Middle", "Sopranos", "Game of thrones", "Vikings", "Mad Men", "How I Met Your Mother", "The Wire",
      "Harry Potter", "Monk", "The Boys", "True Detective", "The Mentalist", "Breaking Bad", "Better call saul",
      "Young Sheldon", "The Big Bang Theory", "Modern Family", "The Walking Dead", "Fear the Walking Dead", "Andor",
      "Star Wars", "Supernatural"]


def prio(top):
    if any(k in top for k in P0):
        return 0, next(i for i, k in enumerate(P0) if k in top)
    if any(k in top for k in P1):
        return 1, 0
    if any(k.lower() in top.lower() for k in P2):
        return 2, next(i for i, k in enumerate(P2) if k.lower() in top.lower())
    if "Cartoons DATA" in top:
        return 4, 0
    return 3, 0


scan = json.load(open(os.path.join(HERE, "scan_all.json"), encoding="utf-8"))
items = []
for top, files in scan.items():
    if any(s in top for s in SKIP):
        continue
    for f in files:
        if not any(s in f["p"] for s in SKIP):
            items.append((prio(top), f["p"]))
try:                                  # user 2026-10-12: nothing from the new SSD 2 (F:\Movies) until they say so
    import volumes as _vol
except ImportError:
    from libjobs import volumes as _vol
items = [it for it in items if _vol.volume_of(it[1]) != "SSD 2"]
# user 2026-10-13: "finish only the priority shows, then stop" - only.json = list of path fragments to KEEP (all if absent)
ONLY = os.path.join(HERE, "only.json")
if os.path.exists(ONLY):
    keep = [k.lower() for k in json.load(open(ONLY, encoding="utf-8"))]
    items = [it for it in items if any(k in it[1].lower() for k in keep)]
items.sort()
# 2026-10-16 (user): new footage on SSD 2 is indexed in place before its Drive upload - extra_roots.json lists those
# folders. They are interleaved 1:1 with the rest (local SSD reads + network Drive reads keep both resources busy).
EXTRA = os.path.join(HERE, "extra_roots.json")
extra = []
if os.path.exists(EXTRA):
    for top in json.load(open(EXTRA, encoding="utf-8")).get("folders", []):
        for dp, dn, fn in os.walk(top):
            dn[:] = [d for d in dn if d.lower() != "cast" and "featurette" not in d.lower() and "extras" not in d.lower()]
            for f in sorted(fn):
                if f.lower().endswith((".mkv", ".mp4", ".avi", ".m4v")) and "sample" not in f.lower() and "trailer" not in f.lower():
                    extra.append(os.path.join(dp, f))
    extra.sort()
if extra:
    base = [p for _, p in items]
    mixed = []
    for i in range(max(len(base), len(extra))):
        if i < len(extra):
            mixed.append(extra[i])
        if i < len(base):
            mixed.append(base[i])
    items = [(None, p) for p in mixed]
json.dump([p for _, p in items], open(os.path.join(HERE, "queue.json.tmp"), "w", encoding="utf-8"), ensure_ascii=False)
os.replace(os.path.join(HERE, "queue.json.tmp"), os.path.join(HERE, "queue.json"))
print(f"queue: {len(items)} videos")
