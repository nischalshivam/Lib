"""A searchable index of what every episode actually *looks* like.

`library.db` answers "where is this line spoken". This answers "where does
this episode look like a man in a red hazmat suit picking up a box cutter" —
and it answers it for a description that was never spoken aloud, which is 92%
of a real scene breakdown.

The shape is deliberately the same as the dialogue index, because the same
shape is what made the dialogue index fast enough to live with:

    index once, per episode, slowly       (minutes, and only when it changes)
    query many times, per shot, instantly (a matrix multiply)

A 47-minute episode sampled every two seconds is about 1,400 frames. Encoding
those takes a few minutes on a CPU. Doing it per shot instead — 147 shots,
each scanning its own window — would repeat that work until the build took
all night, and would still see less of the episode. So the frames are encoded
once into a file beside `library.db`, and every script written about that
episode afterwards queries it for free.

## Scores are relative, always

The raw similarity between an image and a sentence has no meaning on its own.
It shifts with the model, with how long the sentence is, with how dark the
episode is. A fixed threshold tuned on Breaking Bad would be wrong on an
anime and wrong again on a documentary — which is precisely the trap this
whole layer exists to escape.

So a match is judged against **the same episode's own distribution**:

    lift = (best - median) / (p95 - median)

The median is what this description scores against a typical frame of this
episode; the 95th percentile is what the better frames score. A lift of 1.0
means the winner is merely a good frame. A lift of 2.5 means it stands as far
beyond the top 5% as the top 5% stands beyond ordinary — which no frame
reaches by chance. Nothing about that number depends on the title, the genre
or the model, which is the only reason it can be trusted on the next script.

This is the same idea `sync.py` uses to decide whether a subtitle offset is
real, and for the same reason.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field

import numpy as np

from . import embed
from .probe import ProbeError, probe, require_ffmpeg

# One frame every two seconds. Shots in this kind of footage last three to
# five, so nothing meaningful is skipped, and halving it would double an
# already slow one-time cost for pictures that mostly repeat.
DEFAULT_FPS = 0.5
FRAME_BATCH = 16                # frames handed to the model at a time
DECODE_TIMEOUT = 3600           # a feature film at 0.5 fps, with room to spare

# How far beyond ordinary a frame must stand before its match is believed.
# Starting values, chosen to be strict: on a 1,400-frame episode the p95 gap
# is a wide target, and clearing it twice over does not happen by accident.
LIFT_STRONG = 2.0
LIFT_OK = 1.2
# Past this the number stops carrying information and starts making logs hard
# to read. "Twelve" and "four hundred" both mean the same thing: nothing else
# in this episode came close.
LIFT_CEILING = 10.0


class VisualError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# reading frames
# ---------------------------------------------------------------------------

def frame_batches(path: str, fps: float = DEFAULT_FPS,
                  size: int = embed.IMAGE_SIZE, timeout: int = DECODE_TIMEOUT):
    """Yield (times, pixels) in batches, streaming — never the whole film.

    A 47-minute episode at this rate is 1,400 frames, which is 212 MB of raw
    pixels. Holding all of it to hand to the model in one call would be the
    easy way to write this and the reason it fell over on a feature film.
    """
    cmd = [require_ffmpeg(), "-v", "error", "-i", path,
           "-vf", f"fps={fps},scale={size}:{size},format=rgb24",
           "-f", "rawvideo", "-"]
    frame_bytes = size * size * 3
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    index = 0
    finished = False
    deadline = time.time() + timeout
    try:
        while True:
            want = frame_bytes * FRAME_BATCH
            buf = bytearray()
            while len(buf) < want:
                chunk = proc.stdout.read(want - len(buf))
                if not chunk:
                    break
                buf += chunk
            if time.time() > deadline:
                raise VisualError(f"frame read timed out after {timeout}s")
            whole = len(buf) // frame_bytes
            if not whole:
                finished = True
                break
            pixels = np.frombuffer(bytes(buf[:whole * frame_bytes]),
                                   dtype=np.uint8).reshape(whole, size, size, 3)
            times = np.array([(index + i) / fps for i in range(whole)],
                             dtype=np.float32)
            index += whole
            yield times, pixels
            if whole < FRAME_BATCH:
                finished = True
                break
    finally:
        # Never raise from here. A caller that stops early — a `break`, or
        # the generator being collected — unwinds through this block, and an
        # exception raised during that unwinding replaces whatever the caller
        # was actually doing with a confusing one from the cleanup.
        _shutdown(proc, drain=finished)
    if finished and index == 0:
        raise ProbeError(f"could not read frames from {os.path.basename(path)}")


def _shutdown(proc, drain: bool) -> None:
    """Close a decoder down without letting the teardown raise."""
    try:
        if not drain and proc.poll() is None:
            proc.kill()             # caller stopped early; do not wait for it
    except OSError:
        pass
    for pipe in (proc.stdout, proc.stderr):
        try:
            if pipe is not None:
                pipe.close()
        except OSError:
            pass
    try:
        proc.wait(timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


# ---------------------------------------------------------------------------
# the index itself
# ---------------------------------------------------------------------------

@dataclass
class VisualIndex:
    """Every sampled frame of one video, as a unit vector."""
    path: str
    times: np.ndarray                       # seconds, ascending
    vecs: np.ndarray                        # (N, dim) float32 unit vectors
    model: str = ""
    fps: float = DEFAULT_FPS

    def __len__(self) -> int:
        return len(self.times)

    def similarities(self, text_vec: np.ndarray) -> np.ndarray:
        """One score per frame. Cosine, because both sides are unit length."""
        v = np.asarray(text_vec, dtype=np.float32).reshape(-1)
        if not len(self) or v.shape[0] != self.vecs.shape[1]:
            return np.zeros(len(self), dtype=np.float32)
        return self.vecs @ v

    def window(self, lo: float, hi: float) -> np.ndarray:
        """Indices of the frames inside a time range, in order."""
        return np.nonzero((self.times >= lo) & (self.times <= hi))[0]


@dataclass
class Match:
    """One frame that answered one description."""
    time: float = 0.0
    similarity: float = 0.0
    lift: float = 0.0
    searched: int = 0
    scope: str = "window"           # window | episode
    note: str = ""

    @property
    def confidence(self) -> str:
        if self.lift >= LIFT_STRONG:
            return "high"
        if self.lift >= LIFT_OK:
            return "medium"
        return "low"

    @property
    def believable(self) -> bool:
        return self.lift >= LIFT_OK and self.searched > 0


def lift_of(sims: np.ndarray, value: float) -> float:
    """How far past ordinary one score stands, in this episode's own terms.

    Median and 95th percentile rather than mean and standard deviation: a
    handful of near-identical frames — a long static shot, a title card —
    drags a mean around, and this has to survive that.
    """
    if sims.size < 8:
        return 0.0
    med = float(np.median(sims))
    p95 = float(np.percentile(sims, 95))
    spread = p95 - med
    excess = float(value) - med
    if spread <= 1e-6:
        # Every frame scored alike. Either this one did too — which says
        # nothing — or it is the single thing in the episode that stood out,
        # which says everything. Dividing by the spread would call both of
        # them zero, and the second answer would be exactly backwards.
        return 0.0 if excess <= 1e-6 else LIFT_CEILING
    return min(LIFT_CEILING, excess / spread)


def lifts_of(sims: np.ndarray) -> np.ndarray:
    """`lift_of` for every frame at once, and agreeing with it frame by frame."""
    if sims.size < 8:
        return np.zeros_like(sims)
    med = float(np.median(sims))
    spread = float(np.percentile(sims, 95)) - med
    if spread <= 1e-6:
        # Same reading as `lift_of`: every frame alike says nothing, and the
        # one frame that is not alike says everything.
        return np.where(sims - med > 1e-6, LIFT_CEILING, 0.0).astype(sims.dtype)
    return np.clip((sims - med) / spread, -LIFT_CEILING, LIFT_CEILING)


def best_in(index: VisualIndex, text_vec: np.ndarray,
            lo: float | None = None, hi: float | None = None,
            bonus: np.ndarray | None = None) -> Match:
    """The frame that best answers this description, optionally in a range.

    The lift is always measured against the WHOLE episode even when the
    search was limited to a window, and that is the important part: a window
    of twenty frames has no distribution worth comparing against, so a
    window-local score would call the least bad of twenty frames a match.

    `bonus` is a per-frame number in lift units — what somebody knows that
    the description does not say, at the moment this is written that means
    which characters are in the frame. It is added, not multiplied, so a
    frame with nobody recognisable in it is left exactly where it was.
    """
    if not len(index):
        return Match(note="nothing indexed for this video")
    sims = index.similarities(text_vec)
    if not np.any(sims):
        return Match(note="description could not be encoded")

    if lo is None and hi is None:
        pool = np.arange(len(index))
        scope = "episode"
    else:
        pool = index.window(lo if lo is not None else -1e9,
                            hi if hi is not None else 1e9)
        scope = "window"
        if not pool.size:
            return Match(scope=scope, note="no indexed frame in that window")

    scored = lifts_of(sims)
    if bonus is not None and len(bonus) == len(sims):
        scored = scored + np.asarray(bonus, dtype=np.float32)
    winner = int(pool[int(np.argmax(scored[pool]))])
    return Match(time=float(index.times[winner]),
                 similarity=float(sims[winner]),
                 lift=float(scored[winner]),
                 searched=int(pool.size), scope=scope)


def top_in(index: VisualIndex, text_vec: np.ndarray, n: int = 10,
           apart: float = 8.0, lo: float | None = None,
           hi: float | None = None) -> list:
    """The n best frames for one description — but n DIFFERENT ones.

    `best_in` answers "where is this?", which is what a build needs. A person
    looking at a wrong shot needs something else: a choice. Taking the ten
    highest scores would hand them ten frames of the same two seconds, which
    is one choice wearing ten hats.

    So each pick suppresses everything within `apart` seconds of it. Ten
    genuinely different moments beat ten samples of the best one, even when
    some of them score lower — the score is a guess and the eye is not.
    """
    if not len(index):
        return []
    sims = index.similarities(text_vec)
    if not np.any(sims):
        return []
    if lo is None and hi is None:
        pool = np.arange(len(index))
        scope = "episode"
    else:
        pool = index.window(lo if lo is not None else -1e9,
                            hi if hi is not None else 1e9)
        scope = "window"
    if not pool.size:
        return []

    order = pool[np.argsort(-sims[pool])]
    picked: list = []
    for i in order:
        when = float(index.times[int(i)])
        if any(abs(when - m.time) < apart for m in picked):
            continue
        picked.append(Match(time=when, similarity=float(sims[int(i)]),
                            lift=lift_of(sims, sims[int(i)]),
                            searched=int(pool.size), scope=scope))
        if len(picked) >= n:
            break
    return picked


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def store_dir(db_path: str) -> str:
    """Vectors live beside library.db, in their own folder.

    Not inside the database. A 1,400-frame episode is 4 MB of float32, and 62
    of them would quadruple a file that is opened, copied and backed up by
    everything else in the tool. numpy also reads an .npz back in one call,
    which sqlite blobs would not.
    """
    base = os.path.splitext(os.path.abspath(db_path))[0]
    return base + "_visual"


def _vector_file(db_path: str, video_path: str) -> str:
    key = hashlib.sha1(os.path.abspath(video_path).encode("utf-8")).hexdigest()
    return os.path.join(store_dir(db_path), key[:16] + ".npz")


def vectors_file(con, db_path: str, video_path: str, stored: str) -> str:
    """Where this row's frames actually are now.

    The row holds an absolute path, which stays true right up until the
    library folder itself moves — copied to an SSD, or tidied into
    E:\\Libraries\\<title>\\ with the database. Not one frame changed; only
    the road to them did. Without this, every episode would look unindexed
    and the slowest step in the tool would run again for nothing, which is
    the same accident `rehome` exists to prevent for moved footage.

    The file name is a hash, so it is unique: the same name in the store
    beside THIS database is the same vectors. Found that way, the row is
    corrected, so a moved library costs one lookup rather than one per run.
    """
    if stored and os.path.isfile(stored):
        return stored
    if not stored:
        return ""
    beside = os.path.join(store_dir(db_path), os.path.basename(stored))
    if not os.path.isfile(beside):
        return stored                   # genuinely gone; the caller decides
    try:
        con.execute("UPDATE visual SET vectors=? WHERE path=?",
                    (beside, os.path.abspath(video_path)))
        con.commit()
    except sqlite3.Error:
        pass                            # reading still works; only the
    return beside                       # repair was optional


def _stamp(video_path: str) -> tuple:
    st = os.stat(video_path)
    return st.st_size, int(st.st_mtime)


def rehome(con, video_path: str) -> bool:
    """Follow a file that was simply moved, rather than indexing it again.

    Someone who tidies "D:\\Breaking Bad Season 5" into "D:\\Breaking Bad"
    has not changed a single frame, but every row here is keyed by absolute
    path, so all of it would look unindexed and the slowest step in the tool
    would run again for nothing.

    Same name, same byte count, same modification time is the same file.
    Only a lone match is followed: two identical copies in two folders is
    exactly the situation where guessing is wrong.
    """
    try:
        size, mtime = _stamp(video_path)
    except OSError:
        return False
    name = os.path.basename(video_path)
    rows = [r for r in con.execute(
        "SELECT path FROM visual WHERE file_size=? AND file_mtime=?",
        (size, mtime)).fetchall()
        if os.path.basename(r["path"]) == name
        and not os.path.isfile(r["path"])]
    if len(rows) != 1:
        return False
    con.execute("UPDATE visual SET path=? WHERE path=?",
                (os.path.abspath(video_path), rows[0]["path"]))
    con.commit()
    return True


def is_current(con, db_path: str, video_path: str, model: str,
               fps: float = DEFAULT_FPS) -> bool:
    """Has this exact video already been indexed with this exact model?"""
    row = con.execute(
        "SELECT file_size, file_mtime, model, fps, vectors FROM visual "
        "WHERE path=?", (os.path.abspath(video_path),)).fetchone()
    if not row and rehome(con, video_path):
        row = con.execute(
            "SELECT file_size, file_mtime, model, fps, vectors FROM visual "
            "WHERE path=?", (os.path.abspath(video_path),)).fetchone()
    if not row:
        return False
    try:
        size, mtime = _stamp(video_path)
    except OSError:
        return False
    return (row["file_size"] == size and row["file_mtime"] == mtime
            and row["model"] == model and abs(row["fps"] - fps) < 1e-6
            and os.path.isfile(vectors_file(con, db_path, video_path,
                                            row["vectors"])))


def load(con, db_path: str, video_path: str) -> VisualIndex | None:
    """Read one video's vectors back, or None if it was never indexed."""
    row = con.execute(
        "SELECT model, fps, vectors FROM visual WHERE path=?",
        (os.path.abspath(video_path),)).fetchone()
    if not row and rehome(con, video_path):
        row = con.execute(
            "SELECT model, fps, vectors FROM visual WHERE path=?",
            (os.path.abspath(video_path),)).fetchone()
    vectors = vectors_file(con, db_path, video_path,
                           row["vectors"]) if row else ""
    if not row or not os.path.isfile(vectors):
        return None
    try:
        with np.load(vectors) as z:
            times = np.asarray(z["times"], dtype=np.float32)
            vecs = np.asarray(z["vecs"], dtype=np.float32)
    except (OSError, ValueError, KeyError):
        return None
    return VisualIndex(path=os.path.abspath(video_path), times=times,
                       vecs=vecs, model=row["model"], fps=row["fps"])


