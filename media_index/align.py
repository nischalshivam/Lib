"""Place shots that have no dialogue, by walking the scene in order.

Dialogue matching solves the easy case. It does not solve the case that
matters most for a scene breakdown, because the best scenes are often the
quiet ones — measured on a real 71-beat script about the Breaking Bad box
cutter scene, **92% of shots had no dialogue at all**. The scene is famous
precisely because nobody speaks.

But that script has a property worth everything: within a stretch of beats
drawn from one episode, the beats follow the scene **in order**. Gus walks in,
takes off his jacket, rolls his sleeves, steps into the suit, ties the apron,
picks up the box cutter. That is the scene's own chronology, written down.

So the shots do not need to be searched for individually. They need to be
*laid along* the scene:

  1. find the few lines that DO match — they are anchors with exact times
  2. detect every shot boundary between the anchors
  3. walk the described moments and the real shots together, in order

One line of dialogue at each end of a scene is enough to place everything
between them. That is what turns 7% coverage into most of the script.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import cutter
from .probe import ProbeError, probe
from .search import find

# A run shorter than this is not worth aligning — individual search is fine.
MIN_RUN = 2
# The axis is built from `duration_target_sec`, which is how long the CLIP
# should be, not how long the moment lasts on screen — a four second clip is
# routinely taken from a twenty second beat. So the ratio between script time
# and film time is genuinely large, and these bounds are here only to catch
# an absurdity, never to overrule what two anchors actually measured.
MIN_SCALE = 0.05
MAX_SCALE = 25.0
# The most of one episode a single run may be spread across.
#
# Two anchors measure the stretch between them, which is right when both are
# right and worse than useless when one is not. On the real script a beat
# about the AUDIENCE — Bryan Cranston's daughter fainting at a screening —
# carried a quote that matched somewhere far from the scene, and the pair
# fitted to x2.14: 103 shots spread over 20:47-37:11 for a sequence that
# runs 33:00-37:15. The video opened on Hank and Marie at home.
#
# A run gathers every shot an essay takes from one episode, which can be a
# few scenes, but not a sixth of an hour. Past this the anchors are not
# describing the same stretch of film and only the strongest is kept.
MAX_RUN_SPAN_S = 600.0
# Two placements closer than this are the same moment; spread them apart.
MIN_SEPARATION_S = 1.5
# Snapping to a shot boundary only helps when one is actually nearby. Scene
# detection misses low-contrast cuts, and dragging a placement 15 s to reach
# the next surviving boundary is worse than trusting the interpolation.
MAX_SNAP_S = 8.0


@dataclass
class Entry:
    beat: int
    shot: int
    data: dict

    @property
    def query(self) -> str:
        return ((self.data.get("exact_dialogue") or "").strip()
                or (self.data.get("nearest_dialogue") or "").strip())

    @property
    def is_hook(self) -> bool:
        """Quoted before the moment it belongs to, to open the essay.

        The closing line of a scene placed at shot 1 told the tool the scene
        ends where it begins, and the whole sequence landed four minutes
        late. A hook still names a real moment, so it is worth cutting; it
        just says nothing about order.
        """
        return bool(self.data.get("hook"))

    @property
    def target_seconds(self) -> float:
        try:
            return float(self.data.get("duration_target_sec") or 4.0)
        except (TypeError, ValueError):
            return 4.0


@dataclass
class Run:
    source: str
    season_episode: str
    entries: list = field(default_factory=list)
    # Which sequence OF that episode. An essay visits the box cutter scene
    # and the cold open of the same hour, and those are two walks, not one.
    part: int = 0

    @property
    def label(self) -> str:
        base = f"{self.source} {self.season_episode}"
        return f"{base} (scene {self.part + 1})" if self.part else base


@dataclass
class Placement:
    beat: int
    shot: int
    path: str = ""
    start_ms: int = 0
    end_ms: int = 0
    method: str = "none"      # anchor | interpolated | none
    confidence: str = "low"   # high | medium | low
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.method != "none"

    @property
    def timecode(self) -> str:
        s, ms = divmod(self.start_ms, 1000)
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        return f"{h:d}:{m:02d}:{s:02d}.{ms:03d}"


# ---------------------------------------------------------------------------
# 1. group consecutive shots that come from the same episode
# ---------------------------------------------------------------------------

def _declared_range(shot: dict) -> tuple | None:
    """(lo, hi) in seconds from a shot's `scene_range`, if it states one."""
    from . import timings                                  # noqa: PLC0415
    got = timings.parse_range((shot or {}).get("scene_range") or "")
    return got if got and got[1] > got[0] else None


