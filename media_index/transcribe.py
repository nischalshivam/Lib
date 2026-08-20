"""Last resort: make the subtitles ourselves, from the English audio.

Some releases ship with no text subtitles at all — the Breaking Bad season 2
folder this was written for had none across all thirteen episodes. Downloading
an .srt per episode works but is manual, and the free API tiers are tight.
Transcribing the audio removes the dependency entirely: it works for any file,
forever, with no account and no rate limit.

Two design decisions matter:

**The result is written as an ordinary `.srt` beside the video.** Nothing
downstream needs to know it was machine-made. It is cached forever, the index
picks it up through the normal sidecar path, and you can open and fix a line by
hand if you ever want to.

**The English track is chosen explicitly.** A Hindi-dubbed release lists the
dub first, and transcribing that produces fluent Hindi against an English
script — a failure that looks like "nothing matches" rather than like a
mistake.

Requires `faster-whisper` (`pip install faster-whisper`). It is optional: the
rest of the tool runs without it, and this module reports its absence clearly
instead of failing at an unhelpful moment.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field

from . import naming
from .probe import ProbeError, pick_audio, probe, require_ffmpeg
from .subtitles import Cue

# base.en is the useful default: roughly 5x realtime on a normal CPU, and for
# clear scripted dialogue its accuracy is well past what fuzzy matching needs.
DEFAULT_MODEL = "base.en"
SAMPLE_RATE = 16000


class TranscribeUnavailable(RuntimeError):
    """faster-whisper is not installed, or its model could not be loaded."""


@dataclass
class TranscribeResult:
    path: str
    srt_path: str = ""
    cues: list = field(default_factory=list)
    seconds: float = 0.0
    model: str = ""
    status: str = "done"          # done | skipped | failed
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("done", "skipped")


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _load_model(name: str, device: str = "auto", compute_type: str = "auto"):
    """Load the model, falling back to the CPU when the GPU cannot be used.

    "auto" picks CUDA whenever a GPU is visible, and a machine can have a
    GPU without the CUDA runtime beside it. That failed with

        Library cublas64_12.dll is not found or cannot be loaded

    which reads like a missing model and is nothing of the kind — the model
    was there, the graphics libraries were not. It cost a real build its
    narration alignment, silently, and the timeline fell back on an estimate
    that was three minutes out.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise TranscribeUnavailable(
            "faster-whisper is not installed — run: pip install faster-whisper"
        ) from exc

    attempts = [(device, compute_type)]
    if device == "auto":
        attempts.append(("cpu", "int8"))
    last = None
    for dev, ctype in attempts:
        try:
            return WhisperModel(name, device=dev, compute_type=ctype)
        except Exception as exc:                 # any GPU/driver/IO problem
            last = exc
    raise TranscribeUnavailable(
        f"could not load model {name!r}: {last}. The first run downloads it, "
        "so this usually means no internet or a blocked connection."
    ) from last


def extract_audio(video_path: str, out_wav: str, timeout=3600) -> str:
    """Pull the English track down to 16 kHz mono, which is what Whisper wants."""
    info = probe(video_path)
    if not info.has_audio:
        raise ProbeError("file has no audio track")
    track = pick_audio(info)
    cmd = [require_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
           "-i", video_path, "-map", f"0:a:{track}",
           "-ac", "1", "-ar", str(SAMPLE_RATE), "-vn", out_wav]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0 or not os.path.exists(out_wav):
        raise ProbeError(f"audio extraction failed: {(r.stderr or '')[-300:]}")
    return out_wav


def _segments_to_cues(segments) -> list[Cue]:
    cues = []
    for seg in segments:
        text = (getattr(seg, "text", "") or "").strip()
        if not text:
            continue
        cues.append(Cue(idx=len(cues),
                        start_ms=int(getattr(seg, "start", 0.0) * 1000),
                        end_ms=int(getattr(seg, "end", 0.0) * 1000),
                        text=text))
    return cues


def write_srt(cues: list, path: str) -> str:
    def tc(ms):
        s, ms = divmod(max(0, int(ms)), 1000)
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    body = "\n\n".join(
        f"{i}\n{tc(c.start_ms)} --> {tc(c.end_ms)}\n{c.text}"
        for i, c in enumerate(cues, 1))
    with open(path, "w", encoding="utf-8") as f:
        f.write(body + "\n")
    return path


