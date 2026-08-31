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
# cold-open (the 5-8s original-audio moment BEFORE the narration starts)
COLD_MIN_S = 5.0
COLD_MAX_S = 8.0
COLD_LEAD_S = 1.8          # scene context before the line
COLD_TAIL_S = 0.6          # a breath after it, before the narration begins
COLD_FADE_S = 0.4
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"   # even loudness so a quiet line still lands


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


def _line_key(video: str, start: float) -> tuple:
    return (os.path.basename(video), round(start, 1))


def find_intro_punches(beats: list, scenes: list, library: dict,
                       intro_s: float = DEFAULT_INTRO_S, exclude_lines=None,
                       log=lambda *a: None) -> list:
    """Choose the punch-in moments: hook lines in the first `intro_s` seconds
    that resolve to a real episode + subtitle timing. One per scene, spaced,
    weighted to the first minute (that is where a hook earns its keep).
    `exclude_lines` (keys from `_line_key`) skips lines already used elsewhere,
    e.g. the one the cold-open already spent."""
    scene_start = {sc.get("scene"): float(sc.get("start", 0.0)) for sc in scenes}
    picks, used = [], []
    used_lines = set(exclude_lines or ())
    for b in beats:
        bn = b.get("beat")
        t0 = scene_start.get(bn)
        if t0 is None or t0 > intro_s:
            continue
        # A shot qualifies on its exact_dialogue + a speaker (a real line someone
        # says on screen). hook:true is preferred but NOT required — most clue
        # scripts never set the hook flag, and requiring it left the intro with
        # zero punch-ins. Take the first qualifying line per scene.
        cand_shots = [s for s in (b.get("shots") or [])
                      if (s.get("exact_dialogue") or "").strip()
                      and (s.get("speaker") or "").strip()]
        cand_shots.sort(key=lambda s: (0 if s.get("hook") else 1))
        for s in cand_shots:
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
            line_key = _line_key(vid, start)
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


def _probe_audio(video: str) -> tuple:
    """(sample_rate, channels) of the base audio — the cold-open is encoded to
    match it so the two can be concatenated with a stream copy (no re-encode of
    the whole video)."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=sample_rate,channels",
         "-of", "csv=p=0", video], capture_output=True, text=True).stdout.strip()
    parts = (out.split(",") + ["48000", "2"])[:2]
    try:
        return int(parts[0] or 48000), int(parts[1] or 2)
    except ValueError:
        return 48000, 2


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


def _keyframe_at_or_after(video: str, t: float) -> float:
    """First video keyframe time >= t — a clean point to stream-copy from."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame",
         "nokey", "-show_entries", "frame=pts_time", "-read_intervals",
         f"{max(0.0, t):.2f}%+30", "-of", "csv=p=0", video],
        capture_output=True, text=True).stdout
    for line in out.splitlines():
        try:
            v = float(line.strip().strip(","))
        except ValueError:
            continue
        if v >= t:
            return v
    return t