def _sequence_of(seen: list, span: tuple) -> int:
    """Which already-known sequence of this episode `span` belongs to.

    Overlapping or touching spans are the same sequence — a script that
    writes 19:00-24:30 for one moment and 24:00-29:30 for the next is
    describing one continuous stretch in two pieces, not two scenes. A span
    that touches nothing starts a new sequence, and one that touches an
    earlier sequence rejoins it, because an essay returns to the scene it
    opened with.
    """
    for i, have in enumerate(seen):
        if span[0] <= have[1] and have[0] <= span[1]:
            seen[i] = (min(have[0], span[0]), max(have[1], span[1]))
            return i
    seen.append(span)
    return len(seen) - 1


def runs(beats: list) -> list[Run]:
    """One walk through one *sequence* of one episode, in script order.

    Grouping only CONSECUTIVE shots looks more conservative and is much
    worse. An essay cuts away constantly — main scene, a flashback, back to
    the main scene — and on a real 106-shot script that produced 36 runs,
    twenty-three of them a single shot long. A lone silent shot has no anchor
    and cannot be placed at all, so the cutaways were not merely fragmenting
    the walk through the scene, they were deleting shots from the video.

    So shots are gathered by episode — but an episode is not always one
    sequence, and pretending it is destroyed a real build:

        Breaking Bad S04E01: 31 shot(s), 1 anchor(s)
        two lines put this run across 25 minutes of the episode

    Those 31 shots were three different parts of that episode: the cold open
    at 0-3:30, Gale's apartment at 3:30-13:00, and the box cutter at
    22:00-35:00. As one run they cannot all increase in time together, so
    `usable_anchors` dropped line after line — correctly, given what it had
    been told — until a single anchor was left holding 31 shots. The video
    came back 95% empty cards.

    The script had said so all along. Genspark writes `scene_range` on the
    first shot of each sequence, and five different ones appeared in that
    single run. A stated range is not a guess about *where* the shots are —
    it may be badly wrong about that — but it is an excellent statement of
    *which shots belong together*, which is all this needs. Shots with no
    range of their own stay in whichever sequence is in force, so a script
    that states none behaves exactly as it did before.
    """
    order: list = []
    by_key: dict = {}
    seen: dict = {}          # (source, episode) -> [merged spans]
    current: dict = {}       # (source, episode) -> sequence index in force
    for b in beats:
        beat_no = b.get("beat")
        for i, shot in enumerate(b.get("shots") or [], 1):
            src = (shot.get("source") or "").strip()
            se = str(shot.get("season_episode") or "unknown").strip()
            home = (src, se)
            span = _declared_range(shot)
            if span is not None:
                current[home] = _sequence_of(seen.setdefault(home, []), span)
            part = current.get(home, 0)
            key = (src, se, part)
            run = by_key.get(key)
            if run is None:
                run = by_key[key] = Run(source=src, season_episode=se,
                                        part=part)
                order.append(run)
            run.entries.append(Entry(beat=beat_no, shot=i, data=shot))
    return order


# ---------------------------------------------------------------------------
# 2. anchors — the handful of lines that really do match
# ---------------------------------------------------------------------------

def stated_anchors(db_path: str, run: Run, con=None) -> list[tuple]:
    """Anchors from times somebody typed, in the same shape as matched ones.

    A stated time is the only evidence in this package that was never
    inferred, so it enters as the strongest kind of anchor there is and the
    subtitle search is not run for that shot at all. Nothing downstream needs
    to know the difference: interpolation, ordering and the picture check all
    work on anchors, and these are anchors.
    """
    from . import timings                                  # noqa: PLC0415
    want = [(i, timings.shot_time(e.data or {}))
            for i, e in enumerate(run.entries)]
    want = [(i, at) for i, at in want if at is not None]
    if not want:
        return []
    home = episode_file(db_path, run, con=con)
    if not home:
        return []
    out = []
    for i, at in want:
        hold = max(0.5, run.entries[i].target_seconds)
        out.append((i, int(at * 1000), int((at + hold) * 1000), home, "high"))
    return out


