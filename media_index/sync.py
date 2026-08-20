"""Detect (and correct) subtitle drift against the actual audio.

A downloaded .srt is very often timed for a *different release* — another cut,
another framerate, with or without a distributor intro. It then runs a few
seconds early or late, and every clip we cut lands next to the line instead of
on it. Worst of all it fails silently: the index looks perfectly healthy.

How it works, without any ML:

  1. A loudness reading every 50 ms says how loud the voice band is.
  2. The loudest share of the episode is called speech — the share the
     subtitles themselves claim, so both timelines end up equally dense.
  3. The subtitle cues give us where the audio *should* be speaking.
  4. Slide one against the other and keep the offset with the best agreement.

Step 1 used to be `silencedetect` with a fixed -30 dB floor, and that never
worked on a film. Drama is scored end to end, so almost nothing falls under
the floor and the "speech" timeline comes back as one unbroken block; on a
real Breaking Bad season every episode was called 100% speech. A solid block
agrees with subtitle cues equally well at every offset, so every drift it
reported was noise, and the episodes it "corrected" had never needed it.
Deciding the threshold from this episode's own loudness has no floor to be
wrong about.

Both timelines are rasterised into bins and packed into Python big integers, so
one candidate offset is a single shift + AND + popcount. That makes a full
search fast enough in pure Python — no numpy needed.

Framerate conversion (23.976 vs 25 fps) shows up as *stretch* rather than
shift. It is **measured**, not searched: the offset is found separately near
the start and near the end of the episode, and the difference between those
two answers over the time between them is the stretch.

That distinction is not academic. Trying nine standard ratios and keeping
whichever scores highest sounds equivalent and is not, because over 47 minutes
of speech the scores of all nine land within noise of each other — so the
winner is decided by chance. Measured on a real Breaking Bad season it chose
four *different* framerate conversions across thirteen episodes of one
download, which cannot happen, and `24→25` alone displaces the end of an
episode by two minutes. A wrong stretch is far worse than no stretch: a
constant offset puts every clip equally close, while a stretch is perfect at
the start and minutes out by the end, so the index looks healthy on the first
line anyone tests.

Two windows can disagree with each other, and that disagreement is the honest
confidence signal — much better than asking how far a correlation peak stands
above its neighbours, which depends entirely on how talkative the content is.
"""
from __future__ import annotations

import math
import re
import subprocess
from dataclasses import dataclass

from .probe import ProbeError, pick_audio, probe, require_ffmpeg

# Common framerate conversions. A wrong-framerate subtitle drifts steadily —
# perfect at the start, minutes out by the end.
SCALES = {
    1.0: "none",
    25.0 / 24.0: "24→25 fps",
    24.0 / 25.0: "25→24 fps",
    25.0 / 23.976: "23.976→25 fps",
    23.976 / 25.0: "25→23.976 fps",
    24.0 / 23.976: "23.976→24 fps",
    23.976 / 24.0: "24→23.976 fps",
    30.0 / 29.97: "29.97→30 fps",
    29.97 / 30.0: "30→29.97 fps",
}

COARSE_BIN_MS = 500
FINE_BIN_MS = 50
DEFAULT_RANGE_MS = 120_000        # subtitles are rarely more than 2 min out

# Measuring stretch needs a lever arm between the two windows. How long an
# arm is set by the snapping tolerance rather than by this number: a stretch
# is only believed when it lands within a share of a real conversion, and the
# resolution of the measurement is one bin over the lever, so a short file
# simply fails to reach the tiny NTSC ratios and says so. This only rules out
# files with no usable arm at all.
DRIFT_MIN_SPAN_S = 60.0
WINDOW_FRACTION = 0.30            # how much of each end to measure in
DRIFT_SEARCH_MS = 10_000          # how far a window may sit from the global fit
# A window needs enough lines in it to have one clear answer. With only a few,
# regularly spaced dialogue matches itself one exchange over just as well, and
# the window locks onto a neighbouring peak — which reads as drift that is not
# there. A feature-length episode puts a couple of hundred lines in each
# window; a two-minute clip puts three, and gets an honest refusal instead.
MIN_CUES_PER_WINDOW = 12
# Below this the stretch is not worth applying: a quarter of a second across a
# whole episode is far inside the length of the shortest line of dialogue.
MIN_MEANINGFUL_DRIFT_MS = 250
# A measured stretch is only believed when it lands on a real conversion. The
# tolerance is a share of how far that conversion is from 1.0, so the tiny
# NTSC ratios are held to a proportionally tighter standard than the PAL ones.
SCALE_SNAP_TOLERANCE = 0.25
# The widest stretch any real conversion produces, used to size the search.
MAX_CONVERSION_DRIFT = 0.045

