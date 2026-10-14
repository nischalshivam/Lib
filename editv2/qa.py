"""Automatic QA so nobody has to watch every video of a batch:
loudness, black frames, frozen frames, sub-0.25 s flash cuts, clue grounding,
verify rejects, burned-text fixes -> qa_report.json + QA.txt with PASS / WARN."""
from __future__ import annotations

import json, os, re, subprocess

from .mix import lufs


def _ff(args):
    return subprocess.run(["ffmpeg", "-hide_banner", "-nostats"] + args + ["-f", "null", "-"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace").stderr


def run(ctx, video, log=print):
    B = ctx["build"]
    R, warn = {}, []
    li, pk = lufs(video)
    R["lufs"], R["peak"] = li, pk
    if li is None or abs(li + 14) > 1.0:
        warn.append(f"loudness {li} LUFS (target -14)")
    if pk is not None and pk > -1.0:
        warn.append(f"true peak {pk} dBFS")
    err = _ff(["-i", video, "-vf", "scale=480:-2,blackdetect=d=0.4:pix_th=0.06,freezedetect=n=0.002:d=1.5,"
               "select='gt(scene,0.25)',showinfo", "-an"])
    blacks = [(float(a), float(b)) for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", err)]
    freezes = [float(x) for x in re.findall(r"freeze_duration: ([\d.]+)", err)]
    # a held title card ('BREATH' text, 'CUTOUT' intro) is meant to stand still - not a frozen picture
    # (The Wire test 2026-10-14: the 2.6-s 'ACCESS, NOT TERRITORY' card was the only 'frozen picture')
    try:
        _cards = [(it["o0"], it["o1"]) for it in json.load(open(os.path.join(B, "state.json"), encoding="utf-8"))["items"]
                  if it["kind"] in ("BREATH", "CUTOUT")]
    except Exception:
        _cards = []
    _fst = [float(x) for x in re.findall(r"freeze_start: ([\d.]+)", err)]
    freezes = [d for s, d in zip(_fst, freezes) if not any(a - 0.4 <= s and s + d <= b + 0.4 for a, b in _cards)]
    cuts = [float(x) for x in re.findall(r"pts_time:([\d.]+)", err)]
    html = open(os.path.join(B, "comp", "index.html"), encoding="utf-8").read()
    ann = [float(x) for x in re.findall(r'tl\.fromTo\("#af\d+".*?,([0-9.]+)\);', html)]   # intended white flash

    def small(t):
        from PIL import Image
        import io
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0, t):.3f}", "-i", video, "-frames:v", "1", "-vf", "scale=64:36,format=gray",
                            "-f", "rawvideo", "-"], capture_output=True)
        return r.stdout

    def real_flash(a, b):
        if any(abs(a - x) < 0.4 or abs(b - x) < 0.4 for x in ann):
            return False
        x, y = small(a - 0.08), small(b + 0.08)
        if len(x) != len(y) or not x:
            return True
        diff = sum(abs(i - j) for i, j in zip(x, y)) / len(x)
        return diff > 14                     # same picture either side -> something passed the lens, not a cut
    def busy(t):                             # >3 "cuts" within 1.5 s = camera/subject motion, not editing
        return sum(1 for c in cuts if abs(c - t) <= 0.75) > 3
    flashes = [round(b, 2) for a, b in zip(cuts, cuts[1:]) if b - a < 0.25 and not busy(b) and real_flash(a, b)]
    R.update(black=blacks, freezes=freezes, cuts=len(cuts), flash_cuts=flashes)
    blacks = [x for x in blacks if x[0] > 0.5]                 # the fade-in is fine
    if blacks:
        warn.append(f"{len(blacks)} black gaps: {blacks[:4]}")
    if [f for f in freezes if f > 2.5]:
        warn.append(f"frozen picture {[f for f in freezes if f > 2.5][:4]} s")
    if len(flashes) > 3:
        warn.append(f"{len(flashes)} flash cuts (<0.25 s) e.g. at {flashes[:5]}")
    # repeated footage: the same moving picture shown twice (frame hashes at 4 fps, constant offset >= 1 s)
    import numpy as np
    st0 = json.load(open(os.path.join(B, "state.json"), encoding="utf-8"))
    def item_at(t):
        return next((k for k, it in enumerate(st0["items"]) if it["o0"] <= t < it["o1"]), -1)
    Wd, Hd = 17, 16
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", video, "-vf", f"fps=4,scale={Wd}:{Hd},format=gray", "-f", "rawvideo", "-"],
                         capture_output=True).stdout
    fr = np.frombuffer(raw, np.uint8)[: (len(raw) // (Wd * Hd)) * Wd * Hd].reshape(-1, Hd, Wd).astype(np.int16)
    hs = (fr[:, :, 1:] > fr[:, :, :-1]).reshape(len(fr), -1)
    var = fr.reshape(len(fr), -1).std(1)
    hits = []
    for i in range(len(hs)):
        if var[i] < 12 or i + 8 >= len(hs) or (hs[i] == hs[i + 2]).all():     # flat or frozen picture
            continue
        dd = (hs[i + 8:] != hs[i]).sum(1)
        j = np.where(dd <= 6)[0]
        for jj in j[:3]:
            mid = (hs[i + 1:i + 8 + jj] != hs[i]).sum(1)
            if len(mid) and mid.max() <= 14:      # never changed in between = one slow shot, not footage shown twice
                continue                          # (The Wire 2026-10-14: 4 'repeats' were still faces 2.0 s apart)
            a_, b_ = i / 4, (i + 8 + jj) / 4
            ka, kb_ = item_at(a_), item_at(b_)
            if not (ka == kb_ and st0["items"][ka]["kind"] in ("STILL", "SU")) and not (hs[i + 8 + jj] == hs[min(len(hs) - 1, i + 10 + jj)]).all():
                hits.append((a_, b_ - a_))
                break
    runs = []
    for a_, off in hits:
        if runs and a_ - runs[-1][1] <= 0.5 and abs(off - runs[-1][2]) < 0.6:
            runs[-1][1] = a_
        else:
            runs.append([a_, a_, off])
    repeats = [(round(x, 1), round(x + off, 1)) for x, y, off in runs if y - x >= 1.0]
    R["repeats"] = repeats
    if repeats:
        warn.append(f"{len(repeats)} repeated footage stretches (first -> again): {repeats[:5]}")
    R["still_share"] = round(sum(it["o1"] - it["o0"] for it in st0["items"] if it["kind"] == "STILL") / st0["total"], 3)
    R["punch_ins"] = [round(it["o0"], 1) for it in st0["items"] if it["kind"] == "SU"]
    gaps = [b - a for a, b in zip([0] + R["punch_ins"], R["punch_ins"] + [st0["total"]])]
    if R["punch_ins"] and max(gaps) > 200:
        warn.append(f"no original-voice punch-in for {max(gaps):.0f} s somewhere")
    clue = json.load(open(os.path.join(B, "clue_v2.json"), encoding="utf-8"))
    R["clue"] = clue.get("stats", {})
    pl = json.load(open(os.path.join(B, "plan.json"), encoding="utf-8"))
    V = pl.get("verify", {})
    rej = sum(1 for v in V.values() if v.get("score", 0) < 5 or not v.get("pick"))
    R["verify"] = dict(shots=len(V), rejected=rej)
    if V and rej / len(V) > 0.25:
        warn.append(f"verify rejected {rej}/{len(V)} shots — library or clue weak for this script")
    st = json.load(open(os.path.join(B, "state.json"), encoding="utf-8"))
    R["text_fixed"] = st.get("text_fixed", 0)
    # cross-show: a line naming only one show's characters must not sit on another show's footage (MITM vs Young
    # Sheldon showed Sheldon while the narration was about Malcolm). plan.py prevents it; this catches what slips.
    R["cross_show"] = []
    if len(ctx.get("shows", [])) > 1:
        try:
            from .plan import Plan
            P = Plan(ctx)
            for it in st["items"]:
                sid = it.get("sid") or (it.get("su") or {}).get("sid")
                line = it.get("line", (it.get("su") or {}).get("line"))
                if sid in P.SH and line is not None:
                    own = P.line_owner(line)
                    if own and P.SH[sid]["ep"][0] != own:
                        R["cross_show"].append(round(it["o0"], 1))
        except Exception as e:                                     # noqa: BLE001
            log(f"  QA cross-show check skipped: {type(e).__name__}: {e}")
        if R["cross_show"]:
            warn.append(f"{len(R['cross_show'])} shots from another show under a line about one show, at {R['cross_show'][:6]}")
    R["look"] = json.load(open(os.path.join(B, "look.json"), encoding="utf-8"))
    R["warnings"], R["result"] = warn, "WARN" if warn else "PASS"
    json.dump(R, open(os.path.join(B, "qa_report.json"), "w", encoding="utf-8"), indent=1, default=str)
    lines = [f"QA {R['result']} — {os.path.basename(video)}",
             f"loudness {li} LUFS, peak {pk} dBFS; {len(cuts)} cuts, {len(flashes)} flash, look {R['look'].get('look')}",
             f"clue: {R['clue']}", f"verify: {R['verify']}, burned text fixed in {R['text_fixed']} clips",
             f"pictures: {100 * R['still_share']:.0f}% stills; punch-ins at {R['punch_ins']}; repeats {len(R['repeats'])}"] + \
            [f"WARN: {w}" for w in warn]
    open(os.path.join(B, "QA.txt"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    for l in lines:
        log("  " + l)
    return R
