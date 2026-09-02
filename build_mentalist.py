"""Build the Mentalist clip library — ONLY the 29 episodes the 10 Red-John
scripts actually need (S1-S6, no S7). Each episode is Gemini-tagged into its own
<video>.catalog.json using that SEASON's cast subset (cast_sN.txt) so the
reference images sent on every shot — and thus the cost — stay small.

Sequential on purpose: the source videos live on the USB SSD, which drops reads
under parallel load. Resumable: a finished episode stamps "complete" and is
skipped in milliseconds on a restart, so this can be stopped and rerun safely.

    python build_mentalist.py            # build all remaining
    python build_mentalist.py S6E01      # build ONE episode, then inspect
    python build_mentalist.py --minutes 12   # cheap partial pass on all
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
import sys
import time

BASE = r"F:\Movies\The Mentalist"
CAST = os.path.join(BASE, "Cast")
CHARS = os.path.join(BASE, "characters.txt")
SHARED = os.path.dirname(os.path.abspath(__file__))
VIDEXT = (".mkv", ".mp4", ".avi", ".m4v", ".ts")

# the 29 episode-slots the scripts reference, per season
NEED = {
    1: [1, 2, 7, 10, 11, 23],
    2: [8, 23],
    3: [3, 10, 16, 23, 24],
    4: [1, 2, 7, 11, 24],
    5: [8, 13, 22],
    6: [1, 2, 3, 4, 5, 6, 7, 8],
}


def season_videos(season: int) -> list:
    folder = os.path.join(BASE, f"The Mentalist Season {season}")
    return [f for f in glob.glob(folder + "/*") if f.lower().endswith(VIDEXT)]


def find_video(season: int, ep: int, vids: list) -> str:
    """Match SxxEyy in a filename. The S3 finale is one combined file named
    ...S03E23E24..., so E23 and E24 both resolve to it."""
    pat = re.compile(rf"S0?{season}E0?{ep:02d}(?:E|\b)", re.I)
    for v in vids:
        if pat.search(os.path.basename(v)):
            return v
    return ""


def is_complete(cat_json: str) -> bool:
    try:
        import json
        with open(cat_json, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(d, dict):
        return False
    if d.get("complete") is True:
        return True
    shots = d.get("shots") or []
    if not shots:
        return False
    described = sum(1 for s in shots if s.get("description"))
    total = d.get("windows")
    if isinstance(total, int):
        return len(shots) >= total and described == len(shots)
    return described == len(shots)


def build_jobs():
    """One job per UNIQUE video file (the combined finale is not built twice)."""
    jobs, seen = [], set()
    for season, eps in NEED.items():
        vids = season_videos(season)
        for ep in eps:
            v = find_video(season, ep, vids)
            tag = f"S{season}E{ep:02d}"
            if not v:
                jobs.append((season, tag, "", None)); continue
            if v in seen:
                continue
            seen.add(v)
            jobs.append((season, tag, v, os.path.splitext(v)[0] + ".catalog.json"))
    return jobs


def wait_for_video(video, tries=20, gap=30):
    """The source lives on a USB SSD that occasionally drops off the bus under
    load; when it does, every file read fails with 'video nahi mila' and the
    whole queue used to die. Instead, wait for the drive to come back."""
    for i in range(tries):
        if os.path.isfile(video):
            try:
                with open(video, "rb") as f:
                    f.read(1)
                return True
            except OSError:
                pass
        print(f"  [SSD?] {os.path.basename(video)} not readable — "
              f"wait {gap}s ({i+1}/{tries})", flush=True)
        time.sleep(gap)
    return False


def run_one(season, tag, video, minutes, retries=3):
    ref = os.path.join(BASE, f"cast_s{season}.txt")
    cmd = [sys.executable, "-m", "media_index", "catalog", video,
           "--cast", CAST, "--characters", CHARS, "--ref-names", ref]
    if minutes and int(minutes) > 0:
        cmd += ["--minutes", str(minutes)]
    print(f"\n{'='*66}\n  {tag}  ·  season-cast subset  ·  {os.path.basename(video)}\n{'='*66}", flush=True)
    for attempt in range(1, retries + 1):
        if not wait_for_video(video):
            print(f"  ! {tag}: SSD gaayab — chhod ke aage badh raha", flush=True)
            return False
        r = subprocess.run(cmd, cwd=SHARED)
        if r.returncode == 0:
            return True
        # catalog.run resumes from its own catalog.json, so a retry continues
        # where a drop cut it off rather than starting over.
        print(f"  ! {tag} attempt {attempt}/{retries} non-zero — "
              f"{'retry after 30s' if attempt < retries else 'giving up'}", flush=True)
        if attempt < retries:
            time.sleep(30)
    return False


def main():
    args = [a for a in sys.argv[1:]]
    minutes = 0
    if "--minutes" in args:
        i = args.index("--minutes"); minutes = args[i + 1]; del args[i:i + 2]
    only = args[0].upper() if args else ""

    jobs = build_jobs()
    total = len([j for j in jobs if j[2]])
    missing = [j[1] for j in jobs if not j[2]]
    if missing:
        print(f"  ⚠ video not found for: {missing}")

    todo = []
    for season, tag, video, cat in jobs:
        if not video:
            continue
        if only and only not in tag.upper():
            continue
        if not minutes and cat and is_complete(cat):
            print(f"  ✓ {tag} already complete — skip")
            continue
        todo.append((season, tag, video))

    print(f"\n  Mentalist build: {len(todo)} episode(s) to catalogue "
          f"(of {total} needed){' — MINUTES=' + str(minutes) if minutes else ''}\n")
    ok = 0
    t0 = time.time()
    for n, (season, tag, video) in enumerate(todo, 1):
        print(f"\n########## {n}/{len(todo)}  ({tag})  "
              f"elapsed {int((time.time()-t0)//60)}m ##########", flush=True)
        if run_one(season, tag, video, minutes):
            ok += 1
        else:
            print(f"  ! {tag} returned non-zero — continuing", flush=True)
    print(f"\n{'='*66}\n  DONE: {ok}/{len(todo)} catalogued in "
          f"{int((time.time()-t0)//60)}m\n{'='*66}")


if __name__ == "__main__":
    main()