def anchors_for(db_path: str, run: Run, con=None) -> list[tuple]:
    """[(index_in_run, start_ms, end_ms, path, confidence)] sorted by time.

    Searched inside the episode the script named. Searching the whole show
    instead was catastrophic and quiet: a line from a run declared S04E01
    matched somewhere in S02E07, `align_run` cuts the entire run from the
    first anchor's file, and 41 of 52 scenes came out of the wrong episode —
    the finished sheet was full of a mariachi band from the opening of "Negro
    y Azul". Every number in the report looked healthy while it happened.
    """
    from . import subtitles
    key = subtitles.episode_key(run.season_episode or "")
    season, episode = key if key else (None, None)

    found = stated_anchors(db_path, run, con=con)
    spoken_for = {a[0] for a in found}
    for i, e in enumerate(run.entries):
        if i in spoken_for or not e.query or e.is_hook:
            continue
        hits = find(db_path, e.query, show=run.source or None,
                    season=season, episode=episode, limit=1, con=con)
        if not hits or hits[0].confidence == "low":
            continue
        h = hits[0]
        found.append((i, h.start_ms, h.end_ms, h.path, h.confidence))

    # When the episode was not declared, anchors may land in different files.
    # The run is cut from ONE file, so anchors from any other are not
    # measurements of this run — they are a different scene entirely.
    if found and (season is None or episode is None):
        home = found[0][3]
        found = [f for f in found if f[3] == home]

    # Anchors must increase in time as they increase in index. Dropping
    # backwards one at a time cascades: on the real script the famous closing
    # line was also quoted at beat 1 as an opener, and unwinding from there
    # took five anchors down to one. Seventy shots then hung off a single
    # point. Keeping the longest run that IS in order throws out the odd
    # misplaced line instead of everything after it.
    found.sort(key=lambda a: a[0])
    return _longest_increasing(_densest(_last_of_each_moment(found), run))


# A run's anchors are only useful if they are talking about the same scene.
# Below this many there is nothing to cluster and every one is kept.
CLUSTER_MIN = 4
# How much film one run's worth of script may plausibly cover.
CLUSTER_SPAN = 1.5
CLUSTER_MIN_S = 180.0
CLUSTER_MAX_S = 900.0


def _densest(anchors: list, run: Run) -> list:
    """The anchors that agree with each other about where this scene is.

    `_longest_increasing` asks the wrong question when there are many
    anchors. It keeps the longest chain that runs forwards in time, which a
    handful of scattered wrong matches can easily win — they are in order
    too, they are just in order across the whole episode.

    A scene is a contiguous stretch of film. Lines that really belong to it
    CLUSTER. Lines that matched the wrong moment scatter. So the largest
    cluster is the scene, and everything outside it is noise, however neatly
    ordered the noise happens to be.

    Measured on a real script: one model wrote 48 quotes for an 88-shot run,
    plenty of them matched, and the pipeline came out with **one** anchor —
    "two lines put this run across 40 minutes of the episode, which is more
    than one sequence". The whole run then hung off that single point and
    landed nine minutes early. Keeping the cluster instead keeps the thirty
    that agreed.
    """
    if len(anchors) < CLUSTER_MIN:
        return anchors
    ax = axis(run)
    span = (ax[-1] - ax[0]) if len(ax) > 1 else 0.0
    width = min(CLUSTER_MAX_S, max(CLUSTER_MIN_S, span * CLUSTER_SPAN)) * 1000.0

    order = sorted(range(len(anchors)), key=lambda i: anchors[i][1])
    best, lo = (0, 0.0), 0
    for start in range(len(order)):
        stop = start
        while (stop + 1 < len(order)
               and anchors[order[stop + 1]][1] - anchors[order[start]][1] <= width):
            stop += 1
        # Most anchors wins; a tie goes to the tighter group, because two
        # windows holding the same lines are not equally good evidence.
        held = stop - start + 1
        tight = -(anchors[order[stop]][1] - anchors[order[start]][1])
        if (held, tight) > (best[0], best[1]):
            best, lo = (held, tight), anchors[order[start]][1]
    if best[0] <= len(anchors) // 2:
        return anchors                  # no majority agrees; keep everything
    return [a for a in anchors if lo <= a[1] <= lo + width]


