"""Render a long comp in time parts (comp/_part_NN.html), then join them without re-encoding.

ROOT CAUSE 2026-10-15 (The Wire, Season 5 + Stringer & Bunny failed every render): a HyperFrames page holding 260-313
<video> clips hangs Chrome a few hundred frames into a worker (Runtime.callFunctionOn / Page.captureScreenshot timed
out) and makes every frame slower. A/B on the same comp: 313 clips -> worker 0 hung at frame 172-317 in 9 of 9 runs;
the same comp with 13 clips -> 2055 frames in 2.5 min, ~3x the speed. preload="none", the decode wait, kinetic text and
the browser pool were all ruled out. A part holds only its own ~5 min of clips; a finished part survives a failed
retry of the next one.

A part starts on a frame boundary (F0 / fps) and every time inside it is shifted by exactly that, so part frame j is
seeked at the same global time as frame F0 + j of the whole comp: the joined video is frame-identical. A cut is only
placed where no clip or tween runs across it (a clip starting less than one frame before the cut is snapped onto it,
a static clip like the letterbox is clipped). Anything unexpected in the comp -> None -> the caller renders it whole."""
from __future__ import annotations

import math, os, re

PART_SECONDS = float(os.environ.get("EDITV2_PART_SECONDS", "300"))
MIN_CLIPS = int(os.environ.get("EDITV2_PART_MIN_CLIPS", "100"))    # fewer <video> clips render fine in one page
SEARCH_S = 90                                                       # look this far around each target for a clean cut
EPS = 1e-6

ROOT_RE = re.compile(r'^(<body><div id="root" data-composition-id="main" data-start="0" data-duration=")([0-9.]+)(".*>)$')
PAIR_RE = re.compile(r'data-start="([0-9.]+)" data-duration="([0-9.]+)"')
TWEEN_RE = re.compile(r'^tl\.(?:fromTo|to)\("#([\w-]+)[^"]*",.*,(-?[0-9.]+)\);$')


def _parse(html):
    """-> (head, root line parts, clip lines, tween lines, tail) or None when the comp is not the shape look.py writes."""
    lines = html.split("\n")
    try:
        r = next(i for i, l in enumerate(lines) if l.startswith('<body><div id="root"'))
        c = lines.index("</div><script>", r)
        t0 = lines.index("const tl = gsap.timeline({ paused: true });", c)
        t1 = lines.index('window.__timelines["main"] = tl;', t0)
    except (StopIteration, ValueError):
        return None
    m = ROOT_RE.match(lines[r])
    if not m:
        return None
    return lines[:r], m, lines[r + 1:c], lines[t0 + 1:t1], lines[c:t0 + 1], lines[t1:]


class Clip:
    def __init__(self, line):
        self.line = line
        pairs = [(float(a), float(b)) for a, b in PAIR_RE.findall(line)]
        self.a = min(a for a, _ in pairs)
        self.b = max(a + d for a, d in pairs)
        self.ids = re.findall(r'id="([^"]+)"', line)
        self.video = "<video" in line
        self.tweens = []

    def static(self):                       # nothing moves or plays: it can be clipped at a cut
        return not self.video and not self.tweens


def plan(html, fps, part_s=PART_SECONDS, min_clips=MIN_CLIPS):
    """-> (parsed, clips, [(f0, f1), ...]) or None (render the comp whole)."""
    p = _parse(html)
    if not p:
        return None
    _, root, clip_lines, tween_lines, _, _ = p
    if sum(l.count("<video") for l in clip_lines) < min_clips:
        return None
    clips, by_id = [], {}
    for l in clip_lines:
        if not l.strip():
            continue
        opens = len(re.findall(r"<(div|span|svg|g|p|text)\b", l))
        if opens != len(re.findall(r"</(div|span|svg|g|p|text)>", l)) or not PAIR_RE.search(l):
            return None                     # an element over several lines / an untimed one: cannot cut safely
        c = Clip(l)
        clips.append(c)
        for i in c.ids:
            by_id[i] = c
    for l in tween_lines:
        m = TWEEN_RE.match(l.strip())
        if not m or m.group(1) not in by_id:
            return None
        pos = float(m.group(2))
        durs = re.findall(r"duration:([0-9.]+)", l)
        st = re.search(r"stagger:([0-9.]+)", l)
        end = pos + (float(durs[-1]) if durs else 0) + (float(st.group(1)) * 8 if st else 0)
        by_id[m.group(1)].tweens.append((pos, end))
    total = float(root.group(2))
    nframes = int(round(total * fps)) if abs(total * fps - round(total * fps)) <= 1e-6 else math.ceil(total * fps)
    n = math.ceil(total / part_s)
    if n < 2:
        return None
    moving = [(min([c.a] + [t[0] for t in c.tweens]), max([c.b] + [t[1] for t in c.tweens])) for c in clips if not c.static()]

    def clean(f):
        T = f / fps
        return not any(a < T - 1 / fps + EPS and b > T + EPS for a, b in moving)
    cuts = []
    for k in range(1, n):
        target = round(k * nframes / n)
        best = next((f for d in range(0, int(SEARCH_S * fps)) for f in (target - d, target + d)
                     if (not cuts or f > cuts[-1] + fps * 30) and f < nframes - fps * 30 and clean(f)), None)
        if best is None:
            return None
        cuts.append(best)
    bounds = list(zip([0] + cuts, cuts + [nframes]))
    return p, clips, bounds


