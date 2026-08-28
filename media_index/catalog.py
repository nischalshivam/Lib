"""Turn a whole film or series into a searchable, tagged shot library.

This is the engine a friend's "index the entire series once, then every video
reuses it" workflow is built on, and it is the piece this tool was missing.
The idea is not clever, it is just thorough: watch the source once, break it
into short shots, and have a vision model write down what each shot *is* — who
is on screen, what happens, the kind of shot, whether it is clean enough to
use. Store that beside the exact dialogue timing already pulled from the
subtitles. The result is a catalogue that can be searched by meaning, so the
edit step can ask "a close-up of Arthur alone, dim, no on-screen text" and get
real answers instead of guessing between two spoken lines.

## Why a catalogue and not just embeddings

The tool already stores a CLIP-style vector per frame, and that vector is a
good *tie-breaker* but a weak *finder*: on a silent scene the right frame does
not clearly beat the noise floor. A one-line description written by a vision
model — "a thin man in clown make-up dances slowly, alone, arms out, dim
bathroom" — is a far stronger match for a narration beat about that moment
than any embedding, because it is language matched against language.

## What is deliberately NOT here

No scraping, no YouTube, no third-party upload. The source is the local file
the user already owns; the catalogue sits next to it on disk. The model is a
labeller, not an oracle — it is shown real frames and asked to describe what
it sees, never asked "where in this film is X". Its character labels are a
claim to be *verified* later (a second pass, `cast.py`), exactly as a friend's
brief describes: "before a character's footage is used, a second check
confirms the person is actually in the shot."

## Testability

Segmentation is a pure function of cut points and duration. The build loop
takes an injectable frame-grabber and an injectable `ask` (the model call),
so the whole pipeline runs in a unit test with neither ffmpeg nor a network.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field, asdict

# A shot shorter than this is merged into its neighbour (a 4-frame flash is not
# a usable clip); one longer than this is split (a 40-second static talk is
# several beats' worth of footage, not one).
MIN_SHOT_S = 1.5
MAX_SHOT_S = 8.0
TARGET_SHOT_S = 5.0

# Frames shown to the model per shot. Enough to see how a shot moves without
# paying to describe near-identical stills. Extracting each frame from a heavy
# x265/10-bit source (GoT) costs ~1.7s, done serially, and IS the build's real
# bottleneck — so this is the biggest speed dial. 2 clean frames still describe a
# shot well (identity comes from the face in-frame, dialogue from the subtitles,
# neither of which needs 4). Tune with FRAMES_PER_SHOT.
try:
    FRAMES_PER_SHOT = max(1, int(os.environ.get("FRAMES_PER_SHOT", "4")))
except ValueError:
    FRAMES_PER_SHOT = 4

# Reference photos per character shown on EVERY shot's labelling call. This is
# the dominant cost of cataloguing with a large cast: at three photos a 28-
# character cast uploaded 84 reference images per shot. One or two clear faces
# is enough to recognise the main cast; the precise wrong-person rejection
# still happens at build time (gemini.confirm_shot) with more photos. Lower =
# faster and cheaper library builds. Set to 1 for maximum speed.
CATALOG_REF_PHOTOS = 1

# How many shots to describe at once. Each call waits ~15s on the model, so the
# work is almost all network idle — firing several at once is what turns a
# ~3-hour episode into ~20 minutes. Frame grabbing stays serialised (see
# build_catalog) so the USB source is never read in parallel; only the model
# calls overlap — so raising this is pure speed: it never touches the SSD and
# never changes what any shot is described with, so accuracy and per-shot cost
# are identical. Bounded only by the Gemini rate limit. Tune with CATALOG_WORKERS.
try:
    CATALOG_WORKERS = max(1, int(os.environ.get("CATALOG_WORKERS", "6")))
except ValueError:
    CATALOG_WORKERS = 6

# Save the library every N described shots instead of every single one. The old
# every-shot save rewrote the whole (growing) JSON to disk each time — hundreds
# of full rewrites per episode. A crash now redescribes at most this many shots.
SAVE_EVERY = 20

# ffmpeg's scene score above which a frame is treated as a new shot. 0.4 is the
# usual middle ground — lower floods on lighting flicker, higher misses soft
# cuts. Overridable per call.
SCENE_THRESHOLD = 0.40


@dataclass
class Shot:
    """One catalogued shot. Mirrors the course's library.json entry."""
    id: str
    source: str
    file: str
    start: float
    end: float
    description: str = ""
    tags: list = field(default_factory=list)
    characters: list = field(default_factory=list)
    action: str = ""
    shot_type: str = ""
    quality: str = ""            # high | mid | low
    safe: bool = True            # False = burned-in text/caption/graphic
    explicit: bool = False       # True = nudity / sex / graphic — never place it
    dialogue: str = ""

    @property
    def dur(self) -> float:
        return round(self.end - self.start, 2)


