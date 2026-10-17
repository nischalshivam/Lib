"""3.8 (library catalogue) vs another model's refine (tools/_refine_ab/<tag>/) of the same episodes.
Prints stats + shot-level agreement and writes a BLIND judging set (frame + both captions, sides shuffled) to
tools/_refine_ab/judge/.   usage: py tools/compare_refine.py <tag> <video> [<video> ...]"""
import collections, hashlib, json, os, random, subprocess, sys

AB = r"D:\Lib\tools\_refine_ab"
tag, videos = sys.argv[1], sys.argv[2:]
J = os.path.join(AB, "judge")
os.makedirs(J, exist_ok=True)
items, key = [], {}
for v in videos:
    cat = os.path.splitext(v)[0] + ".catalog.json"
    A = json.load(open(cat, encoding="utf-8"))
    B = json.load(open(os.path.join(AB, tag, os.path.basename(cat)), encoding="utf-8"))
    ep = os.path.basename(v)[:60]
    print(f"\n== {ep}")
    for f in ("checked", "blocked", "unverified", "fixed", "no_names", "unsafe", "explicit", "text_zones", "allowed_cast",
              "cast_scope", "dialogue", "after", "total_s"):
        print(f"  {f:13} 3.8={A['refined'].get(f)!s:28} {tag}={B['refined'].get(f)!s}")
    bs = {round(s["start"], 1): s for s in B["shots"]}
    pairs = [(a, bs[round(a["start"], 1)]) for a in A["shots"] if round(a["start"], 1) in bs]
    print(f"  shots matched by start: {len(pairs)} of {len(A['shots'])} / {len(B['shots'])}")
    c = collections.Counter()
    for a, b in pairs:
        ca, cb = set(a.get("characters") or []), set(b.get("characters") or [])
        c["names same"] += ca == cb
        c["names only 3.8"] += bool(ca) and not cb
        c[f"names only {tag}"] += bool(cb) and not ca
        c["names differ (both)"] += bool(ca) and bool(cb) and ca != cb
        for f in ("safe", "text", "nsfw", "shot_type", "explicit", "logo"):
            c[f"{f} same"] += a.get(f) == b.get(f)
    for k_, n in c.items():
        print(f"  {k_:22} {n:5} ({100 * n // max(1, len(pairs))}%)")
    lit = [(a, b) for a, b in pairs if a.get("luma", 99) >= 25 and a["end"] - a["start"] >= 1.0
           and (a.get("description") or "").strip() != (b.get("description") or "").strip()]
    for a, b in random.Random(ep).sample(lit, min(20, len(lit))):
        iid = hashlib.md5(f"{ep}|{a['start']}".encode()).hexdigest()[:8]
        t = (a["start"] + a["end"]) / 2
        jpg = os.path.join(J, iid + ".jpg")
        if not os.path.exists(jpg):
            subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", v, "-frames:v", "1", "-vf", "scale=960:-2",
                            "-q:v", "3", jpg], capture_output=True)
        flip = int(iid, 16) % 2 == 1
        sa = {k: a.get(k) for k in ("description", "shot_type", "text", "nsfw")}
        sb = {k: b.get(k) for k in ("description", "shot_type", "text", "nsfw")}
        items.append({"id": iid, "image": jpg, "A": sb if flip else sa, "B": sa if flip else sb})
        key[iid] = {"ep": ep, "t": round(t, 2), "A": tag if flip else "3.8", "B": "3.8" if flip else tag,
                    "names_3.8": a.get("characters"), f"names_{tag}": b.get("characters")}
random.Random(7).shuffle(items)
for n, k0 in enumerate(range(0, len(items), 12), 1):
    json.dump({"items": items[k0:k0 + 12]}, open(os.path.join(J, f"batch_{n:02d}.json"), "w", encoding="utf-8"), indent=1)
json.dump(key, open(os.path.join(J, "key.json"), "w", encoding="utf-8"), indent=1)
print(f"\njudge set: {len(items)} frames in {(len(items) + 11) // 12} batches -> {J}")