def apply(video_in: str, picks: list, out: str,
          run=subprocess.run, log=lambda *a: None) -> str:
    """Splice the punch segments into the finished video at their times, so the
    narration pauses for each line and resumes after it. Only the HEAD (up to a
    keyframe just past the last punch) is re-encoded; the rest of the film is
    stream-COPIED, so a 24-min video splices in a minute or two, not twenty.
    Returns `video_in` unchanged when there is nothing to insert."""
    picks = [p for p in picks if p.get("insert_at") is not None]
    if not picks:
        return video_in
    picks = sorted(picks, key=lambda p: p["insert_at"])
    w, h, fps = _probe(video_in)
    ar, ac = _probe_audio(video_in)
    tmp = tempfile.mkdtemp(prefix="mi_punch_")
    split = _keyframe_at_or_after(video_in, picks[-1]["insert_at"] + 2.0)

    seg_files = []
    for i, p in enumerate(picks):
        seg = os.path.join(tmp, f"punch_{i:02d}.mp4")
        build_segment(p["video"], p["line_start"], p["line_len"], seg,
                      w, h, fps, run=run)
        seg_files.append((p["insert_at"], seg))

    # HEAD = base[0:split] with the punches spliced in, re-encoded to MATCH the
    # base so it can be stream-joined to the copied tail. The base slice is
    # normalised (fps + 48k stereo) then split for the trims (a filter output is
    # single-use). This also fixes prostudio finals whose audio is short/mono.
    inputs = ["-i", video_in]
    for _, s in seg_files:
        inputs += ["-i", s]
    sfmt = "aformat=sample_rates=48000:channel_layouts=stereo"
    # setsar=1 on EVERY video stream: a Sopranos 5.1 source can carry a
    # non-square SAR (e.g. 31975:31968) that the base (1:1) doesn't, and concat
    # rejects mismatched SAR ("parameters do not match"). Force all to square.
    nseg = len(seg_files)
    vlab = "".join(f"[nv{i}]" for i in range(nseg + 1))
    alab = "".join(f"[na{i}]" for i in range(nseg + 1))
    fc = [f"[0:v]trim=0:{split},fps={fps},format=yuv420p,setsar=1,"
          f"setpts=PTS-STARTPTS,split={nseg + 1}{vlab}",
          f"[0:a]atrim=0:{split},{sfmt},asetpts=PTS-STARTPTS,"
          f"asplit={nseg + 1}{alab}"]
    order, prev = [], 0.0
    for i, (t, _s) in enumerate(seg_files):
        fc.append(f"[nv{i}]trim={prev}:{t},setpts=PTS-STARTPTS[bv{i}]")
        fc.append(f"[na{i}]atrim={prev}:{t},asetpts=PTS-STARTPTS[ba{i}]")
        # normalise each punch segment too (fps + square SAR + format)
        fc.append(f"[{i + 1}:v]fps={fps},format=yuv420p,setsar=1,"
                  f"setpts=PTS-STARTPTS[pv{i}]")
        fc.append(f"[{i + 1}:a]{sfmt},asetpts=PTS-STARTPTS[pa{i}]")
        order += [f"[bv{i}]", f"[ba{i}]", f"[pv{i}]", f"[pa{i}]"]
        prev = t
    fc.append(f"[nv{nseg}]trim={prev}:{split},setpts=PTS-STARTPTS[bvL]")
    fc.append(f"[na{nseg}]atrim={prev}:{split},asetpts=PTS-STARTPTS[baL]")
    order += ["[bvL]", "[baL]"]
    n = nseg * 2 + 1
    fc.append("".join(order) + f"concat=n={n}:v=1:a=1[v][a]")
    head = os.path.join(tmp, "head.mp4")
    run(["ffmpeg", "-y", "-v", "error"] + inputs +
        ["-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-profile:v", "high",
         "-preset", "veryfast", "-crf", "20", "-threads", "0", "-c:a", "aac",
         "-ar", str(ar), "-ac", str(ac), head], check=True)

    # TAIL = base[split:end], stream-copied (split is a keyframe, so it is exact).
    tail = os.path.join(tmp, "tail.mp4")
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{split}", "-i", video_in,
         "-c", "copy", tail], check=True)

    listf = os.path.join(tmp, "list.txt")
    with open(listf, "w", encoding="utf-8") as f:
        for p in (head, tail):
            f.write(f"file '{os.path.abspath(p)}'\n")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", listf, "-c", "copy", "-movflags", "+faststart", out], check=True)
    log(f"  intro punch-ins: {len(seg_files)} original-audio moment(s) spliced "
        "into the first 3 minutes")
    return out


# --------------------------------------------------------------------------- #
# cold-open: the 5-8s original-audio hook BEFORE the narration begins
# --------------------------------------------------------------------------- #

