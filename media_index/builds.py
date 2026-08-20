"""Running a build from the browser, without the browser waiting for it.

Everything the menu does in one long blocking run — pre-flight, cut the
footage, time it, render it — happens here on a thread, with a status a page
can ask about every second. Nothing new is decided: this calls exactly the
same functions the menu calls, in the same order, so a video built from the
form is the same video, byte for byte, as one built from `9`, `T`, `R`.

## Why a task and not a request

A pre-flight on a 55-beat script resolves every shot against the whole
library, and a build is forty minutes. Both are far past what a browser will
sit still for, and a page that dies at ninety seconds looks exactly like a
tool that crashed. So a request starts a task and gets an id back, and the
page asks how it is going.

## What is deliberately not here

No queue. One thing at a time, because two builds at once would fight over
ffmpeg and the disk and finish slower than one after the other — and because
"which of these two failed?" is a question nobody should have to answer.
"""
from __future__ import annotations

import os
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field

from . import align, jobs as jobs_mod, term

# How many log lines a task keeps. Enough to see what went wrong, bounded so
# a forty-minute render cannot grow without limit.
KEPT_LINES = 400


@dataclass
class Task:
    """One check or one build, and everything a page can ask about it."""
    id: str
    kind: str                       # "check" | "build"
    name: str = ""
    status: str = "running"         # queued | running | done | failed | blocked
    stage: str = ""                 # the human line under the bar
    scenes_done: int = 0
    scenes_total: int = 0
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    error: str = ""
    report: dict = field(default_factory=dict)      # a check's verdict
    out: str = ""
    video: str = ""
    lines: list = field(default_factory=list)

    @property
    def seconds(self) -> float:
        return (self.finished or time.time()) - self.started

    @property
    def percent(self) -> int:
        if self.status in ("done", "failed", "blocked"):
            return 100
        if self.status == "queued":
            return 0
        if not self.scenes_total:
            return 0
        return min(99, int(self.scenes_done * 100 / self.scenes_total))

    def as_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "name": self.name,
                "status": self.status, "stage": self.stage,
                "scenes_done": self.scenes_done,
                "scenes_total": self.scenes_total,
                "percent": self.percent, "seconds": round(self.seconds, 1),
                "error": self.error, "report": self.report, "out": self.out,
                "video": self.video, "lines": self.lines[-40:]}


class Runner:
    """Every task this session has run, and the one that is running."""

    def __init__(self):
        self._tasks: dict = {}
        self._order: list = []
        self._lock = threading.Lock()
        self._busy = threading.Lock()

    def get(self, task_id: str):
        with self._lock:
            return self._tasks.get(task_id)

    def all(self) -> list:
        with self._lock:
            return [self._tasks[i].as_dict() for i in self._order
                    if i in self._tasks]

    def _new(self, kind: str, name: str) -> Task:
        task = Task(id=uuid.uuid4().hex[:12], kind=kind, name=name)
        with self._lock:
            self._tasks[task.id] = task
            self._order.append(task.id)
        return task

    def _log(self, task: Task):
        def write(*parts):
            text = " ".join(str(p) for p in parts).rstrip()
            if not text:
                return
            with self._lock:
                task.lines.append(text)
                del task.lines[:-KEPT_LINES]
            # The build's own log is the only thing that knows how far in it
            # is. Reading the count off it beats threading a progress
            # callback through five modules that have no other reason to
            # know a browser exists.
            stripped = text.strip()
            if stripped.startswith("scene ") and task.scenes_total:
                head = stripped.split()[1]
                if head.isdigit():
                    task.scenes_done = max(task.scenes_done, int(head))
            task.stage = text.strip()[:160]
        return write

    def start(self, kind: str, name: str, work) -> Task:
        """Run `work(task, log)` on a thread. One at a time."""
        task = self._new(kind, name)

        def run():
            log = self._log(task)
            # Waiting is not working, and a page that says "working" for
            # twenty-three minutes while the task has not started yet is the
            # tool lying to somebody who is trying to decide whether it has
            # crashed. Only claim to be running once we actually are.
            if self._busy.locked():
                task.status = "queued"
                task.stage = ("kataar me — pehle wala kaam khatam hone ka "
                              "intezaar")
            with self._busy:
                task.status = "running"
                outcome, error = "done", ""
                try:
                    work(task, log)
                    if task.status != "running":
                        outcome = task.status   # work reached its own verdict
                except Exception as exc:        # a task must never take the
                    outcome = "failed"          # server down with it
                    error = f"{type(exc).__name__}: {exc}"
                    log(error)
                    log(traceback.format_exc(limit=3))
                if error:
                    task.error = error
                task.finished = time.time()
                # Status changes last, and on purpose. The page stops asking
                # the moment it stops saying "running", so anything it will
                # want to read afterwards — the error, the elapsed time —
                # has to already be there when it looks.
                task.status = outcome

        threading.Thread(target=run, daemon=True).start()
        return task