def srt_path_for(video_path: str) -> str:
    """Where a generated subtitle goes. `.en.srt` so it wins the language rank."""
    return os.path.splitext(video_path)[0] + ".en.srt"


def transcribe_file(video_path: str, model=None, model_name=DEFAULT_MODEL,
                    overwrite=False, log=lambda *a: None) -> TranscribeResult:
    """Transcribe one file and leave a .srt beside it."""
    out_srt = srt_path_for(video_path)
    res = TranscribeResult(path=video_path, srt_path=out_srt,
                           model=model_name)

    if os.path.isfile(out_srt) and not overwrite:
        res.status = "skipped"
        res.note = "a subtitle already exists"
        return res

    t0 = time.time()
    tmp_wav = os.path.join(tempfile.gettempdir(),
                           f"_mi_tx_{abs(hash(video_path))}.wav")
    try:
        log(f"    extracting audio…")
        extract_audio(video_path, tmp_wav)

        if model is None:
            model = _load_model(model_name)

        log(f"    transcribing with {model_name}…")
        segments, info = model.transcribe(
            tmp_wav,
            language="en",
            vad_filter=True,               # skip silence; faster and cleaner
            beam_size=5,
            condition_on_previous_text=False)   # stops runaway repetition
        res.cues = _segments_to_cues(segments)

        if not res.cues:
            res.status = "failed"
            res.note = "no speech was recognised"
            return res

        write_srt(res.cues, out_srt)
        res.status = "done"
    except (TranscribeUnavailable,) :
        raise
    except (ProbeError, OSError, RuntimeError) as exc:
        res.status = "failed"
        res.note = str(exc)[:200]
    finally:
        try:
            os.remove(tmp_wav)
        except OSError:
            pass
        res.seconds = time.time() - t0
    return res


def transcribe_folder(root: str, model_name=DEFAULT_MODEL, overwrite=False,
                      log=print) -> list[TranscribeResult]:
    """Transcribe every file that still has no subtitles.

    Built for an overnight run: files that already have a subtitle are skipped,
    so an interrupted pass resumes for free, and one failure never stops the
    rest.
    """
    from .subtitles import load_for_video

    files = list(naming.walk_media(root))
    todo = []
    for p in files:
        if os.path.isfile(srt_path_for(p)) and not overwrite:
            continue
        kind, _, cues = load_for_video(p)
        if cues and not overwrite:
            continue
        todo.append(p)

    log(f"{len(files)} file(s) found, {len(todo)} need transcribing")
    if not todo:
        return []

    model = _load_model(model_name)        # loaded once, reused for every file
    results = []
    for i, path in enumerate(todo, 1):
        log(f"  [{i}/{len(todo)}] {os.path.basename(path)}")
        try:
            r = transcribe_file(path, model=model, model_name=model_name,
                                overwrite=overwrite, log=log)
        except TranscribeUnavailable:
            raise
        except Exception as exc:           # one bad file must not stop the run
            r = TranscribeResult(path=path, status="failed",
                                 note=f"{type(exc).__name__}: {exc}"[:200])
        results.append(r)
        if r.status == "done":
            log(f"      {len(r.cues)} lines in {r.seconds/60:.1f} min "
                f"-> {os.path.basename(r.srt_path)}")
        else:
            log(f"      {r.status}: {r.note}")
    return results


def format_results(results: list[TranscribeResult]) -> str:
    from . import term
    if not results:
        return "  nothing needed transcribing"
    done = [r for r in results if r.status == "done"]
    failed = [r for r in results if r.status == "failed"]
    lines = ["", "TRANSCRIPTION", ""]
    for r in results:
        icon = term.sym("ok") if r.status == "done" else (
            term.sym("skip") if r.status == "skipped" else term.sym("fail"))
        lines.append(f"  {icon} {os.path.basename(r.path)}  "
                     f"{len(r.cues)} lines  {r.seconds/60:.1f} min"
                     + (f"  {r.note}" if r.note else ""))
    total = sum(r.seconds for r in results) / 60
    lines += ["", f"  {len(done)} transcribed, {len(failed)} failed, "
                  f"{total:.0f} min total"]
    if done:
        lines.append(f"  {term.sym('arrow')} now run 'check' again, then 'build'")
    return "\n".join(lines)
