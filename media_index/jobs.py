"""A queue of videos to build, and the pre-flight that gates it.

The point of this module is a single promise:

    Nothing starts rendering until every scene of that video has a resolved,
    verified asset waiting for it.

Queue 25 videos before bed and the failure you must never wake up to is
"video 8 stopped at 3 a.m. because one clip could not be found, so videos 9
through 25 never ran". So the queue does all of its *checking* first, for
every job, and only then does any *work* — and a job that cannot pass is
skipped rather than half-attempted.

A job file is JSON:

    {
      "defaults": {"db": "library.db", "clip_seconds": 4.0, "height": 1080},
      "jobs": [
        {"name": "Why Walter Broke Bad",
         "script": "scripts/walter.json",
         "audio":  "audio/walter.mp3",
         "out":    "output/walter"}
      ]
    }
"""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field

from . import align, sources, term
from .probe import ProbeError, ffmpeg_bin, probe
from .search import resolve_script

# Two thresholds, not one. The target is what a healthy job looks like; the
# floor is the point below which building would waste an hour of rendering.
# Between them the job still builds and the shortfall is reported — because a
# 50-scene video with two soft scenes is a video, and refusing to build it is
# the wrong answer for someone queueing twenty-five of them overnight.
DEFAULT_MIN_RESOLVED = 0.80     # below this -> GAPS
DEFAULT_HARD_FLOOR = 0.50       # below this -> BLOCKED
# A missing title blocks the job only when it costs this much of the script.
MISSING_SOURCE_BLOCK_SHARE = 0.30
# Free space demanded before a job starts, per minute of narration.
BYTES_PER_NARRATION_MINUTE = 300 * 1024 * 1024


@dataclass
class Job:
    name: str
    script: str
    audio: str = ""
    out: str = ""
    db: str = "library.db"
    clip_seconds: float = 4.0
    height: int | None = None
    min_resolved: float = DEFAULT_MIN_RESOLVED
    hard_floor: float = DEFAULT_HARD_FLOOR
    stills_per_scene: int = 1
    extras: dict = field(default_factory=dict)

    @property
    def slug(self) -> str:
        keep = "".join(c if c.isalnum() or c in "-_ " else "" for c in self.name)
        return "_".join(keep.split()) or "job"


def load_jobs(path: str) -> list[Job]:
    """Parse a job file. Defaults apply to every job unless overridden."""
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if isinstance(data, list):
        data = {"jobs": data}
    defaults = data.get("defaults") or {}
    base = os.path.dirname(os.path.abspath(path))

    def resolve(p):
        if not p:
            return ""
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))

    jobs = []
    for i, raw in enumerate(data.get("jobs") or [], 1):
        merged = {**defaults, **raw}
        known = {"name", "script", "audio", "out", "db", "clip_seconds",
                 "height", "min_resolved", "hard_floor", "stills_per_scene"}
        jobs.append(Job(
            name=merged.get("name") or f"video {i}",
            script=resolve(merged.get("script")),
            audio=resolve(merged.get("audio")),
            out=resolve(merged.get("out")) or resolve(f"output/video_{i}"),
            db=resolve(merged.get("db")) or "library.db",
            clip_seconds=float(merged.get("clip_seconds", 4.0)),
            height=merged.get("height"),
            min_resolved=float(merged.get("min_resolved", DEFAULT_MIN_RESOLVED)),
            hard_floor=float(merged.get("hard_floor", DEFAULT_HARD_FLOOR)),
            stills_per_scene=int(merged.get("stills_per_scene", 1)),
            extras={k: v for k, v in merged.items() if k not in known}))
    return jobs


# ---------------------------------------------------------------------------
# pre-flight
# ---------------------------------------------------------------------------

@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fatal: bool = True          # a failed non-fatal check only downgrades

    @property
    def icon(self) -> str:
        if self.ok:
            return term.sym("ok")
        return term.sym("fail") if self.fatal else term.sym("warn")


