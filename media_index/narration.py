"""Find out when each beat is actually spoken, instead of assuming.

`timeline.boundaries` estimates a beat's length from its word count at 150
words a minute, then stretches the whole plan onto the real runtime. That is
fine when the read is even. Real reads are not even: a narrator pauses on the
turn, races the list, holds the last line. An eight-minute script over a
nine-minute recording does not drift evenly — it drifts wherever the pauses
are, and every visual after a long pause sits under the wrong sentence.

The narration is scripted, so the recording says the same words the script
does. That makes this a text-to-audio alignment, and the tool already owns
both halves: `transcribe` turns the voiceover into timed words, and the
alignment idea is the one `align.py` uses for footage —

    find the handful of points that are unambiguous,
    keep only those that agree on order,
    interpolate everything between them.

An unambiguous point here is a word that occurs **exactly once in the script
and exactly once in the transcript**. There is no other place it could match.
On a two-thousand-word essay there are hundreds of them, which is far more
than the couple of dozen boundaries that need placing — so the interpolation
between anchors is over a few seconds, not a few minutes.

Everything degrades: no faster-whisper, no model, a transcript that will not
align — each of those returns nothing and lets `timeline` fall back on the
estimate it already had, with the reason said out loud.
"""
from __future__ import annotations

import os
import re
import tempfile
import time
from dataclasses import dataclass, field

DEFAULT_MODEL = "base.en"
DEFAULT_LANGUAGE = "en"
# A beat boundary further than this from the nearest anchor is interpolated
# across a stretch long enough that the estimate could be wrong by a shot.
FAR_FROM_ANCHOR_WORDS = 60
# Unicode-aware: an accented word (ação, préférée, größer) is ONE token, not a
# run of stripped one-letter fragments — otherwise a Portuguese/French/Spanish/
# German transcript loses every anchor. English ("don't", "1990s") is unchanged.
_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)


def model_for(language: str = DEFAULT_LANGUAGE) -> tuple:
    """(whisper model, language) for a script language.

    The library is English, but the *script* can be in any language. base.en is
    English-only and mis-hears foreign speech, so anything but English uses the
    multilingual ``base``. ``"auto"`` (or empty) lets whisper detect it.
    """
    lang = (language or "").strip().lower()
    if lang in ("", "en", "eng", "english"):
        return DEFAULT_MODEL, "en"
    if lang in ("auto", "detect"):
        return "base", None
    return "base", lang


class NarrationUnavailable(RuntimeError):
    """The voiceover could not be transcribed."""


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class Alignment:
    """Where every beat begins and ends in the recording."""
    spans: list = field(default_factory=list)        # [(start, end)] per beat
    anchors: int = 0
    script_words: int = 0
    heard_words: int = 0
    total_seconds: float = 0.0
    weak: list = field(default_factory=list)         # beats placed on a guess
    reason: str = ""                                 # why it did not work
    used_clean: bool = False     # the narration script was given and matched
    drifted: int = 0             # beats whose narration is not in that script

    @property
    def ok(self) -> bool:
        return bool(self.spans) and not self.reason

    @property
    def rate(self) -> float:
        return self.anchors / self.script_words if self.script_words else 0.0

    def summary(self) -> str:
        from . import term
        if not self.ok:
            return f"  narration not aligned — {self.reason}"
        d = term.sym("dot")
        return (f"  heard {self.heard_words} word(s) in "
                f"{self.total_seconds / 60:.1f} min {d} "
                f"{self.anchors} unmistakable word(s) matched "
                f"({self.rate:.0%}) {d} "
                f"{len(self.spans) - len(self.weak)}/{len(self.spans)} beats "
                "placed on the recording itself"
                + (f" {d} timed against your narration script"
                   if self.used_clean else ""))


def normalise(text: str) -> list:
    """Lowercase words, no punctuation. Both sides, identically."""
    return _WORD.findall(str(text or "").lower())


def script_words(beats: list) -> tuple:
    """(words, beat_end_index) — every narration word, in order.

    `beat_end_index[i]` is how many words have been spoken by the end of beat
    i, which is the only thing a boundary actually is.
    """
    words, ends = [], []
    for beat in beats:
        words += normalise(beat.get("narration") or "")
        ends.append(len(words))
    return words, ends


# How many of a beat's opening words have to line up before its position in
# the narration script is believed. Long enough that a common phrase cannot
# match by accident, short enough to survive the model dropping a comma.
HEAD_WORDS = 6