def _shift(clip, T0, T1, fps):
    """the clip line moved to a part starting at T0 (and ending at T1)."""
    def one(m):
        a, d = float(m.group(1)), float(m.group(2))
        a2 = a - T0
        if a2 < 0:
            if clip.static():
                d, a2 = d + a2, 0.0                         # letterbox & co: clipped
            elif a2 > -1 / fps - EPS:
                a2 = 0.0                                    # starts inside the frame before the cut: snapped
            else:
                raise ValueError(f"clip crosses the cut at {T0:.3f}: {clip.line[:120]}")
        return f'data-start="{a2:.6f}" data-duration="{min(d, T1 - T0 - a2 + 1):.6f}"'
    return PAIR_RE.sub(one, clip.line)


def write(comp_dir, fps, log=print, part_s=PART_SECONDS):
    """write comp/_part_NN.html for a comp that needs parts -> [(file name, f0, f1)], or None (render whole)."""
    html = open(os.path.join(comp_dir, "index.html"), encoding="utf-8").read()
    pl = plan(html, fps, part_s)
    if not pl:
        return None
    (head, root, _, tween_lines, mid, tail), clips, bounds = pl
    out = []
    for k, (f0, f1) in enumerate(bounds):
        T0, T1 = f0 / fps, f1 / fps
        keep = [c for c in clips if c.a < T1 - EPS and c.b > T0 + EPS]       # seen on at least one part frame
        ids = {i for c in keep for i in c.ids}
        try:
            body = [_shift(c, T0, T1, fps) for c in keep]
        except ValueError as e:
            log(f"  render parts: {e} - rendering the comp whole")
            return None
        tw = []
        for l in tween_lines:
            s = l.strip()
            m = TWEEN_RE.match(s)
            if m.group(1) not in ids:
                continue
            pos = float(m.group(2)) - T0
            if pos < -1 / fps - EPS:
                log(f"  render parts: a tween starts {-pos:.2f}s before the cut at {T0:.3f} - rendering the comp whole")
                return None
            tw.append(s[:m.start(2)] + f"{max(0.0, pos):.6f}" + s[m.end(2):])
        dur = (f1 - f0) / fps
        doc = "\n".join(head + [root.group(1) + f"{dur:.6f}" + root.group(3)] + body + mid + tw + tail)
        name = f"_part_{k:02d}.html"
        open(os.path.join(comp_dir, name), "w", encoding="utf-8").write(doc)
        out.append((name, f0, f1))
    log(f"  render parts: {len(out)} parts at " + ", ".join(f"{f0 / fps / 60:.1f}" for _, f0, _ in out[1:]) + " min "
        f"({sum(c.video for c in clips)} clips in the comp)")
    return out


if __name__ == "__main__":                    # py -m editv2.parts <comp dir>  -> writes the part files, prints the plan
    import sys
    from .common import FPS
    for name, f0, f1 in write(sys.argv[1], FPS) or []:
        h = open(os.path.join(sys.argv[1], name), encoding="utf-8").read()
        print(name, f0, f1, f"{(f1 - f0) / FPS:.1f}s", h.count("<video"), "clips", h.count("tl.from") + h.count("tl.to("), "tweens")