@dataclass
class JobReport:
    job: Job
    checks: list = field(default_factory=list)
    beats: list = field(default_factory=list)
    resolutions: list = field(default_factory=list)
    requirements: list = field(default_factory=list)
    narration_seconds: float = 0.0
    placeable: int = 0
    quotes: object = None            # align.QuoteReport, once it has been run
    clue_windows: dict = field(default_factory=dict)
    clue_note: str = ""              # what the clue script proved, in one line

    @property
    def blocked(self) -> bool:
        return any(not c.ok and c.fatal for c in self.checks)

    @property
    def has_gaps(self) -> bool:
        return any(not c.ok and not c.fatal for c in self.checks)

    @property
    def status(self) -> str:
        if self.blocked:
            return "BLOCKED"
        return "GAPS" if self.has_gaps else "READY"

    @property
    def icon(self) -> str:
        return {"READY": term.sym("ok"), "GAPS": term.sym("warn"),
                "BLOCKED": term.sym("fail")}[self.status]

    @property
    def shots_total(self) -> int:
        return len(self.resolutions)

    @property
    def shots_resolved(self) -> int:
        return sum(1 for r in self.resolutions
                   if r.status in ("resolved", "ambiguous"))

    @property
    def resolved_fraction(self) -> float:
        return (self.shots_resolved / self.shots_total) if self.shots_total else 0.0

    @property
    def placeable_fraction(self) -> float:
        """The share the builder can actually produce footage for.

        Not the same as the share that matched dialogue, and it is this one
        the gate must judge: a silent shot beside an anchored one is built
        from the scene's own chronology, and there is nothing provisional
        about the footage that comes out.
        """
        return (self.placeable / self.shots_total) if self.shots_total else 0.0

    def failures(self) -> list:
        return [c for c in self.checks if not c.ok]


SMART = {"“": '"', "”": '"', "„": '"', "‟": '"',
         "‘": "'", "’": "'", "‚": "'", "‛": "'",
         "«": '"', "»": '"', "′": "'", "″": '"'}


def straighten(text: str) -> str:
    """Turn typographic quotes into the ones JSON accepts.

    A chat model asked for JSON returns JSON. A chat model's *web page*
    returns typographic quotes, and copying out of one is how a real script
    arrived with 6,840 of them and would not parse at all — on line 3,
    character 4, with an error message about property names that says
    nothing about the actual cause.

    Only ever a repair attempt: the straightened text is parsed and used
    only if it parses. If a narration legitimately contains a quoted phrase,
    straightening breaks it, the parse fails, and the original error is
    reported exactly as before.
    """
    for bad, good in SMART.items():
        text = text.replace(bad, good)
    return text


def _escape_inner_quotes(text: str) -> str:
    """Escape straight double-quotes so curly ones can become the delimiters.

    A model's web page can hand back the worst of both worlds: the JSON
    string delimiters are typographic (curly) quotes, but a phrase quoted
    *inside* the text keeps ordinary straight quotes —

        "narration": "You said "good" and smiled"
                     ^curly       ^^straight^^  ^curly

    Straightening alone then turns the curly delimiters into straight quotes
    that collide with the untouched `"good"`, and the file still will not
    parse — on a real Joker script, every narration line broke this way.

    Escaping the straight quotes *first* keeps them as content once the curly
    quotes become the delimiters. Like `straighten`, this is only a repair
    attempt: the result is used only if it parses, so a file that was already
    valid JSON (straight delimiters, inner quotes already escaped) never
    reaches this path — it parses on the first, untouched try.
    """
    return re.sub(r'(?<!\\)"', r'\\"', text)


def _repairs(raw: str):
    """The raw text, then progressively bolder repairs of it.

    Yielded in order of least to most interference, so the first one that
    parses is the gentlest reading that works. A valid file parses at `raw`
    and no repair is ever applied to it.
    """
    yield raw
    straight = straighten(raw)
    if straight != raw:
        yield straight
    escaped = straighten(_escape_inner_quotes(raw))
    if escaped not in (raw, straight):
        yield escaped