def _last_of_each_moment(anchors: list) -> list:
    """One anchor per moment, and when a line is quoted twice, the later one.

    Essays open by quoting the ending. On the real script "Well? Get back to
    work." — the closing line of the scene — is quoted at index 0 as a hook
    and again at 50 and 61 where it belongs. Keeping the first occurrence
    pinned the end of the scene to the start of the run and laid all seventy
    shots AFTER it, so the video opened on the cleanup that follows the
    killing instead of on the killing.

    The later index is also the safer one when the choice is a guess: it puts
    most of the run before the anchor, and a scene almost always builds
    towards the line worth quoting rather than away from it.
    """
    keep: dict = {}
    for a in anchors:
        keep[a[1]] = a          # later indices arrive last and win
    return sorted(keep.values(), key=lambda a: a[0])


def _longest_increasing(anchors: list) -> list:
    """The largest subset whose times increase with their index.

    Ties in time — the same line quoted at three different beats resolves to
    the same moment — cannot all be kept, since two shots cannot both be at
    the same instant and in order. Strictly increasing keeps one of them.
    """
    if not anchors:
        return []
    n = len(anchors)
    best = [1] * n
    prev = [-1] * n
    for i in range(n):
        for j in range(i):
            if anchors[j][1] < anchors[i][1] and best[j] + 1 > best[i]:
                best[i], prev[i] = best[j] + 1, j
    end = max(range(n), key=lambda i: (best[i], anchors[i][4] == "high"))
    out = []
    while end != -1:
        out.append(anchors[end])
        end = prev[end]
    return out[::-1]


# ---------------------------------------------------------------------------
# 3. lay the described moments along the real shots
# ---------------------------------------------------------------------------

def axis(run: Run) -> list:
    """Where each shot sits along the scene, in seconds, per the script.

    The script states a duration for every shot. Laid end to end those give
    the scene's own shape — which shot is a third of the way in, which is near
    the end — and that is far better information than assuming every shot
    takes an equal share of some invented window.

    Assuming otherwise was a real failure, not a theoretical one. With one
    anchor the old code spread the run across a fixed 45 seconds however many
    shots there were, so seventy shots whose stated durations add to 254
    seconds were packed into 54 — one shot every 0.77 s. Every one of them
    landed in the same corner of the episode, and the contact sheet came back
    as the same red-lit frame over and over.
    """
    out, t = [], 0.0
    for e in run.entries:
        d = max(0.5, e.target_seconds)
        out.append(t + d / 2.0)
        t += d
    return out


def fit(run: Run, anchors: list) -> tuple:
    """(scale, offset) mapping the script's axis onto real episode time.

    One anchor pins the axis without stretching it — the axis is then the
    only statement about pacing there is, and it is a far better one than a
    fixed window. Two or more anchors measure the stretch directly, and that
    measurement wins: they are real times from the real episode, while the
    axis is only the shape between them.
    """
    ax = axis(run)
    if len(anchors) < 2:
        i, start = anchors[0][0], anchors[0][1]
        return 1.0, start - ax[i] * 1000.0

    first, last = anchors[0], anchors[-1]
    span_axis = ax[last[0]] - ax[first[0]]
    span_time = (last[1] - first[1]) / 1000.0
    scale = span_time / span_axis if span_axis > 0.01 else 1.0
    if not (MIN_SCALE <= scale <= MAX_SCALE):
        scale = 1.0
    return scale, first[1] - ax[first[0]] * scale * 1000.0


def rate_between(ax: list, a, b) -> float:
    """Seconds of film per second of script, between two anchors."""
    span_axis = ax[b[0]] - ax[a[0]]
    if span_axis <= 0.01:
        return 1.0
    return ((b[1] - a[1]) / 1000.0) / span_axis