# How far above coincidence a fit must sit before it is allowed to move
# anything. These are set from measurement, not taste.
#
# Across 56 real episodes of Breaking Bad the lift ran 1.2x to 1.7x, averaging
# 1.36x, while the synthetic fixture — where the answer is known — reads 3.3x.
# The old bar of 1.3x therefore admitted the entire real library, and eight
# episodes were "corrected" on that basis, five of them by around 30 seconds.
# One of those five, S04E05, then returned a clip half a minute from its line.
#
# A reading barely above coincidence is not a small measurement. It is no
# measurement, and acting on it moves subtitles that were already right.
HIGH_LIFT = 2.2
MEDIUM_LIFT = 1.8

_RE_SIL_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_RE_SIL_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


@dataclass
class SyncResult:
    offset_ms: int = 0            # ADD this to every cue time
    scale: float = 1.0            # MULTIPLY every cue time by this (before offset)
    scale_name: str = "none"
    score: float = 0.0            # 0..1 agreement at the winning offset
    chance: float = 0.0           # what two unrelated tracks would score here
    prominence: float = 0.0       # how far the peak stands above every rival
    confidence: str = "unknown"   # high | medium | low | unknown
    method: str = "silencedetect"
    note: str = ""

    @property
    def lift(self) -> float:
        """How far above coincidence the fit sits.

        Both timelines are made comparably dense before they are compared,
        so an unrelated pair scores `chance` and this lands at 1.0. Unlike a
        raw score it does not move when the content gets more talkative.
        """
        return (self.score / self.chance) if self.chance else 0.0

    @property
    def in_sync(self) -> bool:
        return abs(self.offset_ms) < 250 and self.scale == 1.0

    def describe(self) -> str:
        if self.confidence == "unknown":
            return f"sync not checked ({self.note})"
        if self.in_sync:
            return f"in sync (score {self.score:.2f}, {self.confidence})"
        bits = [f"{self.offset_ms:+d} ms"]
        if self.scale != 1.0:
            bits.append(self.scale_name)
        return (f"drift {' · '.join(bits)} "
                f"(score {self.score:.2f}, {self.lift:.1f}x chance, "
                f"{self.confidence})")


# ---------------------------------------------------------------------------
# 1. where the audio is actually speaking
# ---------------------------------------------------------------------------

def speech_intervals(video_path: str, noise_db=-30, min_silence=0.30,
                     max_seconds: float | None = None,
                     timeout=1800) -> tuple[list, float]:
    """[(start_s, end_s)] of non-silent audio, plus the duration analysed."""
    info = probe(video_path)
    if not info.has_audio:
        raise ProbeError("file has no audio track")
    duration = info.duration or 0.0
    limit = min(duration, max_seconds) if max_seconds else duration

    cmd = [require_ffmpeg(), "-hide_banner", "-nostats"]
    if max_seconds:
        cmd += ["-t", str(max_seconds)]
    cmd += ["-i", video_path, "-map", f"0:a:{pick_audio(info)}",
            "-ac", "1", "-ar", "8000",                 # cheap: mono, low rate
            "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
            "-f", "null", "-"]
    out = subprocess.run(cmd, capture_output=True, text=True,
                         errors="replace", timeout=timeout)
    text = out.stderr or ""

    silences, open_start = [], None
    for line in text.splitlines():
        m = _RE_SIL_START.search(line)
        if m:
            open_start = max(0.0, float(m.group(1)))
            continue
        m = _RE_SIL_END.search(line)
        if m and open_start is not None:
            silences.append((open_start, float(m.group(1))))
            open_start = None
    if open_start is not None:
        silences.append((open_start, limit or open_start))

    # invert silence -> speech
    speech, cursor = [], 0.0
    for a, b in silences:
        if a > cursor:
            speech.append((cursor, a))
        cursor = max(cursor, b)
    if limit and cursor < limit:
        speech.append((cursor, limit))
    return speech, (limit or (speech[-1][1] if speech else 0.0))


