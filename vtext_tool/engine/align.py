"""Audio -> per-script-word timestamps.

Strategy (best available wins):
  1. faster-whisper word timestamps (if installed) — handles music/dialogue.
  2. pocketsphinx free decode word timestamps (bundled model, offline).
Then the ASR word stream is matched against the clean script with difflib;
matched script words take the ASR time, unmatched ones are interpolated.
The script text is the only thing ever shown on screen — ASR is timing only.
"""
from __future__ import annotations

import difflib

from .util import norm_word, norm_words


def _asr_whisper(wav: str, language: str):
    from faster_whisper import WhisperModel  # noqa: optional dependency
    model = WhisperModel("base" if language != "en" else "base.en",
                         device="cpu", compute_type="int8")
    segments, _ = model.transcribe(wav, word_timestamps=True, beam_size=5,
                                   language=language or None)
    out = []
    for seg in segments:
        for w in seg.words or []:
            nw = norm_word(w.word)
            if nw:
                out.append((nw, float(w.start), float(w.end)))
    return out


def _asr_pocketsphinx(wav: str):
    import wave

    from pocketsphinx import Decoder
    d = Decoder(samprate=16000)
    wf = wave.open(wav)
    d.start_utt()
    while True:
        data = wf.readframes(4000)
        if not data:
            break
        d.process_raw(data, False, False)
    d.end_utt()
    out = []
    if d.seg():
        for seg in d.seg():
            if seg.word.startswith("<") or seg.word.startswith("["):
                continue
            nw = norm_word(seg.word)
            if nw:
                out.append((nw, seg.start_frame / 100.0, seg.end_frame / 100.0))
    return out


def align_script(wav: str, script_text: str, language: str = "en",
                 log=print):
    """Returns (words, coverage) where words is a list of
    {w, s, e, matched} per script word (s/e None outside audio coverage)."""
    asr, engine = [], None
    try:
        asr = _asr_whisper(wav, language)
        engine = "faster-whisper"
    except Exception:
        pass
    if not asr:
        try:
            asr = _asr_pocketsphinx(wav)
            engine = "pocketsphinx"
        except Exception as e:
            raise RuntimeError(
                "No ASR engine available. Install faster-whisper (best) or "
                f"pocketsphinx. Last error: {e}")
    log(f"[align] {engine}: {len(asr)} words recognized")

    script_words = norm_words(script_text)
    asr_words = [a[0] for a in asr]
    sm = difflib.SequenceMatcher(a=asr_words, b=script_words, autojunk=False)

    out = [{"w": w, "s": None, "e": None, "matched": False}
           for w in script_words]
    for blk in sm.get_matching_blocks():
        # single-word blocks are noise ("the"/"and" matching deep into the
        # script would hand bogus timestamps to unspoken sections)
        if blk.size < 2:
            continue
        for k in range(blk.size):
            out[blk.b + k]["s"] = asr[blk.a + k][1]
            out[blk.b + k]["e"] = asr[blk.a + k][2]
            out[blk.b + k]["matched"] = True

    matched_n = sum(1 for w in out if w["matched"])
    coverage = matched_n / max(1, len(out))

    # Interpolate unmatched words BETWEEN matched neighbours (never
    # extrapolate beyond audio coverage — those stay None => "not spoken").
    i = 0
    n = len(out)
    while i < n:
        if out[i]["matched"]:
            i += 1
            continue
        j = i
        while j < n and not out[j]["matched"]:
            j += 1
        prev = out[i - 1] if i > 0 else None
        nxt = out[j] if j < n else None
        if prev and nxt and prev["e"] is not None and nxt["s"] is not None:
            gap_words = j - i
            span = max(0.0, nxt["s"] - prev["e"])
            # only fill sane gaps (< ~2.5s per missing word)
            if span / max(1, gap_words) < 2.5:
                step = span / (gap_words + 1)
                tcur = prev["e"]
                for k in range(i, j):
                    out[k]["s"] = tcur + step * (k - i + 1) - step * 0.5
                    out[k]["e"] = tcur + step * (k - i + 1)
        i = j
    log(f"[align] coverage: {coverage:.0%} of script words matched to audio")
    return out, coverage