# ---------------------------------------------------------------------------
# segmentation — pure, so it is tested without a video
# ---------------------------------------------------------------------------

def _slug(name: str) -> str:
    base = os.path.splitext(os.path.basename(name))[0]
    return re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_") or "src"


def shots_from_cuts(cuts: list, duration: float,
                    min_s: float = MIN_SHOT_S, max_s: float = MAX_SHOT_S,
                    target_s: float = TARGET_SHOT_S) -> list:
    """(start, end) windows from a sorted list of cut times and a duration.

    A cut list of [12.0, 47.0] over a 60s clip is three raw shots — 0-12,
    12-47, 47-60. The 35-second middle one is longer than a shot should be, so
    it is split into ~target-length pieces; a sub-`min_s` sliver is folded into
    the shot before it rather than shipped as its own entry.
    """
    if duration <= 0:
        return []
    points = [0.0] + sorted(t for t in cuts if 0.0 < t < duration) + [duration]
    raw = []
    for a, b in zip(points, points[1:]):
        if b - a <= 0:
            continue
        if raw and (a - raw[-1][0]) < 1e-6:      # duplicate cut
            continue
        raw.append((a, b))

    out = []
    for a, b in raw:
        span = b - a
        if span <= max_s:
            out.append((a, b))
            continue
        # split a long take into roughly target-length pieces
        n = max(1, round(span / target_s))
        step = span / n
        for k in range(n):
            out.append((a + k * step, a + (k + 1) * step if k < n - 1 else b))

    # fold slivers into the previous shot
    merged = []
    for a, b in out:
        if merged and (b - a) < min_s:
            pa, _pb = merged[-1]
            merged[-1] = (pa, b)
        else:
            merged.append((a, b))
    return [(round(a, 3), round(b, 3)) for a, b in merged]


def fixed_windows(duration: float, win_s: float = TARGET_SHOT_S) -> list:
    """Even windows across the whole file — the fallback when cut detection is
    unavailable. Deterministic, and good enough to catalogue from."""
    if duration <= 0:
        return []
    out, t = [], 0.0
    while t < duration - 1e-6:
        out.append((round(t, 3), round(min(t + win_s, duration), 3)))
        t += win_s
    return out