def _find_from(hay: list, needle: list, start: int):
    """Where `needle` ends in `hay`, searching forward from `start`.

    Matched on its opening words rather than the whole of it: the beat text
    in a visual script is a copy of the narration, and a copy made by a
    language model routinely loses a word or gains one. Demanding the whole
    passage back verbatim would fail on exactly the scripts this is for.
    """
    key = needle[:HEAD_WORDS] or needle
    for i in range(start, len(hay) - len(key) + 1):
        if hay[i:i + len(key)] == key:
            return min(len(hay), i + len(needle))
    return None


def beats_in_clean(beats: list, clean: list) -> tuple:
    """(beat_end_index into the clean script, how many beats drifted).

    The visual script's `narration` fields are a copy of the real narration,
    and the tool times the video by matching those words against what it
    hears. Every word the model quietly reworded is a word that cannot
    match, and a beat whose text has drifted takes its scene boundary with
    it.

    Given the narration script that was actually read aloud, the beats can
    be located inside THAT instead — the same text the voice is speaking, so
    the anchors are the real ones and the boundaries are the real ones.

    Returns (None, n) when the two disagree too much to be the same script,
    which is a thing worth saying rather than working around.
    """
    ends, cursor, drift = [], 0, 0
    for beat in beats:
        want = normalise(beat.get("narration") or "")
        if not want:
            ends.append(cursor)
            continue
        at = _find_from(clean, want, cursor)
        if at is None:
            # Try from the top: an essay that doubles back, or a beat the
            # model moved. Only accept it ahead of where we already are.
            at = _find_from(clean, want, 0)
            drift += 1
            if at is None or at < cursor:
                ends.append(cursor)
                continue
        cursor = at
        ends.append(cursor)
    if drift > max(2, len(beats) // 4):
        return None, drift
    return ends, drift


# ---------------------------------------------------------------------------
# hearing the recording
# ---------------------------------------------------------------------------

def available() -> tuple:
    from . import transcribe
    if not transcribe.available():
        return False, ("faster-whisper is not installed — run setup.bat, or "
                       "pip install faster-whisper")
    return True, "ready"


def heard(audio_path: str, model_name: str = DEFAULT_MODEL,
          language: str = DEFAULT_LANGUAGE,
          log=lambda *a: None) -> list:
    """Every word of the voiceover, with the second it was said.

    Word timestamps rather than segment ones. A segment is a sentence or
    more, so placing a beat boundary on segment times can only ever be right
    to within a sentence — and a sentence is three or four shots.
    """
    from . import transcribe
    ok, why = available()
    if not ok:
        raise NarrationUnavailable(why)
    if not os.path.isfile(audio_path):
        raise NarrationUnavailable(f"no such file: {audio_path}")

    wav = os.path.join(tempfile.gettempdir(),
                       f"_mi_nar_{abs(hash(audio_path))}.wav")
    try:
        log("    reading the voiceover…")
        transcribe.extract_audio(audio_path, wav)
        # Try the GPU, then the CPU — around the LISTENING, not just the
        # loading. Loading on "auto" succeeds on a machine with a graphics
        # card and no CUDA runtime; the failure comes later, on the first
        # actual computation, as
        #
        #     Library cublas64_12.dll is not found or cannot be loaded
        #
        # A fallback wrapped around the load alone never fired, and two
        # builds silently fell back on a word-count estimate instead.
        last = None
        for device, compute in (("auto", "auto"), ("cpu", "int8")):
            try:
                model = transcribe._load_model(model_name, device=device,
                                               compute_type=compute)
                log(f"    listening with {model_name} on {device}"
                    + (f" ({language})" if language and language != "en" else "")
                    + "…")
                out = _listen(model, wav, language=language)
                if out:
                    return out
                last = RuntimeError("nothing was heard")
            except transcribe.TranscribeUnavailable:
                raise
            except Exception as exc:              # any GPU/driver failure
                last = exc
                if device != "cpu":
                    log(f"    the graphics card could not be used ({exc}); "
                        "listening on the processor instead")
        raise NarrationUnavailable(str(last or "the recording could not be read"))
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass


def _listen(model, wav: str, language: str = "en") -> list:
    """Every word the model heard, with its second.

    ``language=None`` lets whisper auto-detect (used for "auto" scripts).
    """
    segments, _info = model.transcribe(
        wav, language=language, word_timestamps=True, beam_size=5,
        # No VAD here. It exists to skip silence in a film; on a voiceover it
        # can clip the quiet start of a line, and a word dropped from the
        # transcript is one fewer anchor.
        vad_filter=False,
        condition_on_previous_text=False)
    out = []
    for seg in segments:                          # a generator: this is where
        for w in (getattr(seg, "words", None) or []):   # the work happens
            text = normalise(getattr(w, "word", ""))
            if not text:
                continue
            out.append(Word(text=text[0],
                            start=float(getattr(w, "start", 0.0)),
                            end=float(getattr(w, "end", 0.0))))
    return out


# ---------------------------------------------------------------------------
# matching the two
# ---------------------------------------------------------------------------

def unique_anchors(script: list, spoken: list) -> list:
    """[(script_index, spoken_index)] for words that can only match once.

    A word appearing once in the script and once in the transcript has
    exactly one possible pairing — there is nothing to be wrong about. Common
    words are skipped entirely rather than guessed at, which is why this
    needs no scoring, no threshold and no tuning.
    """
    def once(words):
        seen: dict = {}
        for i, w in enumerate(words):
            seen[w] = i if w not in seen else -1
        return {w: i for w, i in seen.items() if i >= 0}

    a, b = once(script), once(spoken)
    pairs = [(a[w], b[w]) for w in a.keys() & b.keys()]
    return sorted(pairs)


def increasing(pairs: list) -> list:
    """The longest run whose spoken order matches the script order.

    A unique word can still be a false friend — the transcriber mishears one
    word as another that happens to appear elsewhere — and one such pair
    dragged out of order would pull every boundary near it. Keeping only the
    longest increasing run drops those without needing to know which they
    are. O(n log n), because a scripted essay yields hundreds of anchors.
    """
    import bisect
    if not pairs:
        return []
    tails, tail_at, prev = [], [], [-1] * len(pairs)
    for i, (_s, spoken) in enumerate(pairs):
        pos = bisect.bisect_left(tails, spoken)
        if pos == len(tails):
            tails.append(spoken)
            tail_at.append(i)
        else:
            tails[pos], tail_at[pos] = spoken, i
        prev[i] = tail_at[pos - 1] if pos else -1
    out, i = [], tail_at[-1]
    while i != -1:
        out.append(pairs[i])
        i = prev[i]
    return out[::-1]


def time_at(word_index: int, anchors: list, spoken: list,
            total: float) -> tuple:
    """(seconds, distance_in_words_to_the_nearest_anchor).

    Between two anchors the reading rate is taken as constant, which over a
    few seconds it very nearly is. Outside them the nearest anchor's rate is
    carried on, because refusing to place the first and last beats would
    leave the two most visible parts of the video unaligned.
    """
    if not anchors:
        return 0.0, 10 ** 6
    times = [spoken[b].start for _s, b in anchors]
    keys = [s for s, _b in anchors]

    import bisect
    pos = bisect.bisect_left(keys, word_index)
    if pos == 0:
        near = 0
    elif pos >= len(keys):
        near = len(keys) - 1
    else:
        near = pos if (keys[pos] - word_index) < (word_index - keys[pos - 1]) \
            else pos - 1
    distance = abs(keys[near] - word_index)

    lo = min(max(pos - 1, 0), len(keys) - 2) if len(keys) >= 2 else 0
    if len(keys) < 2:
        return max(0.0, min(total, times[0])), distance
    hi = lo + 1
    span = keys[hi] - keys[lo]
    if span <= 0:
        return max(0.0, min(total, times[lo])), distance
    frac = (word_index - keys[lo]) / span
    at = times[lo] + frac * (times[hi] - times[lo])
    return max(0.0, min(total, at)), distance


def align(beats: list, spoken: list, total_seconds: float = 0.0,
          clean: str = "") -> Alignment:
    """Turn a transcript into one (start, end) per beat.

    `clean` is the narration script that was read aloud, when there is one.
    It is used in preference to the beat text for exactly one reason: it is
    the words the voice is actually saying, so every anchor it produces is
    real. The beat text is a copy of it, and copies drift.
    """
    words, ends = script_words(beats)
    used_clean, drifted = False, 0
    clean_words = normalise(clean)
    if clean_words:
        mapped, drifted = beats_in_clean(beats, clean_words)
        if mapped:
            words, ends, used_clean = clean_words, mapped, True
    total = total_seconds or (spoken[-1].end if spoken else 0.0)
    result = Alignment(script_words=len(words), heard_words=len(spoken),
                       total_seconds=total, used_clean=used_clean,
                       drifted=drifted)
    if not words:
        result.reason = "the script has no narration text"
        return result
    if not spoken:
        result.reason = "nothing was heard in the recording"
        return result

    anchors = increasing(unique_anchors(words, [w.text for w in spoken]))
    result.anchors = len(anchors)
    if len(anchors) < 2:
        result.reason = (f"only {len(anchors)} word(s) could be matched "
                         "between the script and the recording — is this the "
                         "right audio for this script?")
        return result

    marks, t = [], 0.0
    for i, end_word in enumerate(ends):
        at, distance = time_at(end_word, anchors, spoken, total)
        at = max(at, t + 0.2)               # a beat can never end before it began
        if distance > FAR_FROM_ANCHOR_WORDS:
            result.weak.append(i + 1)
        marks.append((t, at))
        t = at
    # The last beat runs to the end of the recording: the closing line is
    # usually the one place a narrator slows right down, and cutting the
    # picture before the voice stops is the most visible mistake there is.
    if marks:
        start, _end = marks[-1]
        marks[-1] = (start, max(total, start + 0.5))
    result.spans = marks
    return result


def order_by_clean(beats: list, clean: str, log=lambda *a: None) -> list:
    """Put visual-script beats back into the order the narration is spoken.

    Genspark and GPT sometimes emit beats out of reading order — a later
    moment written before an earlier one. `align` matches beats into the clean
    script IN ORDER (`beats_in_clean`), so a shuffled script fails that match,
    drops to an even-read estimate, and the picture drifts seconds off the
    voice. This was the whole cause of a real 10-20s desync.

    Sorting beats by where their narration falls in the clean script repairs it
    deterministically, before a single frame is cut — and ONLY when it is a
    real improvement, so a script already in order (or a `clean` that is not
    this script at all) is returned untouched. No model, no cost.
    """
    words = normalise(clean)
    if not words or len(beats) < 3:
        return beats

    def _pos(b):
        w = normalise(b.get("narration") or "")
        return _find_from(words, w, 0) if w else None

    positions = [_pos(b) for b in beats]
    if sum(p is not None for p in positions) < max(3, len(beats) * 3 // 5):
        return beats                      # clean is not this script — do nothing

    # An unfound beat rides along just after its previous neighbour.
    filled, last = [], -1.0
    for p in positions:
        if p is None:
            filled.append(last + 0.5)
        else:
            filled.append(float(p)); last = float(p)
    order = sorted(range(len(beats)), key=lambda i: filled[i])
    ordered = [beats[i] for i in order]

    ends_b, drift_b = beats_in_clean(beats, words)
    ends_a, drift_a = beats_in_clean(ordered, words)
    improved = (ends_a is not None and ends_b is None) or (drift_a < drift_b)
    if not improved:
        return beats
    moved = sum(1 for i, j in enumerate(order) if i != j)
    for i, b in enumerate(ordered, 1):
        b["beat"] = i
    log(f"    reordered {moved} beat(s) to the narration's order "
        f"(out-of-order drift {drift_b} -> {drift_a}) — timing stays locked")
    return ordered


def read_clean(path: str) -> str:
    """The narration script as text. Never raises — it is an optional input."""
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def align_audio(beats: list, audio_path: str, model_name: str = "",
                total_seconds: float = 0.0, clean: str = "",
                language: str = DEFAULT_LANGUAGE,
                log=lambda *a: None) -> Alignment:
    """The whole thing: listen, match, report. Never raises.

    ``language`` picks the whisper model too: English keeps the fast base.en,
    any other language uses the multilingual base (see :func:`model_for`). An
    explicit ``model_name`` overrides that choice.
    """
    t0 = time.time()
    resolved_model, whisper_lang = model_for(language)
    model_name = model_name or resolved_model
    try:
        spoken = heard(audio_path, model_name=model_name,
                       language=whisper_lang, log=log)
    except NarrationUnavailable as exc:
        return Alignment(reason=str(exc))
    except Exception as exc:                    # a bad recording is not fatal
        return Alignment(reason=f"{type(exc).__name__}: {exc}")
    out = align(beats, spoken, total_seconds=total_seconds, clean=clean)
    if clean and not out.used_clean:
        log("    the narration script does not match this visual script's "
            "beats — timing from the beats instead")
    elif out.used_clean and out.drifted:
        log(f"    {out.drifted} beat(s) had been reworded away from your "
            "narration script; found anyway")
    log(f"    listened in {time.time() - t0:.0f}s")
    return out
