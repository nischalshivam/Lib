"""Hindi / Hinglish voiceover through OmniVoice — chunked, checked, re-rolled.

Why a separate path for Hindi (measured 2026-10-01):
  * digits are garbled in a Hindi script -> numbers are spelled out first
    (`hinglish_num`);
  * the user picked the 32-step voice over the 16-step default;
  * OmniVoice now and then drops or repeats a stretch, and a single ASR pass over
    a 60 s+ clip ALSO skips stretches (both whisper-small and OmniVoice's
    large-v3-turbo did, on different files) — so neither the voice nor the check
    can be trusted at clip length. ~15 s pieces fix both: each piece is spoken on
    its own, transcribed on its own (one 30 s whisper window, reliable), compared
    with its script, and re-made if words are missing or extra.

Only HTTP to the OmniVoice server (it owns the GPU, the model and the
large-v3-turbo transcriber at /api/transcribe). Pieces are joined with a short
breath. Nothing here touches English voiceovers.
"""
from __future__ import annotations

import difflib
import os
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata

from . import hinglish_num

STEPS = int(os.environ.get("OMNIVOICE_STEPS_HI", "32"))
MAX_WORDS = int(os.environ.get("HI_CHUNK_WORDS", "40"))       # ~15 s of speech
MIN_COVERAGE = float(os.environ.get("HI_MIN_COVERAGE", "0.80"))
MAX_LEN_RATIO = float(os.environ.get("HI_MAX_LEN_RATIO", "1.35"))
TRIES = int(os.environ.get("HI_TRIES", "3"))                   # 1 + 2 re-rolls
PAUSE_S = float(os.environ.get("HI_PAUSE_S", "0.25"))


def split_chunks(text: str, max_words: int = MAX_WORDS) -> list:
    """Sentence-packed pieces of <= max_words; an over-long sentence is cut at
    its commas."""
    sents = [s.strip() for s in re.split(r"(?<=[।!?.])\s+|\n+", text) if s.strip()]
    chunks, cur, n = [], [], 0
    for s in sents:
        w = len(s.split())
        if cur and n + w > max_words:
            chunks.append(" ".join(cur))
            cur, n = [], 0
        cur.append(s)
        n += w
    if cur:
        chunks.append(" ".join(cur))
    out = []
    for c in chunks:
        if len(c.split()) <= int(max_words * 1.6):
            out.append(c)
            continue
        buf, k = [], 0
        for p in re.split(r"(?<=[,;])\s+", c):
            pw = len(p.split())
            if buf and k + pw > max_words:
                out.append(" ".join(buf))
                buf, k = [], 0
            buf.append(p)
            k += pw
        if buf:
            out.append(" ".join(buf))
    return out


# Consonant skeleton shared by Devanagari, Urdu and Latin. The transcriber
# auto-detects the language and writes perfectly spoken Hindi in URDU script on
# some pieces (Hindi and Urdu sound the same), so comparing letters directly
# scored a correct piece 0%. Vowels, matras and the vowel-ish letters y/v/h
# (و ی ہ double as vowels in Urdu) are dropped on both sides; dental/retroflex
# and aspirated/plain pairs merge, as do nukta sounds with their base letter.
_DEV = {}
for _letters, _k in (("कखक़ख़", "k"), ("गघग़", "g"), ("ङञणन", "n"), ("चछ", "c"),
                     ("जझज़", "j"), ("टठतथ", "t"), ("डढदधड़ढ़", "d"), ("पफफ़", "p"),
                     ("बभ", "b"), ("म", "m"), ("र", "r"), ("लळ", "l"),
                     ("शषस", "s")):
    for _ch in unicodedata.normalize("NFD", _letters):
        if _ch != "़":
            _DEV.setdefault(_ch, _k)
_URDU = {}
for _letters, _k in (("ب", "b"), ("پ", "p"), ("تٹطة", "t"), ("ثسشص", "s"),
                     ("جذزژضظ", "j"), ("چ", "c"), ("خقکكغ", "k"), ("گ", "g"),
                     ("دڈڑ", "d"), ("ر", "r"), ("ف", "p"), ("ل", "l"), ("م", "m"),
                     ("نں", "n")):
    for _ch in _letters:
        _URDU[_ch] = _k
_LATIN = {c: c for c in "bcdgjklmnprst"}
_LATIN.update({"f": "p", "q": "k", "x": "k", "z": "j"})


def _skel(t: str) -> str:
    """Script-independent consonant skeleton (numbers spelled out first —
    the transcriber writes digits)."""
    t, _ = hinglish_num.normalize(t or "")
    t = unicodedata.normalize("NFD", t).replace("़", "")   # nukta -> base
    out = []
    for ch in t.lower():
        k = _DEV.get(ch) or _URDU.get(ch) or _LATIN.get(ch)
        if k:
            out.append(k)
    return "".join(out)