def job_from(spec: dict, db: str) -> jobs_mod.Job:
    """A form's answers as the Job every other module already understands."""
    return jobs_mod.Job(
        name=(spec.get("name") or "video").strip(),
        script=os.path.abspath(spec.get("script") or ""),
        audio=os.path.abspath(spec["audio"]) if spec.get("audio") else "",
        out=os.path.abspath(spec.get("out") or "output"),
        db=os.path.abspath(spec.get("db") or db),
        clip_seconds=float(spec.get("clip_seconds") or 4.0),
        stills_per_scene=int(spec.get("stills") or 2),
        extras={k: v for k, v in spec.items()
                if k in ("pace", "quality", "captions", "preset", "after",
                         "transitions", "filters", "animation", "title",
                         "timings", "cast", "narration", "mode", "clues")})


def timing_advice(rep, typed: str = "") -> list:
    """Which runs still need a time typed into the box, worst first.

    The single most useful thing a pre-flight can say. "40% of shots are
    guesses" tells someone their video will be bad; "type a time for these
    four episodes and it will not be" tells them what to do about it, in the
    two minutes before they start a build rather than the two hours after.
    """
    from . import timings

    beats = getattr(rep, "beats", None) or []
    said = timings.from_script(beats) + timings.parse_lines(typed or "")
    out, seen = [], set()
    for shots, label, key in timings.unstated(beats, said):
        seen.add(label)
        out.append({"label": label, "shots": shots,
                    "example": f"{key} __:__-__:__",
                    "why": "koi timing nahi — ye sabse zaroori hai"})
    for _ratio, label, shots, room, wanted in timings.too_wide(beats, said):
        if label in seen:
            continue
        seen.add(label)
        out.append({"label": label, "shots": shots,
                    "example": f"{label.split()[-1]} — abhi {room/60:.0f} min",
                    "why": f"is run ko sirf {_seconds(wanted)} footage chahiye "
                           "— range scene jitni chhoti karo"})
    return out


def _seconds(value: float) -> str:
    """Screen time as something readable. `0 min` was neither."""
    if value < 90:
        return f"{value:.0f} sec"
    return f"{value/60:.1f} min"


def _placements(rep) -> list:
    """Where alignment will actually put every shot, worked out once.

    The pre-flight has to answer with the same numbers the build will
    produce. Anything else is a page telling somebody their video is fine
    and a log telling them it is not.
    """
    got = getattr(rep, "_places", None)
    if got is not None:
        return got
    beats = getattr(rep, "beats", None) or []
    try:
        got = align.align(rep.job.db, beats) if beats else []
    except Exception:                   # a pre-flight must never fail here
        got = []
    try:
        rep._places = got
    except AttributeError:
        pass
    return got


def learned_timings(rep) -> list:
    """Timings the pre-flight worked out from lines that really matched.

    The point of showing these is that they cost the person nothing. A run
    with a quoted line has already stated where it is, exactly; there is no
    reason to make somebody scrub a player for a number the tool is holding.
    """
    from . import timings

    beats = getattr(rep, "beats", None) or []
    if not beats:
        return []
    return [{"line": line, "shots": shots, "lines_matched": count}
            for shots, line, count in timings.derive(beats, _placements(rep))]


