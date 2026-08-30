"""Intro diegetic punch-ins — the hook technique the user asked for.

For the first ~3 minutes only (the make-or-break part of a long video), when a
character on screen delivers a famous line, the narration ducks out, a beat of
silence lands, the ORIGINAL show/movie audio plays that line for a second or
two, another beat of silence, and the narration resumes exactly where it cut.
That momentary switch to the real voice is worth ten ordinary seconds of hook.

Everything needed is already in the clue script: a strong shot is marked
`hook: true` with an `exact_dialogue` line and a `source` + `season_episode`.
We match that line to the episode's subtitle to get its precise in/out, cut it
with a pad of silence on each side, and splice it into the finished video at the
scene where it belongs. Narration is INSERTED-around, never overwritten, so no
word of the script is lost — the video just grows by a couple of seconds.

Kept deliberately small and side-effect-injected (`run` = the ffmpeg call) so
the finder and the splice plan can be tested without touching a file.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile

PAD_S = 0.18                 # silence before and after the line — the "breath"
DEFAULT_INTRO_S = 180.0      # only the first three minutes
FIRST_MINUTE_S = 60.0
MAX_PUNCHES = 6              # whole intro
MIN_GAP_S = 7.0             # never two punch-ins closer than this
MIN_LINE_S = 1.0
MAX_LINE_S = 3.0
VOICE_GAIN = 1.7            # lift the (often centre-channel) dialogue after downmix


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (text or "").lower()).strip()


def resolve_video(source: str, season_episode: str, library: dict) -> str:
    """The real episode file behind a clue shot, via any library shot that
    already came from that show + episode (its `.file`)."""
    from . import subtitles, plan
    want_show = subtitles.show_prefix(f"{source} {season_episode}")
    want_ep = plan._norm_ep(f"{source} {season_episode}")
    if not want_ep:
        return ""
    for s in library.values():
        if plan._norm_ep(s.source) != want_ep:
            continue
        sh = subtitles.show_prefix(s.source)
        if want_show and sh and sh != want_show:
            continue
        if s.file and os.path.isfile(s.file):
            return s.file
    return ""


def subtitle_span(video: str, exact_dialogue: str) -> tuple:
    """(start, end) seconds of a line in the episode, from its subtitle. The
    line is matched on its first several words, so a slightly reworded clue
    still lands. () if the line is not found."""
    from . import subtitles
    key = _norm(exact_dialogue)
    if len(key) < 6:
        return ()
    head = key[:24]
    cues = subtitles.load_for_video(video)[2]
    for c in cues:
        if head in _norm(c.text):
            return (c.start_ms / 1000.0, c.end_ms / 1000.0)
    return ()


def find_intro_punches(beats: list, scenes: list, library: dict,
                       intro_s: float = DEFAULT_INTRO_S,
                       log=lambda *a: None) -> list:
    """Choose the punch-in moments: hook lines in the first `intro_s` seconds
    that resolve to a real episode + subtitle timing. One per scene, spaced,
    weighted to the first minute (that is where a hook earns its keep)."""
    scene_start = {sc.get("scene"): float(sc.get("start", 0.0)) for sc in scenes}
    picks, used, used_lines = [], [], set()
    for b in beats:
        bn = b.get("beat")
        t0 = scene_start.get(bn)
        if t0 is None or t0 > intro_s:
            continue
        for s in (b.get("shots") or []):
            if not s.get("hook") or not (s.get("exact_dialogue") or "").strip():
                continue
            vid = resolve_video(s.get("source", ""), s.get("season_episode", ""),
                                library)
            if not vid:
                continue
            span = subtitle_span(vid, s["exact_dialogue"])
            if not span:
                continue
            start, end = span
            # never punch the SAME original line twice (two clue shots often
            # quote one subtitle cue) — it would replay the identical audio.
            line_key = (os.path.basename(vid), round(start, 1))
            if line_key in used_lines:
                continue
            line_len = max(MIN_LINE_S, min(MAX_LINE_S, end - start))
            insert_at = t0 if bn != (beats[0].get("beat") if beats else None) \
                else scene_start.get(bn + 1, t0)      # never before the very first word
            if any(abs(insert_at - u) < MIN_GAP_S for u in used):
                continue
            used_lines.add(line_key)
            used.append(insert_at)
            picks.append({
                "insert_at": round(insert_at, 2), "video": vid,
                "line_start": round(start, 2), "line_len": round(line_len, 2),
                "speaker": s.get("speaker", ""), "dialogue": s["exact_dialogue"]})
            break                                     # one punch per scene
    picks.sort(key=lambda p: p["insert_at"])
    first_min = [p for p in picks if p["insert_at"] <= FIRST_MINUTE_S]
    rest = [p for p in picks if p["insert_at"] > FIRST_MINUTE_S]
    chosen = (first_min + rest)[:MAX_PUNCHES]
    chosen.sort(key=lambda p: p["insert_at"])
    for p in chosen:
        log(f"    punch-in @ {p['insert_at']:.0f}s — {p['speaker']}: "
            f"\"{p['dialogue'][:48]}\" ({p['line_len']:.1f}s original audio)")
    return chosen


def _probe(video: str) -> tuple:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate",
         "-of", "csv=p=0", video], capture_output=True, text=True).stdout.strip()
    w, h, rate = (out.split(",") + ["1280", "720", "25/1"])[:3]
    try:
        num, den = rate.split("/"); fps = round(float(num) / float(den))
    except Exception:
        fps = 25
    return int(w or 1280), int(h or 720), max(1, fps)


def build_segment(video: str, line_start: float, line_len: float, out: str,
                  w: int, h: int, fps: int, pad: float = PAD_S,
                  run=subprocess.run) -> str:
    """One punch segment: video rolls continuously; audio is pad silence, the
    original line (stereo-downmixed, dialogue lifted), then pad silence."""
    grab_start = max(0.0, line_start - pad)
    total = line_len + 2 * pad
    ms = int(pad * 1000)
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1")
    af = (f"aformat=channel_layouts=stereo,"
          f"atrim={pad}:{pad + line_len},asetpts=PTS-STARTPTS,"
          f"adelay={ms}|{ms},apad=pad_dur={pad + 0.05},"
          f"atrim=0:{total},volume={VOICE_GAIN}")
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{grab_start}", "-i", video,
         "-t", f"{total}", "-filter_complex", f"[0:v]{vf}[v];[0:a]{af}[a]",
         "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-r", str(fps), "-c:a", "aac", "-ar", "48000", "-ac", "2", out],
        check=True)
    return out


def apply(video_in: str, picks: list, out: str,
          run=subprocess.run, log=lambda *a: None) -> str:
    """Splice the punch segments into the finished video at their times. The
    base video is cut at each insert point and the segments concatenated
    between the pieces, so narration pauses for the line and resumes after it.
    Returns `video_in` unchanged when there is nothing to insert."""
    picks = [p for p in picks if p.get("insert_at") is not None]
    if not picks:
        return video_in
    w, h, fps = _probe(video_in)
    tmp = tempfile.mkdtemp(prefix="mi_punch_")
    seg_files = []
    for i, p in enumerate(sorted(picks, key=lambda p: p["insert_at"])):
        seg = os.path.join(tmp, f"punch_{i:02d}.mp4")
        build_segment(p["video"], p["line_start"], p["line_len"], seg,
                      w, h, fps, run=run)
        seg_files.append((p["insert_at"], seg))

    inputs = ["-i", video_in]
    for _, s in seg_files:
        inputs += ["-i", s]
    # coerce the base audio to the same stereo/48k as the punch segments, or
    # concat refuses to join a mono narration track to a stereo line.
    sfmt = "aformat=sample_rates=48000:channel_layouts=stereo"
    fc, order, prev = [], [], 0.0
    for i, (t, _s) in enumerate(seg_files):
        fc.append(f"[0:v]trim={prev}:{t},setpts=PTS-STARTPTS[bv{i}]")
        fc.append(f"[0:a]atrim={prev}:{t},asetpts=PTS-STARTPTS,{sfmt}[ba{i}]")
        order += [f"[bv{i}]", f"[ba{i}]", f"[{i + 1}:v]", f"[{i + 1}:a]"]
        prev = t
    fc.append(f"[0:v]trim={prev},setpts=PTS-STARTPTS[bvL]")
    fc.append(f"[0:a]atrim={prev},asetpts=PTS-STARTPTS,{sfmt}[baL]")
    order += ["[bvL]", "[baL]"]
    n = len(seg_files) * 2 + 1
    fc.append("".join(order) + f"concat=n={n}:v=1:a=1[v][a]")
    run(["ffmpeg", "-y", "-v", "error"] + inputs +
        ["-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-ar", "48000", "-ac", "2", "-movflags", "+faststart", out],
        check=True)
    log(f"  intro punch-ins: {len(seg_files)} original-audio moment(s) spliced "
        "into the first 3 minutes")
    return out