def find_cold_open(beats: list, library: dict, log=lambda *a: None) -> dict:
    """The script's own opening hook: the FIRST shot marked hook:true with an
    exact_dialogue that resolves to a real episode + subtitle timing. That is
    the line the writer chose to open on, so it is always on-topic. Returns a
    spec, or {} if nothing usable — falls back to the earliest exact_dialogue."""
    def scan(require_hook: bool):
        for b in beats:
            for s in (b.get("shots") or []):
                if require_hook and not s.get("hook"):
                    continue
                if not (s.get("exact_dialogue") or "").strip():
                    continue
                vid = resolve_video(s.get("source", ""),
                                    s.get("season_episode", ""), library)
                if not vid:
                    continue
                span = subtitle_span(vid, s["exact_dialogue"])
                if not span:
                    continue
                return {"video": vid, "line_start": round(span[0], 2),
                        "line_len": round(span[1] - span[0], 2),
                        "speaker": s.get("speaker", ""),
                        "dialogue": s["exact_dialogue"]}
        return {}
    spec = scan(True) or scan(False)
    if spec:
        log(f"  cold-open: {spec['speaker']}: \"{spec['dialogue'][:52]}\" "
            f"({os.path.basename(spec['video'])})")
    return spec


def _cold_length(line_len: float) -> tuple:
    """(grab_lead, total) for a cold-open. The WHOLE line always plays — the
    lead-in flexes so the cut to narration lands after the line, never on top
    of it. 5s floor, 8s ceiling unless the line itself is longer."""
    lead, tail = COLD_LEAD_S, COLD_TAIL_S
    total = lead + line_len + tail
    if total > COLD_MAX_S:                         # trim the lead, never the line
        lead = max(0.8, COLD_MAX_S - line_len - tail)
        total = lead + line_len + tail
    if total < COLD_MIN_S:                          # too short — show more context
        lead += (COLD_MIN_S - total)
        total = COLD_MIN_S
    return round(lead, 2), round(total, 2)


def build_cold_open(spec: dict, out: str, w: int, h: int, fps: int,
                    ar: int = 48000, ac: int = 2, run=subprocess.run) -> str:
    """A natural clip of the scene — real audio, loudness-evened — that ends a
    breath after the hook line, fading down so the narration can begin. Encoded
    to match the base video (w/h/fps + audio ar/ac) so the two can be joined
    with a stream copy instead of re-encoding the whole film."""
    lead, total = _cold_length(float(spec["line_len"]))
    grab_start = max(0.0, float(spec["line_start"]) - lead)
    fo = max(0.0, total - COLD_FADE_S)
    ch = "stereo" if ac == 2 else "mono"
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
          f"fade=t=in:d=0.3,fade=t=out:st={fo}:d={COLD_FADE_S}")
    af = (f"aformat=channel_layouts={ch},{LOUDNORM},"
          f"afade=t=out:st={fo}:d={COLD_FADE_S}")
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{grab_start}", "-i",
         spec["video"], "-t", f"{total}",
         "-filter_complex", f"[0:v]{vf}[v];[0:a]{af}[a]",
         "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-profile:v", "high", "-r", str(fps),
         "-c:a", "aac", "-ar", str(ar), "-ac", str(ac), out], check=True)
    return out


def prepend_cold_open(video_in: str, spec: dict, out: str,
                      run=subprocess.run, log=lambda *a: None) -> str:
    """Put the cold-open in front of the finished video. Narration is untouched
    — it just starts a few seconds later, after the hook has landed.

    Fast path: only the ~6s cold-open is encoded (to match the base's format);
    the film itself is stream-COPIED via the concat demuxer, so a 24-minute
    video is joined in seconds instead of re-encoded for 20 minutes."""
    if not spec:
        return video_in
    w, h, fps = _probe(video_in)
    ar, ac = _probe_audio(video_in)
    tmp = tempfile.mkdtemp(prefix="mi_cold_")
    cold = build_cold_open(spec, os.path.join(tmp, "cold.mp4"), w, h, fps,
                           ar=ar, ac=ac, run=run)
    listf = os.path.join(tmp, "list.txt")
    with open(listf, "w", encoding="utf-8") as f:
        for p in (cold, video_in):
            safe = os.path.abspath(p).replace("'", "'\\''")
            f.write(f"file '{safe}'\n")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", listf, "-c", "copy", "-movflags", "+faststart", out], check=True)
    log("  cold-open: original-audio hook placed before the narration")
    return out