def evidence(rep, typed: str = "") -> dict:
    """How much of this video will rest on evidence, and how much on a guess.

    The number the Check panel used to lead with was `shots placeable`, and
    on a real script it read **98%** while the build that followed reported
    **60% usable**. Both were computed honestly and they measure different
    things: "placeable" means the episode is known, not that the moment is.
    A page saying 98% to somebody about to spend forty minutes rendering is
    the tool being cheerful at the wrong moment.

    This counts what actually decides the footage:

      * a quoted line the subtitles confirm, or a time somebody stated —
        those are exact
      * a shot laid between two of those in the same run — a good guess
      * everything else — the right episode and nothing more
    """
    from . import timings

    beats = getattr(rep, "beats", None) or []
    places = _placements(rep)
    if not beats or not places:
        return {}
    said = timings.from_script(beats) + timings.parse_lines(typed or "")
    stated = timings.windows_for(beats, said)
    firm = held = 0
    for p in places:
        if p.method == "anchor":
            firm += 1
        elif (p.beat, p.shot) in stated:
            held += 1
        elif p.ok:
            held += 1
    total = len(places)
    return {"exact": firm, "between": held, "loose": total - firm - held,
            "total": total,
            "percent": int(round((firm + held) * 100 / max(1, total)))}


def report_dict(rep, typed: str = "") -> dict:
    """A pre-flight as the Check panel draws it.

    The order is the order it is read in: the verdict, then what was
    checked, then the scenes that will be guesses — which is the one part
    someone can still do something about, by fixing the script.
    """
    weak = [r for r in rep.resolutions if r.status in ("weak", "no_query")]
    return {
        "verdict": rep.status,                  # READY | GAPS | BLOCKED
        "beats": len(rep.beats),
        "shots": rep.shots_total,
        "placeable": rep.placeable,
        "resolved": rep.shots_resolved,
        "percent": int(round(rep.placeable_fraction * 100)),
        "narration_seconds": round(rep.narration_seconds, 1),
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail,
                    "fatal": c.fatal} for c in rep.checks],
        "weak_scenes": sorted({r.beat for r in weak if getattr(r, "beat", None)}),
        "episodes": sorted({r.title for r in rep.requirements}),
        # Last, because it is the one line worth acting on.
        "needs_timing": timing_advice(rep, typed),
        "learned_timing": learned_timings(rep),
        "evidence": evidence(rep, typed),
        "clue_note": getattr(rep, "clue_note", ""),
    }


def look_at_folder(media_root: str) -> dict:
    """What a folder of episodes holds, before anything slow is started.

    Counting files and finding their subtitles takes seconds. Reading every
    frame takes hours. Someone should learn that two episodes have no
    subtitles in the first minute, not the fifth hour — because subtitles
    are the one thing the slow step cannot fix.
    """
    from . import naming, subtitles

    files = list(naming.walk_media(media_root))
    subbed, missing, bitmap = 0, [], []
    shows: dict = {}
    for path in files:
        mid = naming.parse(path)
        shows[mid.show] = shows.get(mid.show, 0) + 1
        try:
            kind, _sub, cues = subtitles.load_for_video(path)
        except Exception:
            kind, cues = "", []
        if cues:
            subbed += 1
        elif kind == "bitmap_only":
            bitmap.append(mid.label)
        else:
            missing.append(mid.label)
    guess = len(files) * 6.0                # ~6 minutes an episode, measured
    return {"root": os.path.abspath(media_root), "files": len(files),
            "subtitled": subbed, "missing_subs": sorted(missing)[:40],
            "bitmap_subs": sorted(bitmap)[:40],
            "shows": sorted(shows.items(), key=lambda kv: -kv[1])[:8],
            "minutes": int(guess)}


def index_title(runner: Runner, media_root: str, db: str,
                pictures: bool = True, force: bool = False) -> Task:
    """Scan a folder into the library, then read its frames.

    Two steps, in this order and never merged: the first is minutes and can
    fail in ways a person must fix, the second is hours and cannot start
    until the first succeeded.
    """
    from . import library as library_mod, lockfile, visual

    def work(task, log):
        # Said here rather than thrown from four frames down, because the
        # only thing a person can do about it is close the other window and
        # that sentence has to reach them intact.
        owner = lockfile.held_by(db)
        if owner:
            task.status = "blocked"
            task.stage = f"ye library abhi busy hai — {owner[0]}"
            log(f"is library par pehle se '{owner[0]}' chal raha hai "
                f"({owner[1] / 60:.0f} min pehle tak). Do kaam ek saath "
                "chalane se dono ruk jaate hain — pehle wale ko khatam hone "
                "do, ya us window ko band karo.")
            return
        task.stage = "reading subtitles"
        log(f"scanning {media_root}")
        res = library_mod.build(media_root, db, log=log)
        log(f"  {res.added} added {term.sym('dot')} {res.updated} updated "
            f"{term.sym('dot')} {res.skipped} unchanged")
        if res.no_subs:
            log(f"  {len(res.no_subs)} file(s) have no usable subtitles")
        if not pictures:
            task.stage = f"{res.added + res.updated + res.skipped} file(s) in "\
                         "the library"
            return

        from . import naming
        only = list(naming.walk_media(media_root))
        task.scenes_total = len(only)       # the bar counts episodes here
        task.stage = f"reading frames from {len(only)} file(s)"
        log(task.stage)
        seen = {"n": 0}

        def watch(*parts):
            text = " ".join(str(p) for p in parts)
            log(text)
            # visual.build names each file as it starts on it. Counting those
            # is what turns "this is slow" into "23 of 73".
            if "frames in" in text:
                seen["n"] += 1
                task.scenes_done = seen["n"]

        got = visual.build(db, only=only, force=force, log=watch)
        task.scenes_done = task.scenes_total
        done, total = visual.coverage(db)
        task.stage = f"{done} of {total} file(s) can be checked by picture"
        log(task.stage)
        if got.failed:
            log(f"  {len(got.failed)} file(s) could not be read")

    return runner.start("library", os.path.basename(media_root.rstrip("\\/")),
                        work)


