"""Narration timing.

Best case: faster-whisper gives every word's timestamp (offline, CPU).
Fallback (no whisper / model unavailable): word-count weighting refined by
snapping scene boundaries to real SILENCE gaps in the audio — proven to fix
the "text ahead of voice" problem on real narration.
"""
from __future__ import annotations

import re
import subprocess
import threading


def duration(path: str) -> float:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                              "format=duration", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=60).stdout.strip()
    except subprocess.TimeoutExpired:
        return 0.0
    return float(out or 0)


def silence_gaps(path: str, noise_db=-27, min_d=0.15, max_t=None):
    """[(start, end), ...] silent stretches of the narration."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-i", path]
    if max_t:
        cmd += ["-t", str(max_t)]
    cmd += ["-af", f"silencedetect=noise={noise_db}dB:d={min_d}", "-f", "null", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=180).stderr
    except subprocess.TimeoutExpired:
        return []
    starts = [float(m) for m in re.findall(r"silence_start: ([0-9.]+)", out)]
    ends = [float(m) for m in re.findall(r"silence_end: ([0-9.]+)", out)]
    return list(zip(starts, ends[:len(starts)]))


def try_whisper_words(audio: str, model_size: str, language, log=print,
                      load_timeout=300):
    """[(word, start, end)] or None if whisper unavailable.

    The FIRST run downloads the model from Hugging Face; on a slow/blocked
    connection that download can hang indefinitely with no error, which
    used to freeze the whole job forever. It now runs on a daemon thread
    with a hard timeout, so a stuck download degrades to silence-sync
    instead of hanging the job (and can't block process exit either)."""
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        log(f"  whisper unavailable ({type(exc).__name__}) -> silence-snap sync")
        return None

    log(f"  whisper ({model_size}) loading model "
        f"(first run downloads it — up to {load_timeout // 60} min) ...")
    result = {}

    def _load():
        try:
            result["model"] = WhisperModel(model_size, device="cpu",
                                           compute_type="int8")
        except Exception as exc:
            result["error"] = exc

    t = threading.Thread(target=_load, daemon=True)
    t.start()
    t.join(timeout=load_timeout)
    if t.is_alive():
        log(f"  whisper model download timed out after {load_timeout}s "
            "(network blocked?) -> silence-snap sync")
        return None
    if "error" in result:
        log(f"  whisper unavailable ({type(result['error']).__name__}) "
            "-> silence-snap sync")
        return None

    try:
        log("  whisper transcribing narration ...")
        model = result["model"]
        segs, info = model.transcribe(audio, word_timestamps=True,
                                      language=language)
        words = []
        for seg in segs:
            for w in seg.words or []:
                words.append((w.word.strip(), w.start, w.end))
        log(f"  whisper: {len(words)} words ({info.language})")
        return words or None
    except Exception as exc:
        log(f"  whisper transcription failed ({type(exc).__name__}) "
            "-> silence-snap sync")
        return None


def scene_windows(scenes, audio: str, model_size="base", language=None,
                  log=print):
    """Per-scene (start, end) seconds + optional per-word times.

    scenes: objects with .narration (text). Returns (windows, words|None).
    """
    total = duration(audio)
    counts = [max(1, len(s.narration.split())) for s in scenes]
    total_words = sum(counts)

    words = try_whisper_words(audio, model_size, language, log)
    if words:
        # boundary = end time of the last word belonging to each scene
        bounds, acc = [0.0], 0
        for c in counts[:-1]:
            acc += c
            idx = min(len(words) - 1, round(acc * len(words) / total_words))
            bounds.append(words[idx][1])
        bounds.append(total)
    else:
        # weighted split, then snap each boundary to the nearest silence gap
        gaps = silence_gaps(audio)
        centers = [(a + b) / 2 for a, b in gaps]
        bounds, t = [0.0], 0.0
        for c in counts[:-1]:
            t += total * c / total_words
            near = min(centers, key=lambda g: abs(g - t), default=t)
            bounds.append(near if abs(near - t) <= 1.4 else t)
        bounds.append(total)
    # monotonic + min scene length guard
    for i in range(1, len(bounds)):
        bounds[i] = max(bounds[i], bounds[i - 1] + 1.2)
    bounds[-1] = total
    windows = [(bounds[i], bounds[i + 1]) for i in range(len(scenes))]
    return windows, words


def _norm(w: str) -> str:
    return re.sub(r"[^\w']", "", w, flags=re.UNICODE).lower()


def align_narration_times(narration: str, window, words):
    """Absolute spoken-start time for EVERY word of a scene's narration.

    With whisper `words` [(text,start,end)] it matches the narration words to
    the transcript (sequential fuzzy match) so text lands exactly on the spoken
    word. Without whisper it linearly interpolates across the scene window.
    Returns a list of floats, len == number of narration words.
    """
    w0, w1 = window
    narr = narration.split()
    n = max(1, len(narr))
    interp = [w0 + (w1 - w0) * i / n for i in range(n)]
    if not words:
        return interp

    cand = [(_norm(t), s) for (t, s, e) in words
            if w0 - 0.5 <= s <= w1 + 0.8 and _norm(t)]
    if not cand:
        return interp

    times = [None] * n
    j = 0
    for i, raw in enumerate(narr):
        tgt = _norm(raw)
        if not tgt:
            continue
        for k in range(j, min(len(cand), j + 7)):
            c = cand[k][0]
            if c == tgt or (len(tgt) >= 4 and len(c) >= 4
                            and (c.startswith(tgt[:4]) or tgt.startswith(c[:4]))):
                times[i] = cand[k][1]
                j = k + 1
                break

    # fill unmatched words by interpolating between known anchors
    known = [(i, t) for i, t in enumerate(times) if t is not None]
    if not known:
        return interp
    if known[0][0] != 0:
        times[0] = w0
        known = [(0, w0)] + known
    if known[-1][0] != n - 1:
        times[n - 1] = w1
        known = known + [(n - 1, w1)]
    for (ia, ta), (ib, tb) in zip(known, known[1:]):
        for i in range(ia + 1, ib):
            if times[i] is None:
                frac = (i - ia) / max(1, (ib - ia))
                times[i] = ta + (tb - ta) * frac
    for i in range(n):
        if times[i] is None:
            times[i] = interp[i]
    # keep monotonic
    for i in range(1, n):
        times[i] = max(times[i], times[i - 1])
    return times


def word_time(words, scene_window, scene_text, word_index):
    """Back-compat single-word helper (interpolation)."""
    w0, w1 = scene_window
    n = max(1, len(scene_text.split()))
    return w0 + (w1 - w0) * (word_index / n)