@dataclass
class BuildResult:
    indexed: int = 0
    skipped: int = 0
    failed: list = field(default_factory=list)      # [(path, reason)]
    frames: int = 0
    seconds: float = 0.0


def index_video(con, db_path: str, video_path: str, backend=None,
                fps: float = DEFAULT_FPS, force: bool = False,
                log=lambda *a: None) -> int:
    """Encode one video's frames and store them. Returns frames written."""
    backend = backend or embed.load(log=log)
    video_path = os.path.abspath(video_path)
    if not force and is_current(con, db_path, video_path, backend.name, fps):
        return 0

    try:
        duration = probe(video_path).duration
    except ProbeError:
        duration = 0.0
    expect = int(duration * fps) if duration else 0
    name = os.path.basename(video_path)
    log(f"    {name}" + (f"  (~{expect} frames)" if expect else ""))

    times_all, vecs_all = [], []
    t0 = time.time()
    for times, pixels in frame_batches(video_path, fps=fps):
        vecs_all.append(backend.encode_images(pixels))
        times_all.append(times)
        n = sum(len(t) for t in times_all)
        if expect and n % (FRAME_BATCH * 20) == 0:
            log(f"      {n}/{expect} frames  ({time.time() - t0:.0f}s)")
    if not times_all:
        raise VisualError("no frames could be read")

    times = np.concatenate(times_all).astype(np.float32)
    vecs = np.concatenate(vecs_all).astype(np.float32)

    os.makedirs(store_dir(db_path), exist_ok=True)
    out = _vector_file(db_path, video_path)
    np.savez_compressed(out, times=times, vecs=vecs.astype(np.float16))
    size, mtime = _stamp(video_path)
    con.execute(
        "INSERT OR REPLACE INTO visual"
        " (path, file_size, file_mtime, model, fps, frames, dim, vectors,"
        "  built_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (video_path, size, mtime, backend.name, float(fps), int(len(times)),
         int(vecs.shape[1]), out, int(time.time())))
    con.commit()
    log(f"      {len(times)} frames in {time.time() - t0:.0f}s")
    return int(len(times))