def _documents(raw: str) -> tuple:
    """([every JSON value in the file], whatever prose followed them).

    The visual-script prompt asks for the beats as an array, then says
    "append one final JSON object" carrying the model's own summary, and
    then invites a line of plain English about which ranges are guesses. A
    model following all three instructions exactly produces a file that
    `json.loads` refuses:

        Extra data: line 2104 column 1 (char 80848)

    That character is the opening brace of the summary block. The file was
    correct, the instructions were correct, and the reader was the thing
    that was wrong — a 139-shot script the model had got right in every
    other respect could not be opened at all.

    So: read JSON values until something stops being JSON, and keep the
    remainder as text. The first document is what matters; everything after
    it is the model talking, and the tool should listen rather than choke.
    Only a file whose FIRST value will not parse is a broken file.
    """
    dec = json.JSONDecoder()
    out, at, n = [], 0, len(raw)
    while at < n:
        while at < n and raw[at].isspace():
            at += 1
        if at >= n:
            break
        try:
            value, at = dec.raw_decode(raw, at)
        except json.JSONDecodeError:
            if not out:
                raise                       # nothing parsed: a real failure
            return out, raw[at:].strip()
        out.append(value)
    return out, ""


def _beats_in(documents: list) -> list:
    """The beats, whichever of the documents is carrying them.

    Never the summary: that object has no `beats` key, so a file written the
    other way round — summary first — still finds the right one.
    """
    for doc in documents:
        if isinstance(doc, list):
            return doc
    for doc in documents:
        if isinstance(doc, dict) and doc.get("beats"):
            return doc["beats"]
    return []