def score(script: str, heard: str) -> tuple:
    """(coverage of the script by what was heard, heard/script length)."""
    a, b = _skel(script), _skel(heard)
    if not a:
        return 1.0, 1.0
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    cov = sum(x.size for x in sm.get_matching_blocks()) / len(a)
    return cov, len(b) / len(a)


def _tts(requests, api, voice, text) -> bytes:
    with open(voice, "rb") as vf:
        r = requests.post(api + "/api/tts",
                          data={"text": text, "language": "hi", "format": "wav",
                                "steps": STEPS},
                          files={"voice": (os.path.basename(voice), vf)}, timeout=900)
    r.raise_for_status()
    if len(r.content) < 2000:
        raise RuntimeError("OmniVoice returned no audio for a piece")
    return r.content


def _heard(requests, api, wav_path) -> str | None:
    try:
        with open(wav_path, "rb") as f:
            r = requests.post(api + "/api/transcribe", files={"audio": f}, timeout=300)
        r.raise_for_status()
        r.encoding = "utf-8"
        return r.json().get("text", "")
    except Exception:
        return None                      # older server / ASR down -> unchecked


def _join(parts: list, out_wav: str, tmp: str) -> None:
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                            "-show_entries", "stream=sample_rate,channels",
                            "-of", "csv=p=0", parts[0]],
                           capture_output=True, text=True).stdout.strip()
    sr, ch = (probe.split(",") + ["24000", "1"])[:2]
    gap = os.path.join(tmp, "gap.wav")
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"anullsrc=r={sr}:cl={'mono' if ch == '1' else 'stereo'}",
                    "-t", f"{PAUSE_S}", gap], check=True)
    lst = os.path.join(tmp, "list.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for i, p in enumerate(parts):
            if i:
                f.write(f"file '{gap}'\n")
            f.write(f"file '{p}'\n")
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "concat",
                    "-safe", "0", "-i", lst, "-c:a", "pcm_s16le", out_wav], check=True)


def generate(api: str, voice: str, text: str, out_wav: str, log=print) -> str:
    """Speak a Hindi/Hinglish script into out_wav, piece by piece, checked."""
    import requests
    text, n_num = hinglish_num.normalize(text)
    if n_num:
        log(f"  [voiceover] {n_num} number(s) spelled out as Hinglish words "
            "(e.g. $80,000 -> अस्सी हज़ार डॉलर)")
    chunks = split_chunks(text)
    log(f"  [voiceover] OmniVoice Hindi: {len(text.split())} words in {len(chunks)} "
        f"piece(s), {STEPS} steps, each piece checked by ear (re-made if words "
        f"go missing), voice {os.path.basename(voice)} ...")
    t0 = time.time()
    tmp = tempfile.mkdtemp(prefix="hi_vo_")
    parts, rerolled, weak, unchecked = [], 0, [], 0
    try:
        for i, chunk in enumerate(chunks, 1):
            best = None                                  # (cov, path)
            for attempt in range(1, TRIES + 1):
                p = os.path.join(tmp, f"c{i:04d}_{attempt}.wav")
                with open(p, "wb") as f:
                    f.write(_tts(requests, api, voice, chunk))
                heard = _heard(requests, api, p)
                if heard is None:                        # cannot check -> keep it
                    unchecked += 1
                    best = (1.0, p)
                    break
                cov, lr = score(chunk, heard)
                good = cov >= MIN_COVERAGE and lr <= MAX_LEN_RATIO
                rank = cov - max(0.0, lr - 1.0)          # penalise extra speech
                if best is None or rank > best[0]:
                    best = (rank, p)
                if good:
                    break
                if attempt < TRIES:
                    rerolled += 1
            if best[0] < MIN_COVERAGE:
                weak.append(i)
            parts.append(best[1])
            if i % 10 == 0 or i == len(chunks):
                log(f"    voiceover {i}/{len(chunks)} pieces "
                    f"({time.time() - t0:.0f}s, {rerolled} re-made)")
        os.makedirs(os.path.dirname(os.path.abspath(out_wav)) or ".", exist_ok=True)
        _join(parts, out_wav, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    msg = (f"  [voiceover] done in {time.time() - t0:.0f}s: {len(chunks)} pieces, "
           f"{rerolled} re-made")
    if unchecked:
        msg += f", {unchecked} unchecked (transcriber unavailable)"
    if weak:
        msg += (f"; still below {int(MIN_COVERAGE * 100)}% after {TRIES} tries: "
                f"piece(s) {weak[:12]} — listen to those spots")
    log(msg)
    return out_wav