def files_for_script(db_path: str, beats: list) -> list:
    """The videos one script actually needs, in library order.

    Looking at a whole five-season library takes hours; a script uses three
    episodes of it. Indexing everything is the right thing to leave running
    overnight and the wrong thing to demand before someone can test one
    script, so a script can name its own shortlist.

    Titles are matched the way `sources` matches them — loosely, because a
    script writes "Breaking Bad" and a file is called "Breaking.Bad.S04E01" —
    and an episode the script did not declare is left out.
    """
    from . import sources
    from .library import connect
    con = connect(db_path)
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT path, show, season, episode FROM media ORDER BY path")]
    finally:
        con.close()

    wanted: list = []
    for req in sources.requirements(beats):
        key = sources.canonical(req.title)
        if not key:
            continue
        episodes = req.episodes_declared
        for row in rows:
            lib = sources.canonical(row["show"] or "")
            if not lib or not (lib == key or key in lib or lib in key):
                continue
            se = (row["season"], row["episode"])
            # A film has no season or episode, so it is always wanted; an
            # episode is wanted only if the script asked for it by number.
            if episodes and row["season"] is not None and se not in episodes:
                continue
            if row["path"] not in wanted:
                wanted.append(row["path"])
    return wanted


def build(db_path: str, only: list | None = None, fps: float = DEFAULT_FPS,
          force: bool = False, log=lambda *a: None) -> BuildResult:
    """Index the pictures of every video already in the dialogue index.

    Driven off `media` rather than off a folder, so this can only ever see
    files the dialogue index already knows about — one list of what you own,
    not two that can disagree.
    """
    from . import lockfile
    from .library import connect
    res = BuildResult()
    t0 = time.time()
    # Held before anything is opened. Two of these on one library do not go
    # twice as fast; they go slower than one, fight over the same file, and
    # neither of them looks broken while it happens.
    with lockfile.held(db_path, "pictures padhna", log=log):
        con = connect(db_path)
        try:
            backend = embed.load(log=log)
            rows = con.execute("SELECT path FROM media ORDER BY path").fetchall()
            paths = [r["path"] for r in rows]
            if only:
                wanted = {os.path.abspath(p) for p in only}
                paths = [p for p in paths if os.path.abspath(p) in wanted]
            log(f"  {len(paths)} video(s) to look at")
            for path in paths:
                if not os.path.isfile(path):
                    res.failed.append((path, "file is gone"))
                    continue
                try:
                    n = index_video(con, db_path, path, backend=backend,
                                    fps=fps, force=force, log=log)
                except (ProbeError, VisualError, OSError) as exc:
                    res.failed.append((path, str(exc)))
                    log(f"      failed — {exc}")
                    continue
                # Still here. A lock nobody touches is treated as abandoned,
                # which is the right answer after a laptop lid closes.
                lockfile.touch(db_path)
                if n:
                    res.indexed += 1
                    res.frames += n
                else:
                    res.skipped += 1
        finally:
            con.close()
    res.seconds = time.time() - t0
    return res


def coverage(db_path: str) -> tuple:
    """(videos with pictures indexed, videos in the library)."""
    from .library import connect
    con = connect(db_path)
    try:
        total = con.execute("SELECT COUNT(*) c FROM media").fetchone()["c"]
        done = con.execute(
            "SELECT COUNT(*) c FROM visual v JOIN media m ON m.path = v.path"
        ).fetchone()["c"]
        return done, total
    finally:
        con.close()