def script_extras(path: str) -> tuple:
    """(the model's summary block, the note it wrote after the JSON).

    Worth surfacing rather than discarding. The summary is the model marking
    its own homework, and the marks are informative even when they are
    wrong. The note is better still — on the real script it read:

        "The S03E13 Gale-killing run and S04E08 cartel-pool flashback run
         are low confidence; both are late in their episodes but I don't
         know the exact minute. ... should be verified in a player before
         building."

    That is the model naming, unprompted, exactly which four numbers a
    person should check before starting a forty-minute build. Throwing it
    away to keep the parser tidy would be the worst trade in this file.
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            raw = f.read()
    except OSError:
        return {}, ""
    docs, note = [], ""
    for candidate in _repairs(raw):
        try:
            docs, note = _documents(candidate)
            break
        except (json.JSONDecodeError, ValueError):
            continue
    for doc in docs:
        if isinstance(doc, dict) and isinstance(doc.get("summary"), dict):
            return doc["summary"], note
    return {}, note


def read_beats(path: str) -> list:
    with open(path, "r", encoding="utf-8-sig") as f:
        raw = f.read()
    first = None
    for candidate in _repairs(raw):
        try:
            return _beats_in(_documents(candidate)[0])
        except json.JSONDecodeError as err:
            if first is None:
                first = err                  # the untouched file's real fault
    raise first


def _apply_clues(job: Job, rep: JobReport, add, log) -> None:
    """Let the optional third script fill in what it can prove.

    A clue script contributes quoted lines and bracketed windows to a visual
    script that had neither. It is checked line by line against the local
    subtitles inside `clues.enrich`, so what lands on `rep.beats` is
    evidence; the recollection is left at the door.

    Nothing here may stop a build. A clue script is a third file supplied by
    hand, and the worst it is allowed to do is contribute nothing — the
    build that follows is exactly the build that would have run without it.
    """
    path = (job.extras.get("clues") or "").strip()
    if not path:
        return
    from . import clues as clues_mod                     # noqa: PLC0415

    if not os.path.isfile(path):
        add(Check("clue script", False, f"not found: {path}", fatal=False))
        return
    try:
        found = clues_mod.read(path)
        got = clues_mod.enrich(job.db, rep.beats, found, log=log)
    except clues_mod.ClueError as exc:
        add(Check("clue script", False, str(exc)[:160], fatal=False))
        return
    except Exception as exc:                             # never fatal, ever
        add(Check("clue script", False, f"could not be applied: {exc}"[:160],
                  fatal=False))
        return

    rep.clue_windows = got.windows
    rep.clue_note = got.summary().strip()
    # `ok` is about whether the clues were *useful*, not whether the file
    # was valid — a clue script none of whose lines exist in the subtitles
    # parsed perfectly and is worth nothing, and saying so is the point.
    add(Check("clue script", got.lines_found > 0,
              f"{len(found)} clue {chr(183)} {got.lines_found}/"
              f"{got.lines_checked} line subtitle me mili {chr(183)} "
              f"{got.quotes_added} shot ko asli quote mila",
              fatal=False))


def preflight(job: Job, log=lambda *a: None) -> JobReport:
    """Everything that can be verified without rendering a single frame."""
    rep = JobReport(job=job)
    add = rep.checks.append

    add(Check("ffmpeg available", ffmpeg_bin() is not None,
              "install ffmpeg and put it on PATH" if not ffmpeg_bin() else ""))

    # --- script ---
    if not job.script or not os.path.isfile(job.script):
        add(Check("script file", False, f"not found: {job.script}"))
        return rep
    try:
        rep.beats = read_beats(job.script)
        add(Check("script parses", True, f"{len(rep.beats)} beats"))
    except (json.JSONDecodeError, OSError) as exc:
        add(Check("script parses", False, str(exc)[:160]))
        return rep
    if not rep.beats:
        add(Check("script has beats", False, "the script contains no beats"))
        return rep

    # --- library ---
    if not os.path.isfile(job.db):
        add(Check("library index", False, f"not found: {job.db} — run 'build' first"))
        return rep
    add(Check("library index", True, os.path.basename(job.db)))

    # --- clue script ---
    # Applied here rather than at build time, and that is deliberate. The
    # Check panel saying "6% exact" and the build then reporting 40% was a
    # real complaint about this tool, caused by the two counting different
    # things. Everything the clues can prove is proved now, so both numbers
    # describe the same script.
    _apply_clues(job, rep, add, log)

    # --- narration audio ---
    if job.audio:
        if not os.path.isfile(job.audio):
            add(Check("narration audio", False, f"not found: {job.audio}"))
        else:
            try:
                info = probe(job.audio)
                rep.narration_seconds = info.duration
                ok = info.duration > 1.0
                add(Check("narration audio", ok,
                          f"{info.duration/60:.1f} min" if ok
                          else "file is unreadable or empty"))
            except ProbeError as exc:
                add(Check("narration audio", False, str(exc)[:160]))
    else:
        add(Check("narration audio", True, "none supplied (clips only)",
                  fatal=False))

    # --- sources ---
    rep.requirements = sources.check(job.db, rep.beats)
    missing = [r for r in rep.requirements if r.status == "missing"]
    partial = [r for r in rep.requirements
               if r.status in ("partial", "no_text_subs")]
    total_shots = sum(r.shots for r in rep.requirements) or 1
    lost_share = sum(r.shots for r in missing) / total_shots
    if missing:
        # One missing title out of five costs a few shots, not the video.
        add(Check("sources in library", False,
                  f"missing: {', '.join(r.title for r in missing)} "
                  f"({lost_share:.0%} of shots)",
                  fatal=lost_share >= MISSING_SOURCE_BLOCK_SHARE))
    else:
        add(Check("sources in library", True, f"{len(rep.requirements)} title(s)"))
    if partial:
        add(Check("sources complete", False,
                  "; ".join(f"{r.title}: {r.note}" for r in partial), fatal=False))

    # --- every shot ---
    rep.resolutions = resolve_script(job.db, rep.beats)
    try:
        rep.placeable, _total = align.placeable(job.db, rep.beats)
    except Exception as exc:                    # a gate must never crash
        rep.placeable = rep.shots_resolved
        add(Check("alignment", False, str(exc)[:160], fatal=False))
    frac = rep.placeable_fraction
    detail = (f"{rep.placeable}/{rep.shots_total} ({frac:.0%}, "
              f"target {job.min_resolved:.0%}, floor {job.hard_floor:.0%})")
    if frac < job.hard_floor:
        add(Check("shots placeable", False, detail + " — too little to build"))
    elif frac < job.min_resolved:
        add(Check("shots placeable", False,
                  detail + " — will build with gaps", fatal=False))
    else:
        add(Check("shots placeable", True, detail))
    if rep.placeable > rep.shots_resolved:
        add(Check("how they were placed", True,
                  f"{rep.shots_resolved} on a quoted line, "
                  f"{rep.placeable - rep.shots_resolved} along the scene"))
    weak = [r for r in rep.resolutions if r.status in ("weak", "no_query")]
    if weak:
        add(Check("all shots exact", False,
                  f"{len(weak)} shot(s) need a visual check", fatal=False))

    # The script's own summary counts its verbatim lines. This counts the
    # ones the subtitles actually contain, which is a different number and
    # the only one worth acting on — and it is worth acting on HERE, while
    # the script can still be sent back and fixed for the price of a retry.
    try:
        rep.quotes = align.quote_report(job.db, rep.beats)
        good = rep.quotes.rate >= 0.6 and not rep.quotes.runs_without_anchor
        add(Check("quoted lines are real", good, rep.quotes.detail(),
                  fatal=False))
    except Exception as exc:                    # a gate must never crash
        add(Check("quoted lines are real", False, str(exc)[:160], fatal=False))

    # --- output location and disk ---
    try:
        os.makedirs(job.out, exist_ok=True)
        probe_file = os.path.join(job.out, ".write_test")
        with open(probe_file, "w") as f:
            f.write("ok")
        os.remove(probe_file)
        add(Check("output writable", True, job.out))
    except OSError as exc:
        add(Check("output writable", False, str(exc)[:160]))
        return rep

    need = max(1.0, rep.narration_seconds / 60.0) * BYTES_PER_NARRATION_MINUTE
    try:
        free = shutil.disk_usage(job.out).free
        add(Check("disk space", free > need,
                  f"{free/1e9:.1f} GB free, ~{need/1e9:.1f} GB needed"))
    except OSError:
        add(Check("disk space", True, "could not be measured", fatal=False))

    return rep


def preflight_all(jobs: list[Job], log=lambda *a: None) -> list[JobReport]:
    reports = []
    for i, job in enumerate(jobs, 1):
        log(f"[{i}/{len(jobs)}] checking {job.name!r}…")
        try:
            reports.append(preflight(job, log))
        except Exception as exc:                     # a check must never crash
            rep = JobReport(job=job)
            rep.checks.append(Check("pre-flight", False, f"{type(exc).__name__}: {exc}"))
            reports.append(rep)
    return reports


def format_reports(reports: list[JobReport]) -> str:
    lines = ["", "PRE-FLIGHT", ""]
    width = max((len(r.job.name) for r in reports), default=10)
    for i, r in enumerate(reports, 1):
        detail = (f"{r.shots_resolved}/{r.shots_total} shots"
                  if r.shots_total else "not checked")
        lines.append(f"  {r.icon} {i:>2}. {r.job.name:<{width}}  "
                     f"{r.status:<8} {detail}")
        for c in r.failures():
            lines.append(f"          {c.icon} {c.name}: {c.detail}")
    ready = sum(1 for r in reports if r.status == "READY")
    gaps = sum(1 for r in reports if r.status == "GAPS")
    blocked = sum(1 for r in reports if r.status == "BLOCKED")
    lines += ["", f"  {ready} ready {term.sym('dot')} {gaps} with gaps "
            f"{term.sym('dot')} {blocked} blocked"]
    if blocked:
        lines.append("  blocked jobs will be skipped, not attempted")
    return "\n".join(lines)