def loudness_envelope(video_path: str, bin_ms: int = FINE_BIN_MS,
                      max_seconds: float | None = None,
                      timeout=1800) -> list[float]:
    """Loudness in dBFS, one reading every `bin_ms`, over the voice band.

    This exists because `silencedetect` cannot find the silence in a film.
    Drama is scored end to end — music, room tone, traffic, weather — and
    almost none of it drops below a fixed -30 dB floor, so the "speech"
    timeline comes back as one unbroken block. Correlating a solid block
    against subtitle cues gives the same answer at every offset: measured on
    a real Breaking Bad season, every episode scored within noise of
    sqrt(speech_share x cue_share), which is precisely what two unrelated
    signals score, and prominence was 0.00 across the board.

    A continuous reading has no floor to be wrong about. What counts as
    speech is decided afterwards, against the rest of this episode's own
    loudness, so a quiet film and a loud one are treated alike.
    """
    info = probe(video_path)
    if not info.has_audio:
        raise ProbeError("file has no audio track")
    samples = max(1, int(round(8000 * bin_ms / 1000.0)))
    chain = (f"aresample=8000,aformat=channel_layouts=mono,"
             # the voice band, so score and rumble carry less of the reading
             f"highpass=f=200,lowpass=f=3500,"
             f"asetnsamples=n={samples}:p=0,astats=metadata=1:reset=1,"
             f"ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-")
    cmd = [require_ffmpeg(), "-hide_banner", "-nostats"]
    if max_seconds:
        cmd += ["-t", str(max_seconds)]
    cmd += ["-i", video_path, "-map", f"0:a:{pick_audio(info)}",
            "-af", chain, "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       errors="replace", timeout=timeout)

    out = []
    for line in (r.stdout or "").splitlines():
        if "RMS_level" not in line:
            continue
        value = line.rsplit("=", 1)[-1].strip()
        try:
            out.append(-200.0 if value.endswith("inf") else float(value))
        except ValueError:
            continue
    if not out:
        raise ProbeError("no loudness readings — ffmpeg lacks astats")
    return out


def bits_from_envelope(env: list, duty: float, bin_ms: int, n_bins: int,
                       group: int = 1) -> int:
    """Mark the loudest `duty` share of the episode as speech.

    Calibrating against the episode's own distribution rather than a fixed
    dB floor is what makes this work on any mix. `duty` comes from how much
    of the running time the subtitles themselves cover, so the two timelines
    are made comparably dense before they are ever compared — otherwise a
    nearly-solid audio track scores well against everything.
    """
    if not env:
        return 0
    ordered = sorted(env)
    k = min(max(int(len(ordered) * (1.0 - duty)), 0), len(ordered) - 1)
    threshold = ordered[k]

    buf = bytearray((n_bins + 7) // 8)
    for i, value in enumerate(env):
        if value <= threshold:
            continue
        slot = i // group
        if slot >= n_bins:
            break
        buf[slot >> 3] |= 1 << (slot & 7)
    return int.from_bytes(bytes(buf), "little")


def cue_duty(cues, analysed_s: float) -> float:
    """What share of the running time the subtitles claim someone is talking."""
    if not cues or analysed_s <= 0:
        return 0.30
    spoken = sum(max(0, c.end_ms - c.start_ms) for c in cues) / 1000.0
    return min(0.60, max(0.15, spoken / analysed_s))


# ---------------------------------------------------------------------------
# 2. rasterise both timelines into bitsets
# ---------------------------------------------------------------------------

def _bits_from_intervals(intervals, bin_ms: int, n_bins: int) -> int:
    """Pack [(start_s, end_s)] into an integer, one bit per bin."""
    bits = 0
    for a, b in intervals:
        i0 = max(0, int(a * 1000) // bin_ms)
        i1 = min(n_bins - 1, int(b * 1000) // bin_ms)
        if i1 < i0:
            continue
        width = i1 - i0 + 1
        bits |= ((1 << width) - 1) << i0
    return bits


def _bits_from_cues(cues, bin_ms: int, n_bins: int,
                    scale=1.0, shift_ms=0) -> int:
    ivals = [(((c.start_ms * scale) + shift_ms) / 1000.0,
              ((c.end_ms * scale) + shift_ms) / 1000.0) for c in cues]
    return _bits_from_intervals(ivals, bin_ms, n_bins)


def _agreement(a_bits: int, b_bits: int, a_pop: int, b_pop: int) -> float:
    """Cosine similarity of two binary vectors."""
    if not a_pop or not b_pop:
        return 0.0
    return (a_bits & b_bits).bit_count() / math.sqrt(a_pop * b_pop)


GUARD_MS = 2000        # a rival peak this close to the winner is the same peak


@dataclass
class Peak:
    score: float = 0.0
    offset_ms: int = 0
    prominence: float = 0.0
    at_limit: bool = False        # the best offset was the edge of the search

    @property
    def real(self) -> bool:
        """A peak found against the wall is not a peak, it is a wall.

        When nothing inside the search range fits, the best score drifts to
        whichever end of the range happens to be least bad, and the answer
        comes back as a confident-looking ±120000 ms. Treating that as a
        measurement is how a subtitle for a different release gets accepted.
        """
        return self.score > 0.0 and not self.at_limit


def _scan(a_bits, cues, bin_ms, n_bins, scale, lo_ms, hi_ms, step_ms,
          mask=None) -> Peak:
    """Best offset over a range, optionally within one window of the episode.

    `prominence` is the gap between the winning offset and the best rival that
    is not simply the shoulder of the same peak. It is reported, but it is no
    longer trusted as a confidence signal: on sparse synthetic audio it runs
    0.20-0.30 and on real film — where people talk more or less continuously —
    it runs 0.00-0.05 for the very same quality of match.
    """
    pad = n_bins + 2 * (max(abs(lo_ms), abs(hi_ms)) // bin_ms + 2)
    base = _bits_from_cues(cues, bin_ms, pad, scale=scale)
    if mask is not None:
        a_bits = a_bits & mask
    a_pop = a_bits.bit_count()
    if not a_pop:
        return Peak()

    scores = []
    for off_ms in range(lo_ms, hi_ms + 1, step_ms):
        shift = off_ms // bin_ms
        moved = (base << shift) if shift >= 0 else (base >> -shift)
        if mask is not None:
            moved &= mask
        scores.append((_agreement(a_bits, moved, a_pop, moved.bit_count()),
                       off_ms))
    if not scores:
        return Peak()
    best_score, best_off = max(scores)
    rivals = [s for s, o in scores if abs(o - best_off) > GUARD_MS]
    runner_up = max(rivals) if rivals else 0.0
    return Peak(score=best_score, offset_ms=best_off,
                prominence=best_score - runner_up,
                at_limit=best_off in (lo_ms, hi_ms) and lo_ms != hi_ms)


def _nearest_conversion(scale: float) -> tuple:
    """Snap a measured stretch to a real framerate conversion, or to 1.0.

    An arbitrary ratio is almost always measurement noise; a ratio that lands
    on one of the standard conversions is a claim worth making.
    """
    best, best_err = 1.0, abs(scale - 1.0)
    for cand in SCALES:
        if cand == 1.0:
            continue
        err = abs(scale - cand)
        if err < best_err and err <= SCALE_SNAP_TOLERANCE * abs(cand - 1.0):
            best, best_err = cand, err
    return best, SCALES.get(best, "none")


def measure_drift(a_coarse, a_fine, cues, n_coarse, n_fine, analysed_s,
                  centre_ms, log=lambda *a: None) -> tuple:
    """Measure stretch by fitting each end of the episode separately.

    Returns (scale, offset_ms, residual_ms, early, late) where `scale` is the
    CORRECTION — multiply cue times by it — so a track running fast comes back
    as a number below 1.0. `residual_ms` is how far the two ends still
    disagree once the stretch is taken out, and it is the number that decides
    whether any of this can be believed. `None` there means not measured.
    """
    span = analysed_s * WINDOW_FRACTION
    early_mid, late_mid = span / 2.0, analysed_s - span / 2.0
    lever_ms = (late_mid - early_mid) * 1000.0

    def population(a_s, b_s) -> int:
        return sum(1 for c in cues
                   if a_s * 1000 <= c.start_ms + centre_ms <= b_s * 1000)

    thin = min(population(0.0, span), population(analysed_s - span, analysed_s))
    if thin < MIN_CUES_PER_WINDOW:
        log(f"  only {thin} line(s) at one end — not enough to measure stretch")
        return 1.0, centre_ms, None, None, None

    # A conversion displaces a point by the ratio times its distance from the
    # start, so the far window can sit two minutes from the global fit on a
    # 45-minute episode. Sizing this from the lever instead of the whole
    # running time leaves the late window searching against the wall, which
    # reads as no measurement at all. Searching that span at 50 ms would be
    # wasteful, so each window is found coarsely and then refined.
    reach = int(analysed_s * 1000 * MAX_CONVERSION_DRIFT) + DRIFT_SEARCH_MS

    def window(lo_s: float, hi_s: float, scale: float = 1.0) -> Peak:
        coarse = _scan(a_coarse, cues, COARSE_BIN_MS, n_coarse, scale,
                       centre_ms - reach, centre_ms + reach, COARSE_BIN_MS,
                       mask=_bits_from_intervals([(lo_s, hi_s)],
                                                 COARSE_BIN_MS, n_coarse))
        if not coarse.real:
            return coarse
        fine = _scan(a_fine, cues, FINE_BIN_MS, n_fine, scale,
                     coarse.offset_ms - COARSE_BIN_MS,
                     coarse.offset_ms + COARSE_BIN_MS, FINE_BIN_MS,
                     mask=_bits_from_intervals([(lo_s, hi_s)],
                                               FINE_BIN_MS, n_fine))
        # at_limit belongs to the wide search; the refinement's own edges are
        # half a second away and mean nothing.
        return Peak(score=max(fine.score, coarse.score),
                    offset_ms=fine.offset_ms if fine.score >= coarse.score * 0.9
                    else coarse.offset_ms,
                    prominence=coarse.prominence, at_limit=False)

    # Each candidate stretch is judged by a question it cannot fake: with this
    # stretch applied, do the two ends of the episode ask for the SAME shift?
    #
    # Deriving the stretch arithmetically from two offset-only fits does not
    # work for the large ratios. A 4% conversion spreads the cues inside a
    # thirteen-minute window by half a minute, so no single shift shifts that
    # window into place and the fit lands on noise. Applying the stretch first
    # removes exactly that spread, which is why the right ratio is the one
    # that makes the disagreement collapse.
    trials = []
    for cand in SCALES:
        early = window(0.0, span, cand)
        late = window(analysed_s - span, analysed_s, cand)
        if not (early.real and late.real):
            continue
        residual = abs(late.offset_ms - early.offset_ms)
        quality = min(early.score, late.score)
        trials.append((cand, residual, quality, early, late))
        log(f"  {SCALES[cand]:<14} ends differ by {residual:6d} ms "
            f"(worst end scores {quality:.2f})")

    if not trials:
        return 1.0, centre_ms, None, None, None

    flat = next((t for t in trials if t[0] == 1.0), None)
    best = min(trials, key=lambda t: (t[1], -t[2]))
    cand, residual, quality, early, late = best
    offset = (early.offset_ms + late.offset_ms) // 2

    if cand != 1.0:
        # A stretch has to earn its place. It is only better than leaving the
        # track alone if it makes the ends agree decisively better AND fits at
        # least as well — otherwise this is the noise-picking that produced
        # four different conversions for one season.
        # The floor is below the drift the smallest real conversion produces
        # (23.976->24 moves the ends about 1.9 s apart across an episode),
        # since holding out for more would rule that conversion out entirely.
        clearly_better = (flat is not None
                          and residual < MIN_MEANINGFUL_DRIFT_MS
                          and flat[1] > max(4 * residual, 750)
                          and quality >= flat[2] * 0.95)
        if not clearly_better:
            cand, residual, quality, early, late = flat or best
            offset = (early.offset_ms + late.offset_ms) // 2
            cand = 1.0

    return cand, int(offset), float(residual), early, late


# ---------------------------------------------------------------------------
# 3. the detector
# ---------------------------------------------------------------------------

def detect(video_path: str, cues, search_ms=DEFAULT_RANGE_MS,
           try_framerates=True, max_seconds: float | None = None,
           log=lambda *a: None) -> SyncResult:
    """Compare `cues` against the audio of `video_path`."""
    if not cues:
        return SyncResult(confidence="unknown", note="no cues")
    method = "loudness"
    try:
        env = loudness_envelope(video_path, FINE_BIN_MS, max_seconds)
        analysed = len(env) * FINE_BIN_MS / 1000.0
    except (ProbeError, subprocess.SubprocessError, OSError) as exc:
        # Older ffmpeg builds have no astats. Falling back keeps the tool
        # working, and the caller can see which measurement it got.
        try:
            speech, analysed = speech_intervals(video_path,
                                                max_seconds=max_seconds)
        except (ProbeError, subprocess.SubprocessError, OSError):
            return SyncResult(confidence="unknown", note=str(exc)[:120])
        env, method = None, "silencedetect"
    if analysed <= 0:
        return SyncResult(confidence="unknown", note="no audio to measure")

    n_coarse = int(analysed * 1000) // COARSE_BIN_MS + 2
    n_fine = int(analysed * 1000) // FINE_BIN_MS + 2
    if env is not None:
        duty = cue_duty(cues, analysed)
        group = COARSE_BIN_MS // FINE_BIN_MS
        a_coarse = bits_from_envelope(env, duty, COARSE_BIN_MS, n_coarse, group)
        a_fine = bits_from_envelope(env, duty, FINE_BIN_MS, n_fine)
    else:
        a_coarse = _bits_from_intervals(speech, COARSE_BIN_MS, n_coarse)
        a_fine = _bits_from_intervals(speech, FINE_BIN_MS, n_fine)
    if a_coarse.bit_count() == 0:
        return SyncResult(confidence="unknown", note="audio is entirely silent",
                          method=method)

    # 1. one shift, no stretch — where does the whole track sit?
    coarse = _scan(a_coarse, cues, COARSE_BIN_MS, n_coarse, 1.0,
                   -search_ms, search_ms, COARSE_BIN_MS)
    if not coarse.real:
        return SyncResult(score=round(coarse.score, 4), confidence="low",
                          method=method,
                          note="no alignment found anywhere in range — "
                               "these subtitles are for another release")

    fine = _scan(a_fine, cues, FINE_BIN_MS, n_fine, 1.0,
                 coarse.offset_ms - COARSE_BIN_MS,
                 coarse.offset_ms + COARSE_BIN_MS, FINE_BIN_MS)
    best = fine if fine.score >= coarse.score * 0.9 else coarse

    # 2. does the end of the episode want a different shift from the start?
    scale, off, residual = 1.0, best.offset_ms, None
    early = late = None
    if try_framerates and analysed >= DRIFT_MIN_SPAN_S:
        scale, off, residual, early, late = measure_drift(
            a_coarse, a_fine, cues, n_coarse, n_fine, analysed,
            best.offset_ms, log)

    n = max(1, n_fine)
    chance = math.sqrt((a_fine.bit_count() / n)
                       * (_bits_from_cues(cues, FINE_BIN_MS, n).bit_count() / n))
    res = SyncResult(offset_ms=int(off), scale=scale,
                     scale_name=SCALES.get(scale, "none"),
                     score=round(best.score, 4), method=method,
                     chance=round(chance, 4),
                     prominence=round(best.prominence, 4))

    # 3. a correction that walks the subtitles off the end of the file is not
    #    a correction, however well it scored.
    moved = apply(cues, res.offset_ms, res.scale)
    span_ms = (probe(video_path).duration or analysed) * 1000.0
    if moved and (moved[0].start_ms < -2000 or moved[-1].end_ms > span_ms + 5000):
        return SyncResult(score=res.score, confidence="low", method=method,
                          note="the fit pushes the subtitles outside the "
                               "running time — not applied")

    # 4. confidence is how well the two ends agree, not how spiky the peak is
    lift = res.lift
    if residual is None:
        # No lever arm, so the only evidence is how far the single fit sits
        # above coincidence. Both bars matter: lift alone passes a handful of
        # cues that happened to land on something, and a raw score alone moves
        # with how talkative the content is.
        if best.score >= 0.70 and lift >= 1.8:
            res.confidence = "high"
        elif best.score >= 0.55 and lift >= 1.5:
            res.confidence = "medium"
        else:
            res.confidence = "low"
            res.note = "no clear fit, and too short to check for stretch"
    elif residual <= 300 and lift >= HIGH_LIFT:
        res.confidence = "high"
    elif residual <= 800 and lift >= MEDIUM_LIFT:
        res.confidence = "medium"
    elif lift < MEDIUM_LIFT:
        res.confidence = "low"
        res.note = (f"the fit is only {lift:.1f}x better than coincidence — "
                    "these subtitles do not match this audio")
    else:
        res.confidence = "low"
        res.note = (f"the start and the end disagree by {residual:.0f} ms — "
                    "this subtitle is probably for a different cut")
    return res


def apply(cues, offset_ms: int, scale: float = 1.0):
    """Return cues with the correction applied (never mutates the input)."""
    if offset_ms == 0 and scale == 1.0:
        return cues
    out = []
    for c in cues:
        moved = type(c)(idx=c.idx,
                        start_ms=max(0, int(c.start_ms * scale) + offset_ms),
                        end_ms=max(0, int(c.end_ms * scale) + offset_ms),
                        text=c.text)
        out.append(moved)
    return out