def check(runner: Runner, spec: dict, db: str) -> Task:
    """Everything that can be known before a single frame is cut."""
    job = job_from(spec, db)

    def work(task, log):
        log(f"checking {job.name!r}")
        rep = jobs_mod.preflight(job, log=log)
        task.report = report_dict(rep, spec.get("timings") or "")
        task.out = job.out
        task.status = "blocked" if rep.status == "BLOCKED" else "done"
        task.stage = f"{rep.status} · {task.report['percent']}% shots placeable"
        log(task.stage)

    return runner.start("check", job.name, work)


def build(runner: Runner, spec: dict, db: str) -> Task:
    """Pre-flight, cut the footage, time it, and — if asked — render it.

    The same three steps as `9`, `T`, `R`, and in that order for the same
    reason: re-timing is seconds and re-cutting is an hour, so the timing is
    written as its own file rather than baked into the footage.
    """
    from . import narration, probe, render, runner as runner_mod, timeline

    job = job_from(spec, db)
    to_editor = (spec.get("after") or "editor") != "export"

    def work(task, log):
        task.out = job.out
        log(f"pre-flight for {job.name!r}")
        rep = jobs_mod.preflight(job, log=log)
        task.report = report_dict(rep, spec.get("timings") or "")
        if rep.status == "BLOCKED":
            task.status = "blocked"
            task.stage = "blocked — " + "; ".join(
                c.name for c in rep.failures() if c.fatal)
            log(task.stage)
            return
        task.scenes_total = len(rep.beats)
        task.stage = f"{len(rep.beats)} scenes — cutting footage"
        log(task.stage)

        result = runner_mod.run_job(job, rep, log=log)
        if result.status == "failed":
            task.status = "failed"
            task.error = result.error
            return
        task.scenes_done = task.scenes_total

        # --- timing -------------------------------------------------------
        task.stage = "timing it against the narration"
        log(task.stage)
        manifest = timeline.load_manifest(job.out)
        total, spans = 0.0, None
        if job.audio and os.path.isfile(job.audio):
            try:
                total = probe.probe(job.audio).duration
            except probe.ProbeError as exc:
                log(f"could not read the narration — {exc}")
            heard = narration.align_audio(
                rep.beats, job.audio, total_seconds=total,
                clean=narration.read_clean(job.extras.get("narration") or ""),
                log=log)
            log(heard.summary())
            if heard.ok:
                spans = heard.spans
        tl = timeline.plan(rep.beats, manifest, total_seconds=total,
                           pace=str(job.extras.get("pace") or "normal"),
                           spans=spans,
                           audio=job.audio if job.audio else "")
        timeline.write(tl, job.out)
        log(tl.summary())

        if to_editor:
            task.stage = "ready to edit"
            log(task.stage)
            return

        # --- render -------------------------------------------------------
        task.stage = "rendering"
        log(task.stage)
        res = render.render_folder(job.out, audio=job.audio, log=log)
        log(render.describe(res))
        if not res.ok:
            task.status = "failed"
            task.error = "; ".join(f"{w}: {why}" for w, why in res.failed[:3]) \
                or "the render produced no file"
            return
        task.video = res.path
        task.stage = f"done — {os.path.basename(res.path)}"

    return runner.start("build", job.name, work)