def usable_anchors(run: Run, anchors: list, log=lambda *a: None) -> list:
    """Drop an anchor only when it disagrees with its NEIGHBOURS.

    The old rule was the run's total span: more than ten minutes and one of
    the lines was declared wrong. That was written for a real failure — a
    beat about the AUDIENCE quoted a line that matched far from the scene,
    and 103 shots ended up over sixteen minutes — but it punishes the shape
    rather than the fault. An essay legitimately visits one episode twice,
    and on a real script it cost 84 shots three of their four quoted lines,
    leaving the whole sequence hanging off a single point at its far end.

    What a wrong anchor actually produces is an impossible RATE: 33 seconds
    of script stretched over 16 minutes of film. That is what gets checked,
    pair by pair, so a bad line costs its own neighbourhood and nothing more.
    """
    if len(anchors) < 3:
        return anchors
    ax = axis(run)
    kept = list(anchors)
    while len(kept) > 2:
        bad = None
        for j in range(len(kept) - 1):
            r = rate_between(ax, kept[j], kept[j + 1])
            if not (MIN_SCALE <= r <= MAX_SCALE):
                bad = (j, r)
                break
        if bad is None:
            break
        j, r = bad
        # Drop the weaker of the pair: a low-confidence match before a high
        # one, and otherwise the one whose own neighbours disagree with it.
        a, b = kept[j], kept[j + 1]
        drop = j if a[4] != "high" and b[4] == "high" else (
            j + 1 if b[4] != "high" and a[4] == "high" else
            (j if j > 0 else j + 1))
        log(f"      the line at shot {kept[drop][0] + 1} implies "
            f"{r:.0f}x the pace of the script around it — dropped, the "
            "others still stand")
        kept.pop(drop)
    return kept


def stretch(run: Run, anchors: list) -> list:
    """Real episode time (ms) for every shot, from the anchors, piecewise.

    Between two anchors the script's own shape decides, and the two real
    timestamps decide the pace. Outside the outer anchors the nearest
    segment's pace carries on.

    A single global line through the first and last anchor was the previous
    method and it threw away everything in between: with four quoted lines it
    used two. Piecewise uses all of them, and confines a wrong one to the
    shots either side of it instead of tilting the whole run.
    """
    ax = axis(run)
    if not anchors:
        return [0.0 for _ in ax]
    if len(anchors) == 1:
        i, start = anchors[0][0], float(anchors[0][1])
        return [start + (a - ax[i]) * 1000.0 for a in ax]

    pts = [(ax[a[0]], float(a[1])) for a in anchors]
    rates = []
    for j in range(len(anchors) - 1):
        r = rate_between(ax, anchors[j], anchors[j + 1])
        rates.append(r if MIN_SCALE <= r <= MAX_SCALE else 1.0)

    out = []
    for a in ax:
        if a <= pts[0][0]:
            out.append(pts[0][1] + (a - pts[0][0]) * rates[0] * 1000.0)
        elif a >= pts[-1][0]:
            out.append(pts[-1][1] + (a - pts[-1][0]) * rates[-1] * 1000.0)
        else:
            j = 0
            while j < len(pts) - 2 and a > pts[j + 1][0]:
                j += 1
            out.append(pts[j][1] + (a - pts[j][0]) * rates[j] * 1000.0)
    return out


def episode_file(db_path: str, run: Run, con=None) -> str:
    """The video a run declares, found without needing a line from it.

    A run with no quoted line used to be dropped whole — 28 shots of a real
    script, three of its six runs, gone. But the script named the episode,
    and the library knows where that file is: the only thing missing was a
    reason to look, which is that the picture index can now place a shot
    without any dialogue at all.
    """
    from . import sources, subtitles
    key = subtitles.episode_key(run.season_episode or "")
    if not key or not run.source:
        return ""
    season, episode = key
    own = None
    if con is None:
        from .library import connect
        own = con = connect(db_path)
    try:
        want = sources.canonical(run.source)
        rows = con.execute(
            "SELECT path, show FROM media WHERE season=? AND episode=?",
            (season, episode)).fetchall()
        # A file that is no longer where the library remembers it is no use,
        # and it is the one a tidied folder leaves behind: the same episode
        # can be listed twice, once at a path that has gone. Prefer the one
        # that is actually on disk, and only fall back to the other so the
        # message stays "this episode is missing" rather than nothing.
        stale = ""
        for row in rows:
            have = sources.canonical(row["show"] or "")
            if not have or not (have == want or want in have or have in want):
                continue
            if os.path.isfile(row["path"]):
                return row["path"]
            stale = stale or row["path"]
        return stale
    finally:
        if own is not None:
            own.close()