def _keyframe_cuts(path: str, timeout: int = 600) -> list:
    """Shot boundaries approximated by the video's KEYFRAME timestamps, read
    straight from the packet headers — NO decoding at all.

    Full scene detection decodes every frame, which on a long 1080p x265/10-bit
    file (Game of Thrones) is ~15-20 min PER episode and dominated by decode.
    Encoders insert a keyframe at hard scene cuts, so keyframe times track the
    real cuts closely (~90% on our GoT sample) and come out in seconds. The
    catalogue only needs shots roughly segmented for describing; the PRECISE cut
    for a delivered clip is re-detected by makevideo on a tiny window later, so
    final-video accuracy is unchanged. Opt in with MEDIA_CUTS=keyframe.
    """
    import subprocess
    from .probe import ffprobe_bin
    try:
        exe = ffprobe_bin()
    except Exception:
        return []
    cmd = [exe, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", path]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return []
    cuts = []
    for line in (r.stdout or b"").decode("utf-8", "replace").splitlines():
        parts = line.strip().split(",")
        if len(parts) >= 2 and "K" in parts[1]:          # keyframe flag
            try:
                cuts.append(float(parts[0]))
            except ValueError:
                pass
    return sorted(set(cuts))


def detect_cuts(path: str, threshold: float = SCENE_THRESHOLD,
                timeout: int = 1800) -> list:
    """Cut timestamps from ffmpeg scene detection. [] if ffmpeg cannot run.

    One pass, reading only the scene scores ffmpeg prints — no video is
    decoded to disk. A failure here is never fatal: the caller falls back to
    fixed windows, which still produces a usable catalogue.

    MEDIA_CUTS=keyframe uses the fast, decode-free keyframe approximation
    (see `_keyframe_cuts`) — for heavy x265 libraries. 'auto' tries keyframes
    and falls back to scene detection if the file has none.
    """
    import subprocess
    from .probe import ffmpeg_bin
    mode = os.environ.get("MEDIA_CUTS", "scene").lower()
    if mode in ("keyframe", "auto"):
        kc = _keyframe_cuts(path)
        if kc or mode == "keyframe":
            return kc
    try:
        exe = ffmpeg_bin()
    except Exception:
        return []
    cmd = [exe, "-v", "info", "-i", path, "-filter_complex",
           f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return []
    text = (r.stderr or b"").decode("utf-8", "replace")
    cuts = []
    for m in re.finditer(r"pts_time:(\d+(?:\.\d+)?)", text):
        cuts.append(float(m.group(1)))
    return sorted(set(cuts))


def dialogue_for(cues: list, start: float, end: float) -> str:
    """Subtitle text that overlaps a shot window, joined in order.

    A shot's spoken line is the strongest label it can carry, so it is stored
    right next to the model's description — the same shot is then findable by
    what was said in it and by what it looked like.
    """
    parts = []
    for c in cues or []:
        cs, ce = c.start_ms / 1000.0, c.end_ms / 1000.0
        if ce > start and cs < end:           # any overlap
            t = (c.text or "").strip()
            if t:
                parts.append(t)
    return " ".join(parts)[:400]


# ---------------------------------------------------------------------------
# the model prompt and its answer
# ---------------------------------------------------------------------------

def tag_messages(frames: list, known_characters: list | None = None,
                 dialogue: str = "", refs: dict | None = None) -> list:
    """Messages asking the model to DESCRIBE a shot from its frames.

    Framed as description, never location: the model is shown real frames and
    asked what is in them.

    ## Why references belong HERE, at catalogue time

    Without reference photos the model has no way to know a minor character —
    it can name Walter White from memory but not Victor, so every silent shot
    of Victor lands with `characters: []`. Retrieval then filters by character
    and those blank shots earn no bonus, so the right footage never surfaces
    and no amount of later verification can rescue a candidate pool it was
    never in. That is the whole "the character's clip never comes up" failure.

    `refs` is {name: [photo_bytes, ...]}. Shown FIRST, labelled by name, they
    turn "who is this?" (a guess the model is told to refuse) into "which of
    these known people is this, if any?" (a comparison it can actually make).
    The catalogue's character labels become reliable at the source, which is
    the one place that fixes retrieval for every future video. A visible person
    who matches no reference is still `unknown` — the honest-blank rule holds,
    it just no longer swallows the main cast.
    """
    from .gemini import _data_uri, _img_uri
    refs = refs or {}
    known = ", ".join(known_characters) if known_characters else ""
    if refs:
        char_rule = (
            "- characters: reference photos of the main cast are shown first, "
            "labelled by name. For each person visible in the shot, if they are "
            "clearly the SAME person as one of the references, use that "
            "reference's exact name. A visible person who matches no reference, "
            "or whom you cannot identify with confidence, is \"unknown\". Never "
            "guess a name and never force a reference onto a different person.")
    else:
        char_rule = (
            "- characters: only people you can actually see and recognise. If "
            "you are not sure who someone is, use \"unknown\". Never guess a "
            "name.")
    rules = (
        "You label footage for a searchable clip library. You are shown a few "
        "frames sampled from ONE short shot, in order. Describe only what is "
        "visibly there.\n\n"
        "Answer ONLY with strict JSON:\n"
        '{"description": "<one vivid sentence: who + what + setting>", '
        '"tags": ["<lowercase keyword>", ...], '
        '"characters": ["<named person actually visible>", ...], '
        '"action": "<what happens, few words>", '
        '"shot_type": "<wide|medium|close-up|extreme close-up|insert|aerial>", '
        '"quality": "<high|mid|low>", '
        '"safe": <true|false>, '
        '"explicit": <true|false>}\n\n'
        "Rules:\n"
        f"{char_rule}\n"
        "- safe=false if the shot has burned-in subtitles, captions, or large "
        "on-screen text/graphics; otherwise true.\n"
        "- explicit=true ONLY for adult content that a family/monetised YouTube "
        "video must never show: visible nudity, a sex scene, or graphic sexual "
        "activity. Ordinary kissing, violence, blood or a shirtless man is NOT "
        "explicit. When in doubt, false.\n"
        "- quality: low if blurry, dark to the point of unreadable, or a "
        "transition/black frame.\n"
        "- tags: 4-8 concrete keywords (mood, setting, objects, action)."
    )
    content = []
    # Reference photos first, so the model has the faces in hand before it sees
    # the shot it must name them in. ONE clear face per character is enough for
    # the "same person?" comparison here, and it is decisive for speed/cost: a
    # 28-character cast at three photos each meant 84 reference images uploaded
    # on EVERY shot's call (thousands per episode). The precise identity check
    # that actually rejects a wrong person still runs at build time
    # (`gemini.confirm_shot`) with more photos — this labelling pass does not
    # need them, so it stays lean.
    for name, imgs in refs.items():
        for photo in imgs[:CATALOG_REF_PHOTOS]:
            content.append({"type": "text", "text": f"Reference — {name}:"})
            content.append({"type": "image_url",
                            "image_url": {"url": _img_uri(photo)}})
    ask = "Describe this shot."
    if refs:
        ask += ("\nName any visible person ONLY by matching the reference "
                "photos above; anyone unmatched is \"unknown\".")
    elif known:
        ask += f"\nKnown characters in this title (use these spellings if you " \
               f"see them): {known}"
    if dialogue:
        ask += f'\nLine spoken during this shot (context only): "{dialogue}"'
    content.append({"type": "text", "text": ask})
    for i, jpeg in enumerate(frames, 1):
        content.append({"type": "text", "text": f"Shot frame {i}:"})
        content.append({"type": "image_url",
                        "image_url": {"url": _data_uri(jpeg)}})
    return [{"role": "system", "content": rules},
            {"role": "user", "content": content}]


def canonicalize(names: list, canon: dict) -> list:
    """Collapse the model's varied character labels to canonical names.

    Gemini calls the same person "Joaquin Phoenix", "Joker", and "Arthur
    Fleck" across three shots — an actor name, a persona, a full name. For
    search to work, one person must have one name. `canon` maps any known
    alias (lowercased) to the canonical label; an unmapped name is kept as-is
    (it might be a real minor character), and duplicates are removed in order.
    """
    out, seen = [], set()
    for raw in names:
        # Trailing punctuation ("Walt Jr." vs "Walt Jr") must not split one
        # person into two catalogue entries, so it is stripped for the lookup.
        key = re.sub(r"\s+", " ", str(raw).strip().lower()).strip(" .,-'\"")
        name = canon.get(key, raw)
        if name.lower() not in seen:
            out.append(name)
            seen.add(name.lower())
    return out


def alias_map(people: list) -> dict:
    """{alias_lower: canonical} for a list of 'Canonical = alias, alias' lines
    or plain names. `Arthur = Arthur Fleck, Joker, Joaquin Phoenix` teaches
    the collapse; a bare `Murray` maps only itself."""
    canon: dict = {}
    for line in people or []:
        line = str(line).strip()
        if not line:
            continue
        if "=" in line:
            name, aliases = line.split("=", 1)
            name = name.strip()
            parts = [name] + [a.strip() for a in aliases.split(",")]
        else:
            name, parts = line, [line]
        for a in parts:
            if a:
                canon[a.lower()] = name
    return canon


def list_entries(v) -> list:
    """A field that may be a real list, a stringified list, or a joined string,
    read back as a list. Visual scripts have carried `characters` all three
    ways — `["Arthur"]`, `"['Arthur', 'Murray']"`, `"Arthur, Murray"`."""
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    text = str(v or "").strip()
    if not text:
        return []
    if text[:1] in "[(" and text[-1:] in ")]":
        import ast
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, (list, tuple)):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except (ValueError, SyntaxError):
            pass
    return [p.strip() for p in re.split(r"[,;]", text) if p.strip()]


def parse_tags(text: str) -> dict:
    """The model's JSON, made safe. Tolerant of fences and stray prose."""
    raw = (text or "").strip()
    a, b = raw.find("{"), raw.rfind("}")
    if a < 0 or b <= a:
        return {}
    try:
        obj = json.loads(raw[a:b + 1])
    except (ValueError, TypeError):
        return {}
    if not isinstance(obj, dict):
        return {}

    def as_list(v):
        return list_entries(v)

    chars = [c for c in as_list(obj.get("characters"))
             if c.lower() not in ("unknown", "none", "n/a", "")]
    return {
        "description": str(obj.get("description") or "").strip()[:400],
        "tags": [t.lower() for t in as_list(obj.get("tags"))][:12],
        "characters": chars[:8],
        "action": str(obj.get("action") or "").strip()[:120],
        "shot_type": str(obj.get("shot_type") or "").strip().lower()[:40],
        "quality": (str(obj.get("quality") or "").strip().lower() or "mid"),
        "safe": bool(obj.get("safe", True)),
        "explicit": bool(obj.get("explicit", False)),
    }


# ---------------------------------------------------------------------------
# the library file
# ---------------------------------------------------------------------------

def load_library(path: str) -> dict:
    """{id: Shot}. A missing or unreadable file is an empty library.

    `path` may be a single catalog.json OR a folder — a whole series is many
    per-episode catalogues, and retrieval has to see all of them at once, so a
    folder loads and merges every `*.catalog.json` under it.
    """
    if os.path.isdir(path):
        return load_libraries(sorted(
            os.path.join(r, f)
            for r, _d, files in os.walk(path)
            for f in files if f.lower().endswith(".catalog.json")))
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    from . import paths as _paths                          # noqa: PLC0415
    out = {}
    for row in data.get("shots", []) if isinstance(data, dict) else []:
        try:
            shot = Shot(**{k: row.get(k) for k in
                           Shot.__dataclass_fields__ if k in row})
        except (KeyError, TypeError):
            continue
        # Remap the stored path onto whatever letter the SSD is mounted on now,
        # so a catalogue built on E: keeps working after it becomes F:/G:/...
        if shot.file:
            shot.file = _paths.resolve(shot.file)
        out[row["id"]] = shot
    return out


def load_libraries(paths: list) -> dict:
    """Merge many catalogues into one {id: Shot}. Shot ids carry a per-file
    slug, so episodes never collide."""
    merged = {}
    for p in paths:
        merged.update(load_library(p))
    return merged


def save_library(path: str, shots: dict, windows: int | None = None,
                 complete: bool = False) -> None:
    """Write the whole catalogue, sorted by source then time. Atomic-ish.

    `windows` (the episode's total shot count from cut detection) and
    `complete` are stamped so a later run can tell a finished episode from a
    half-done one WITHOUT re-reading the whole video for cut detection — the
    slow part that otherwise makes every restart re-scan finished episodes.
    """
    rows = sorted((asdict(s) for s in shots.values()),
                  key=lambda r: (r["source"], r["start"]))
    payload = {"shots": rows, "count": len(rows)}
    if windows is not None:
        payload["windows"] = windows
    if complete:
        payload["complete"] = True
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def plan_shots(duration: float, path: str = "") -> list:
    """The (start, end) windows to catalogue: real cuts if we can, else even
    windows. Kept separate so a caller can preview the count before paying to
    tag anything."""
    cuts = detect_cuts(path) if path else []
    windows = shots_from_cuts(cuts, duration) if cuts else fixed_windows(duration)
    return windows


def _all_but_a_few(library: dict, total: int) -> bool:
    """Whether an episode is done enough to stamp 'complete': all but a small
    tail of shots carry a description. A BLOCK of blanks — a mid-build USB drop
    or a rate-limit burst — leaves it below this and unstamped, so a re-run
    retries those shots instead of the flag freezing the gap in place. Only a
    genuinely undescribable handful (<2%) is tolerated as finished."""
    if total <= 0:
        return False
    described = sum(1 for s in library.values() if getattr(s, "description", ""))
    return described >= 0.98 * total


def build_catalog(source: str, file: str, duration: float, out_json: str,
                  grab, ask, cues: list | None = None,
                  known_characters: list | None = None,
                  canon: dict | None = None,
                  windows: list | None = None, log=lambda *a: None,
                  resume: bool = True, refs: dict | None = None) -> dict:
    """Catalogue one video into `out_json`. Returns {id: Shot}.

    `grab(start, end) -> [jpeg_bytes, ...]` samples representative frames of a
    window; `ask(messages) -> text` is the model call. Both injected so this
    runs under test with neither ffmpeg nor a network. Saved after every shot,
    so a run interrupted at shot 900 of 1500 resumes there — no frame is
    described twice, and nothing is lost to a crash. `canon` collapses the
    model's varied character labels (actor/persona/name) to one name each.
    `refs` ({name: [photo_bytes]}) are shown to the model with every shot so it
    identifies the cast against real faces instead of guessing — the single
    fix that makes the catalogue's character labels trustworthy.
    """
    library = load_library(out_json) if resume else {}
    slug = _slug(file or source)
    canon = canon or {}
    # Backfill cheap fields on shots already catalogued by an earlier run,
    # without re-describing a single frame. Two things drift onto old shots:
    # a name map supplied later (Joaquin Phoenix -> Arthur), and dialogue that
    # was missing because the subtitle had not been found on the first pass.
    # Both are pure functions of data we already have, so a resume fixes them
    # for free instead of leaving the first run's gaps frozen in the JSON.
    changed = False
    for shot in library.values():
        if canon and shot.characters:
            fixed = canonicalize(shot.characters, canon)
            if fixed != shot.characters:
                shot.characters = fixed
                changed = True
        if cues and not shot.dialogue:
            line = dialogue_for(cues, shot.start, shot.end)
            if line:
                shot.dialogue = line
                changed = True
    if changed:
        save_library(out_json, library)
    windows = windows if windows is not None else plan_shots(duration, file)
    total = len(windows)
    todo = [(i, start, end) for i, (start, end) in enumerate(windows)
            if not (f"{slug}__{i:05d}" in library
                    and library[f"{slug}__{i:05d}"].description)]
    done = total - len(todo)
    if not todo:
        save_library(out_json, library, windows=total, complete=True)
        return library

    # Describe shots CONCURRENTLY. Each call spends ~15s waiting on the model,
    # so running several at once collapses a ~3-hour episode to ~20 minutes.
    # Frame grabbing is serialised behind a lock — the source often lives on a
    # single USB disk that drops under parallel reads — so only the network-
    # bound model calls overlap, never the disk reads.
    import threading
    from concurrent.futures import ThreadPoolExecutor
    grab_lock = threading.Lock()

    def _describe(job):
        i, start, end = job
        shot_id = f"{slug}__{i:05d}"
        line = dialogue_for(cues, start, end)
        with grab_lock:                          # one USB read at a time
            try:
                frames = grab(start, end)
            except Exception as exc:             # a bad window never dies a run
                log(f"      shot {i} grab failed: {exc}")
                frames = []
        tags = {}
        if frames:
            try:
                tags = parse_tags(ask(tag_messages(
                    frames, known_characters, line, refs=refs)))
            except Exception as exc:
                log(f"      shot {i} tag failed: {exc}")
                tags = {}
        return shot_id, start, end, line, tags

    # Results are consumed in the main thread, so the library dict is only ever
    # mutated by one thread; the workers just wait on the network.
    with ThreadPoolExecutor(max_workers=CATALOG_WORKERS) as pool:
        for shot_id, start, end, line, tags in pool.map(_describe, todo):
            if tags.get("characters"):
                tags["characters"] = canonicalize(tags["characters"], canon)
            library[shot_id] = Shot(
                id=shot_id, source=source, file=file, start=start, end=end,
                dialogue=line, **{k: tags[k] for k in
                                  ("description", "tags", "characters", "action",
                                   "shot_type", "quality", "safe") if k in tags})
            done += 1
            if done % SAVE_EVERY == 0:
                save_library(out_json, library, windows=total,
                             complete=_all_but_a_few(library, total))
            if done % 25 == 0 or done == total:
                log(f"      catalogued {done}/{total} shots")
    save_library(out_json, library, windows=total,
                 complete=_all_but_a_few(library, total))
    return library


# ---------------------------------------------------------------------------
# the real wiring — ffmpeg frames and the Gemini call
# ---------------------------------------------------------------------------

def real_grab(path: str, n: int = FRAMES_PER_SHOT, width: int = 768):
    """A frame-grabber over a real file: the best `n` distinct frames of a
    window, as JPEG bytes. Reuses the same sharpness/dedupe scoring the still
    picker uses, so a shot is described by clean frames, not blurred ones."""
    from .cutter import extract_frame
    from . import frames as frames_mod

    def grab(start: float, end: float) -> list:
        try:
            best = frames_mod.pick(frames_mod.scan(path, start, end), n)
        except Exception:
            best = []
        times = [c.time for c in best] or [(start + end) / 2.0]
        out = []
        for t in times:
            fd, tmp = tempfile.mkstemp(suffix=".jpg")
            os.close(fd)
            try:
                extract_frame(path, t, tmp, width=width)
                with open(tmp, "rb") as f:
                    out.append(f.read())
            except Exception:
                pass
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return out
    return grab


def gemini_ask(cfg=None):
    """An `ask` bound to the configured Gemini endpoint. Returns '' on any
    failure so a single bad shot never dies the whole catalogue run."""
    from . import gemini
    cfg = cfg or gemini.config()

    def ask(messages) -> str:
        text, _detail = gemini.call(cfg, messages)
        return text or ""
    return ask


def _is_complete(out_json: str) -> bool:
    """Whether an episode's catalogue is finished, WITHOUT re-detecting cuts.

    A finished run stamps "complete" (and "windows", the total shot count).
    Older catalogues predate the stamp but always hold every window already —
    cut detection ran in full during their build — so an all-described one is
    treated as done. A half-done episode (blanks, or fewer shots than its
    window total) is not, and is resumed normally. This is the check that lets
    a restart skip finished episodes in milliseconds instead of re-reading the
    whole video file for each one.
    """
    try:
        with open(out_json, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    if data.get("complete") is True:
        return True
    shots = data.get("shots") or []
    if not shots:
        return False
    described = sum(1 for s in shots if s.get("description"))
    total = data.get("windows")
    if isinstance(total, int):
        return len(shots) >= total and described == len(shots)
    return described == len(shots)      # pre-stamp: every window is present


def run(video_path: str, out_json: str = "", known_characters: list | None = None,
        max_minutes: float = 0.0, cast_dir: str = "",
        refs: dict | None = None, log=lambda *a: None) -> dict:
    """Catalogue one local video end to end. The function the CLI/UI calls.

    `max_minutes` caps how far in it goes — set it to 20 for a cheap quality
    check before paying to tag a whole two-hour film. `out_json` defaults to
    `<video>.catalog.json` beside the file. `cast_dir` is a folder of
    `Name/photo.jpg` reference photos; passing it makes the model identify the
    cast against real faces at catalogue time, so the character labels it
    writes are reliable enough for retrieval to filter on.
    """
    from .probe import probe
    from . import subtitles, naming, gemini

    ok, why = gemini.available()
    if not ok:
        raise RuntimeError(f"Gemini set nahi hai: {why}")

    # Fast resume: a finished episode is skipped in milliseconds — no probe, no
    # subtitle load, and crucially no cut detection (which re-reads the whole
    # video off the USB disk and is what made every restart re-scan the shows
    # already done). A half-done episode falls through and resumes normally.
    out_json = out_json or (os.path.splitext(video_path)[0] + ".catalog.json")
    if _is_complete(out_json):
        log(f"  {naming.parse(video_path).label}: pehle se poori ho chuki — skip")
        return load_library(out_json)

    # Reference faces: load once here (or accept pre-loaded refs from a folder
    # run, so a 62-episode series reads the cast photos a single time).
    if refs is None and cast_dir:
        from . import assemble
        refs = assemble.load_refs(cast_dir)
    refs = refs or None

    duration = probe(video_path).duration
    if max_minutes and max_minutes * 60.0 < duration:
        duration = max_minutes * 60.0
    kind, _src, cues = subtitles.load_for_video(video_path)
    source = naming.parse(video_path).label
    out_json = out_json or (os.path.splitext(video_path)[0] + ".catalog.json")

    # No subtitle anywhere? Make one from the audio before cataloguing, so the
    # episode still gets dialogue anchors — the strongest, exact placement
    # signal, without which retrieval falls back to the weaker picture search.
    # Whisper writes `<video>.en.srt` beside the file, so a later run reuses it
    # for free. This is the safety net for a show whose subtitle pack is missing
    # an episode (e.g. a series finale) — a real gap that would otherwise build
    # a whole episode blind.
    if not cues:
        try:
            from . import transcribe as _tx
            if _tx.available():
                log("  subtitles: koi nahi mili — Whisper se bana rahe hain "
                    "(ek-baar, thoda time lagega)...")
                _tx.transcribe_file(video_path, log=log)
                kind, _src, cues = subtitles.load_for_video(video_path)
        except Exception as exc:                 # never let this kill the run
            log(f"    (auto-transcribe nahi ho paaya: {exc})")

    # Say out loud whether the dialogue signal is even present. A catalogue
    # with zero subtitle lines still works off the descriptions, but the
    # strongest label a shot can carry is what was said in it — so if this is
    # 0 it is worth knowing now, not discovering it silently in the JSON.
    if cues:
        log(f"  subtitles: {len(cues)} lines ({kind}) — dialogue will be tagged")
    else:
        # List the subtitle-looking files that ARE beside the video, so a
        # "none" is not a dead end. If an .srt is sitting right there but not
        # matched, that is a naming/sync problem to see, not a missing file.
        folder = os.path.dirname(video_path)
        subs = []
        try:
            subs = [f for f in os.listdir(folder)
                    if f.lower().endswith((".srt", ".vtt", ".ass", ".ssa"))]
        except OSError:
            pass
        log(f"  subtitles: koi line nahi mili ({kind}) — sirf picture se tag hoga.")
        if subs:
            log(f"    (folder me ye subtitle file(s) hain par match nahi hui: "
                f"{', '.join(subs[:5])} — naam video jaisa rakho ya .en.srt)")
        else:
            log("    (folder me koi .srt file hai hi nahi — download karke daalo)")

    # A character list (which may carry aliases, e.g.
    # "Arthur = Arthur Fleck, Joker, Joaquin Phoenix") both nudges the model
    # to name the canonical person AND collapses its varied labels afterwards.
    canon = alias_map(known_characters or [])
    canon_names = sorted({v for v in canon.values()}) or (known_characters or [])
    if canon_names:
        log(f"  characters: {', '.join(canon_names)} (baaki ko 'unknown' rakhega)")
    if refs:
        log(f"  reference faces: {len(refs)} character(s) — "
            f"{', '.join(sorted(refs)[:12])} (in faces se pehchan hogi)")
    else:
        log("  reference faces: koi nahi — model bina photo ke characters "
            "guess karega (kam bharosemand). --cast folder do to accuracy badhegi.")

    cuts = detect_cuts(video_path)
    windows = (shots_from_cuts(cuts, duration) if cuts
               else fixed_windows(duration))
    how = f"{len(cuts)} scene cuts" if cuts else "even windows (no cuts found)"
    log(f"  {source}: {len(windows)} shots to catalogue — {how}")
    return build_catalog(source, video_path, duration, out_json,
                         real_grab(video_path), gemini_ask(), cues=cues,
                         known_characters=canon_names, canon=canon,
                         windows=windows, log=log, refs=refs)


def run_folder(folder: str, known_characters: list | None = None,
               max_minutes: float = 0.0, cast_dir: str = "",
               log=lambda *a: None) -> dict:
    """Catalogue every episode under a folder — a whole series in one go.

    Each episode gets its own `<episode>.catalog.json` beside it, so a night
    that stops at episode 20 of 62 resumes at 20, and an episode already fully
    catalogued is skipped in seconds. Returns {episode_path: shot_count}. One
    bad episode is logged and stepped over, never fatal to the rest. `cast_dir`
    (reference face folder) is read once and reused for every episode.
    """
    from . import naming
    videos = list(naming.walk_media(folder))
    if not videos:
        raise RuntimeError(f"is folder me koi video nahi mila: {folder}")
    refs = None
    if cast_dir:
        from . import assemble
        refs = assemble.load_refs(cast_dir) or None
        log(f"  reference faces: {len(refs or {})} character(s) loaded — "
            "poori series me inhi se pehchan hogi")
    log(f"  {len(videos)} episode(s) mile — ek-ek karke catalogue honge")
    out = {}
    for i, video in enumerate(videos, 1):
        label = naming.parse(video).label
        log(f"\n  [{i}/{len(videos)}] {label}")
        try:
            lib = run(video, known_characters=known_characters,
                      max_minutes=max_minutes, refs=refs, log=log)
            out[video] = sum(1 for s in lib.values() if s.description)
        except Exception as exc:              # one bad episode never dies a night
            log(f"      SKIP — {exc}")
            out[video] = 0
    return out


# ---------------------------------------------------------------------------
# retrieval — search the catalogue by meaning (lexical v1)
# ---------------------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9']+")


def _terms(text: str) -> set:
    return set(_WORD.findall((text or "").lower()))


# Strong adult markers. Deliberately NOT 'intimate' or a bare 'bare' — those fire
# on "intimate conversation" / "bare hands" and would drop innocent footage.
_EXPLICIT_RE = re.compile(
    r"\b(nude|nudity|naked|topless|bottomless|full frontal|breasts?|nipples?|"
    r"buttocks|genital\w*|sex scene|having sex|sexual intercourse|making love|"
    r"performs? oral|explicit sexual|graphic sex|orgy|brothel sex)\b", re.I)


def is_explicit(shot) -> bool:
    """Whether a shot is adult (nudity / sex / graphic) and must never be placed
    in a video. Uses the catalogued `explicit` flag when present (new builds),
    and falls back to strong keywords in the description/tags for libraries built
    before the flag existed (e.g. Game of Thrones) — so it protects them too."""
    if getattr(shot, "explicit", False):
        return True
    text = (getattr(shot, "description", "") or "") + " " \
        + " ".join(getattr(shot, "tags", None) or [])
    return bool(_EXPLICIT_RE.search(text))


def search(library: dict, query: str, character: str = "",
           need_safe: bool = True, limit: int = 8, characters=None,
           allow_explicit: bool = False) -> list:
    """Best shots for a narration query, most relevant first.

    A lexical overlap over description + tags + action + dialogue. When the
    caller names the character(s) the line is about (`characters`, or the single
    `character`), a shot that actually SHOWS one of them is ranked in a tier ABOVE
    every shot that does not — so "the line is about Hank" always places a Hank
    shot, never a look-alike scene that merely shares a keyword. Within a tier,
    the lexical score orders them.
    """
    want = [c.strip().lower() for c in (characters or [character]) if c and c.strip()]
    q = _terms(query)
    if not q and not want:
        return []
    scored = []
    for shot in library.values():
        if need_safe and not shot.safe:
            continue
        if shot.quality == "low":
            continue
        if not allow_explicit and is_explicit(shot):
            continue                              # never place adult footage
        desc_t = _terms(shot.description) | _terms(shot.action)
        tag_t = _terms(" ".join(shot.tags))
        dlg_t = _terms(shot.dialogue)
        score = (2.0 * len(q & tag_t) + 1.0 * len(q & desc_t)
                 + 1.5 * len(q & dlg_t))
        tier = 0
        if want:
            chars = " ".join(shot.characters).lower()
            n_match = sum(1 for w in want if w in chars)
            if n_match:
                tier = 1                          # shows a wanted character
                score += 5.0 * n_match            # and more if several match
            elif shot.characters:
                score -= 1.0                      # names only OTHER people
        # a character-anchored request keeps only shots that show the person;
        # otherwise fall back to lexical hits so a blank shot still gets footage.
        if tier == 0 and want and score <= 0:
            continue
        if tier > 0 or score > 0:
            scored.append((tier, score, shot))
    scored.sort(key=lambda s: (-s[0], -s[1], s[2].start))
    return [shot for _t, _s, shot in scored[:limit]]