def align_run(db_path: str, run: Run, con=None, log=lambda *a: None) -> list[Placement]:
    """Place every entry of one run along its scene."""
    out = [Placement(beat=e.beat, shot=e.shot) for e in run.entries]
    anchors = anchors_for(db_path, run, con=con)
    if not anchors:
        # No dialogue to stand on. The run is still worth handing on if the
        # episode is known: `verify` can place it from the pictures alone,
        # and until it does these stay method "none" so nothing is ever cut
        # from a position nobody has checked.
        path = episode_file(db_path, run, con=con)
        if not path:
            for p in out:
                p.note = "no anchor line in this run — cannot place it"
            return out
        try:
            duration = probe(path).duration
        except ProbeError:
            duration = 0.0
        log(f"    {run.label}: {len(out)} shot(s), no quoted line at all — "
            "only the pictures can place these")
        # The run's own length, laid across the middle of the episode — NOT
        # spread over the whole film.
        #
        # Spreading evenly was meant as a harmless placeholder, and it was
        # not harmless: five shots the script says are 22 seconds long were
        # placed 570 seconds apart, and then the picture layer moved one of
        # them and the rest interpolated from that spacing. They landed at
        # 1209s, 1778s, 2348s, 2918s and 3487s of a 2848-second episode —
        # two of them off the end of the film entirely, the others on a
        # lawyer's office in a scene about somebody else.
        #
        # A run is a sequence. Whatever else is unknown about it, its shots
        # are seconds apart, and the script says exactly how many.
        ax = axis(run)
        middle = ax[len(ax) // 2] if ax else 0.0
        centre = (duration / 2.0) if duration else max(ax[-1], 1.0)
        for i, (p, e) in enumerate(zip(out, run.entries)):
            p.path = path
            # Not a guess anyone should act on, which is why the method stays
            # "none" until something has actually looked — but the SHAPE is
            # real, and the shape is what everything downstream inherits.
            at = max(0.0, centre + (ax[i] - middle))
            if duration:
                at = min(at, max(0.0, duration - e.target_seconds))
            p.start_ms = int(at * 1000)
            p.end_ms = p.start_ms + int(e.target_seconds * 1000)
            p.note = "no quoted line anywhere near it — placed by picture only"
        return out

    path = anchors[0][3]
    try:
        duration = probe(path).duration
    except ProbeError:
        duration = 0.0

    ax = axis(run)
    anchors = usable_anchors(run, anchors, log)
    scale, _offset = fit(run, anchors)          # reported, not used to place
    times = stretch(run, anchors)
    if len(anchors) == 2 and (max(times) - min(times)) / 1000.0 > MAX_RUN_SPAN_S:
        # Two lines and nothing to arbitrate between them. Past ten minutes
        # one of them is describing a different sequence, and there is no
        # third anchor to say which — so the clearest one stands alone.
        keep = max(anchors, key=lambda a: (a[4] == "high", a[0]))
        log(f"      two lines put this run across "
            f"{(max(times) - min(times)) / 60000:.0f} minutes of the episode, "
            "which is more than one sequence — so one of them is wrong and "
            "only the clearest is used")
        anchors = [keep]
        scale, _offset = fit(run, anchors)
        times = stretch(run, anchors)
    lo = max(0.0, min(times) - 2000)
    hi = max(times) + 2000
    if duration:
        hi = min(hi, duration * 1000)
    log(f"    {run.label}: {len(run.entries)} shot(s), {len(anchors)} anchor(s), "
        f"span {lo/1000:.0f}s-{hi/1000:.0f}s "
        f"(script says {ax[-1] + run.entries[-1].target_seconds / 2:.0f}s, "
        f"x{scale:.2f})")
    if len(anchors) < 2 and len(run.entries) >= 4:
        # One anchor fixes WHERE the run sits but not which way it runs. If
        # the script put that line at the wrong end, every shot lands on the
        # wrong side of it — which is how seventy shots of a killing came
        # back as the cleanup that follows it.
        at = anchors[0][0]
        log(f"      only one line matched, at shot {at + 1} of "
            f"{len(run.entries)}. Everything else is placed relative to it, "
            "so if that line is not really there, none of them are. A second "
            "quoted line anywhere else in this run would fix that.")

    try:
        boundaries = cutter.detect_shots(path, lo / 1000, hi / 1000)
    except ProbeError as exc:
        boundaries = []
        log(f"      shot detection unavailable ({exc})")

    # Snapping helps only when a boundary is genuinely near. With shots this
    # close together a distant one belongs to a neighbour, so the reach is
    # never more than half the gap to the next placement.
    spacing = (hi - lo) / 1000.0 / max(1, len(run.entries))
    snap_limit = min(MAX_SNAP_S, max(0.5, spacing / 2.0))

    anchor_at = {a[0]: a for a in anchors}
    used: list[float] = []

    for i, e in enumerate(run.entries):
        p = out[i]
        p.path = path
        if e.is_hook and e.query:
            # Cut it where the line really is, but it took no part in
            # deciding where anything else goes.
            hits = find(db_path, e.query, show=run.source or None,
                        limit=1, con=con)
            if hits and hits[0].confidence != "low":
                h = hits[0]
                p.start_ms, p.end_ms = h.start_ms, h.end_ms
                p.method, p.confidence = "anchor", h.confidence
                p.note = "matched on a hook quote — not used for ordering"
                continue
        if i in anchor_at:
            _, s_ms, e_ms, _p, conf = anchor_at[i]
            p.start_ms, p.end_ms = s_ms, e_ms
            p.method, p.confidence = "anchor", conf
            p.note = "matched on dialogue"
            used.append(s_ms / 1000)
            continue

        want = max(0.0, times[i] / 1000.0)
        # snap to the nearest shot boundary that is not already spoken for
        candidates = [b for b in boundaries
                      if all(abs(b - u) > MIN_SEPARATION_S for u in used)]
        p.method = "interpolated"
        nearest = min(candidates, key=lambda b: abs(b - want)) if candidates else None
        if nearest is not None and abs(nearest - want) <= snap_limit:
            chosen = nearest
            p.confidence = "medium"
            p.note = (f"placed between anchors, snapped to a shot "
                      f"{abs(chosen - want):.1f}s away")
        else:
            # No boundary close enough. Detection misses low-contrast cuts, so
            # the estimate is the better answer than a distant boundary.
            chosen = want
            p.confidence = "low"
            p.note = "placed between anchors, no shot boundary nearby"
        used.append(chosen)
        p.start_ms = int(max(0.0, chosen) * 1000)
        p.end_ms = int(p.start_ms + e.target_seconds * 1000)

    return out


@dataclass
class QuoteReport:
    """What the script CLAIMED it quoted, against what the subtitles have.

    The prompt asks the model writing a scene breakdown for one verbatim line
    per ten shots, spread through the run, and it asks it to count them back
    in a summary block. That summary is the model marking its own homework.

    On the real script it reported fifteen verbatim lines; six of them
    actually matched anything, and the ones that did not were paraphrases —
    close enough to read as a quote, not close enough to be one. Nothing in
    the pipeline said so, and the shortfall only became visible three stages
    later as a run with a single anchor at its far end.

    So the tool counts them itself, before anything is built, and names the
    lines that were not found. That is the difference between "the script
    says it did the right thing" and "the right thing is in the index".
    """
    given: int = 0                       # shots carrying a quote
    matched: int = 0                     # ...that the subtitles confirm
    misses: list = field(default_factory=list)      # (beat, shot, quote)
    runs: int = 0
    runs_without_anchor: list = field(default_factory=list)
    longest_gap: int = 0                 # most shots in a row with no anchor

    @property
    def rate(self) -> float:
        return self.matched / self.given if self.given else 0.0

    def detail(self) -> str:
        bits = [f"{self.matched}/{self.given} quoted line(s) found"]
        if self.runs_without_anchor:
            bits.append(f"{len(self.runs_without_anchor)} of {self.runs} run(s) "
                        "have none at all")
        if self.longest_gap:
            bits.append(f"longest stretch without one: {self.longest_gap} shots")
        return ", ".join(bits)

    def advice(self) -> list:
        """What to change in the script, in the words the writer needs."""
        out = []
        for beat, shot, quote in self.misses[:8]:
            out.append(f"beat {beat} shot {shot}: not in the subtitles — "
                       f'"{quote[:60]}"')
        if self.misses:
            out.append("These read like quotes but are not word for word. "
                       "Copy them from the subtitle file, or drop them.")
        for label in self.runs_without_anchor[:5]:
            out.append(f"{label}: no quoted line anywhere in it")
        return out


def quote_report(db_path: str, beats: list, con=None) -> QuoteReport:
    """Count the quotes that are real, before a single frame is rendered."""
    own = None
    if con is None:
        from .library import connect
        own = con = connect(db_path)
    rep = QuoteReport()
    try:
        from . import subtitles
        for run in runs(beats):
            rep.runs += 1
            key = subtitles.episode_key(run.season_episode or "")
            season, episode = key if key else (None, None)
            hits_here = 0
            gap = 0
            for e in run.entries:
                if not e.query:
                    gap += 1
                    rep.longest_gap = max(rep.longest_gap, gap)
                    continue
                rep.given += 1
                hits = find(db_path, e.query, show=run.source or None,
                            season=season, episode=episode, limit=1, con=con)
                if hits and hits[0].confidence != "low":
                    rep.matched += 1
                    hits_here += 1
                    gap = 0
                else:
                    rep.misses.append((e.beat, e.shot, e.query))
                    gap += 1
                    rep.longest_gap = max(rep.longest_gap, gap)
            if not hits_here and len(run.entries) >= MIN_RUN:
                rep.runs_without_anchor.append(run.label)
        return rep
    finally:
        if own is not None:
            own.close()


def placeable(db_path: str, beats: list, con=None) -> tuple:
    """(placeable, total) shots, without decoding a single frame.

    The gate has to answer "can this be built?" before anything renders, and
    the honest answer changed when alignment arrived. Counting only shots that
    match dialogue said 7/106 on a real script and blocked it — while the
    builder, given the chance, places most of those 106, because a run needs
    one quoted line to carry all the silent shots around it.

    Anchors are database lookups. Shot detection is not, so it is left to the
    build; the difference it makes is where inside a second a clip starts, not
    whether the shot can be placed at all.
    """
    own = None
    if con is None:
        from .library import connect
        own = con = connect(db_path)
    try:
        placed = total = 0
        for run in runs(beats):
            n = len(run.entries)
            total += n
            anchors = anchors_for(db_path, run, con=con)
            if not anchors:
                # No dialogue — but if the episode is known AND its pictures
                # have been indexed, the run can still be placed. Reporting
                # it as unbuildable would understate the gate by three runs
                # of a real script, and understating it blocks builds that
                # would have worked.
                path = episode_file(db_path, run, con=con)
                from . import visual
                if path and visual.load(con, db_path, path) is not None:
                    placed += n
                continue
            # A run too short to align is only as good as its own anchors.
            placed += n if n >= MIN_RUN else len(anchors)
        return placed, total
    finally:
        if own is not None:
            own.close()


def align(db_path: str, beats: list, log=lambda *a: None) -> list[Placement]:
    """Align every run in a script. Runs too short to align are left alone."""
    from .library import connect
    con = connect(db_path)
    try:
        placements = []
        for run in runs(beats):
            if len(run.entries) < MIN_RUN:
                placements += [Placement(beat=e.beat, shot=e.shot,
                                         note="run too short to align")
                               for e in run.entries]
                continue
            placements += align_run(db_path, run, con=con, log=log)
        return placements
    finally:
        con.close()


def summarise(placements: list[Placement]) -> str:
    from . import term
    anchored = sum(1 for p in placements if p.method == "anchor")
    verified = sum(1 for p in placements if p.method == "verified")
    interp = sum(1 for p in placements if p.method == "interpolated")
    none = sum(1 for p in placements if p.method == "none")
    total = len(placements) or 1
    return (f"  {anchored} anchored on dialogue "
            + (f"{term.sym('dot')} {verified} confirmed by the picture "
               if verified else "")
            + f"{term.sym('dot')} {interp} placed along the scene "
            f"{term.sym('dot')} {none} unplaced "
            f"({(anchored + verified + interp) / total:.0%} usable)")
