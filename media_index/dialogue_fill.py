"""Fill the `dialogue` of finished catalogues that have none (no text subtitles when they were indexed).

Source order: a text subtitle that is now present (sidecar .srt / Subs/ folder / embedded track) — the best
text — else faster-whisper large-v3-turbo on the GPU, English audio track (dubs like Death Note's are picked).
Only `dialogue` is written; descriptions, characters and timings are untouched. Writes `dialogue_source`.

    py -m media_index.dialogue_fill <list.json | folder> [--min-share 0.05] [--whisper-only]
"""
from __future__ import annotations

import argparse, glob, json, os, subprocess, sys, tempfile, time

from . import catalog, subtitles

MODEL = "large-v3-turbo"
_model = None


def _lp(p: str) -> str:
    return "\\\\?\\" + os.path.abspath(p) if os.name == "nt" and len(p) >= 250 and not p.startswith("\\\\?\\") else p


def _gpu_dlls():
    """ctranslate2 on Windows needs the cuBLAS/cuDNN wheels' DLL folders on the search path (else it hangs)."""
    try:
        import nvidia.cublas
        base = os.path.dirname(nvidia.cublas.__path__[0])
    except Exception:
        return
    for sub in ("cublas", "cudnn", "cuda_nvrtc"):
        b = os.path.join(base, sub, "bin")
        if os.path.isdir(b):
            os.add_dll_directory(b)
            os.environ["PATH"] = b + os.pathsep + os.environ.get("PATH", "")


FAST_MODEL = "small"                    # language id + word timing for subtitle alignment (~0.5 GB VRAM)
_fast = None


def fast_model():
    """small multilingual whisper: fast word timestamps for aligning subtitle cues and for language id."""
    global _fast
    if _fast is None:
        _gpu_dlls()
        from faster_whisper import WhisperModel
        _fast = WhisperModel(FAST_MODEL, device="cuda", compute_type="float16")
    return _fast


def release_model():
    """drop the large model after use (6 workers x 1.6 GB would not fit in 12 GB of VRAM)."""
    global _model
    if _model is not None:
        _model = None
        import gc
        gc.collect()


def release_fast():
    """drop the small model between jobs so other workers' GPU passes have the VRAM."""
    global _fast
    if _fast is not None:
        _fast = None
        import gc
        gc.collect()


def model():
    global _model
    if _model is None:
        _gpu_dlls()
        from faster_whisper import WhisperModel
        _model = WhisperModel(MODEL, device="cuda", compute_type="float16")
    return _model


def english_track(video: str) -> int:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream_tags=language",
                        "-of", "csv=p=0", _lp(video)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    langs = [x.strip().lower() for x in r.stdout.splitlines()]
    for i, l in enumerate(langs):
        if l in ("eng", "en", "english"):
            return i
    return 0


def whisper_cues(video: str, log=print) -> list:
    wav = os.path.join(tempfile.gettempdir(), f"dlg_{os.getpid()}.wav")
    trk = english_track(video)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", _lp(video), "-map", f"0:a:{trk}", "-vn", "-ac", "1", "-ar", "16000",
                    wav], check=True)
    try:
        segs, info = model().transcribe(wav, language="en", vad_filter=True, beam_size=5, word_timestamps=True,
                                        condition_on_previous_text=False)
        # cues from WORD timings (a segment's own span can swallow a minute of silence): break at a pause
        # > 0.5 s, at sentence end, or after 5 s -> every line lands only on the shots where it is spoken
        cues, cur = [], []

        def flush():
            if cur:
                cues.append(subtitles.Cue(len(cues), int(cur[0].start * 1000), int(cur[-1].end * 1000),
                                          "".join(w.word for w in cur).strip()))
                cur.clear()
        for s in segs:
            if s.no_speech_prob >= 0.6:
                continue
            for w in s.words or []:
                if w.probability < 0.15:
                    continue
                if cur and (w.start - cur[-1].end > 0.5 or w.end - cur[0].start > 5.0):
                    flush()
                cur.append(w)
                if w.word.strip()[-1:] in ".?!":
                    flush()
        flush()
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass
    return cues


def share(shots) -> float:
    return sum(1 for s in shots if (s.get("dialogue") or "").strip()) / max(1, len(shots))


def _extract(cmd, work, wav, progress):
    """ffmpeg audio extraction watched like catalog_refine.read_once: heartbeat every 30 s while a.wav grows; no growth
    for 10 min = Drive stalled -> killed, 'incomplete read' (temporary)."""
    with open(os.path.join(work, "ffmpeg.err"), "w", encoding="utf-8", errors="replace") as ef:
        p = subprocess.Popen(cmd, cwd=work, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=ef)
        last, last_t = -1, time.time()
        while True:
            try:
                return p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
            size = os.path.getsize(wav) if os.path.exists(wav) else 0
            if size != last:
                last, last_t = size, time.time()
                progress(f"audio {size / 32000 / 60:.0f} min")
            elif time.time() - last_t > 600:
                p.kill()
                try:
                    p.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    pass
                raise RuntimeError("incomplete read: audio extraction stalled (Drive?)")


def fill(video: str, min_share=0.05, whisper_only=False, log=print, progress=None) -> str:
    """(Re)attach dialogue using the SAME aligned-sync rules as catalog_refine (v4): every subtitle candidate
    (sidecar rips, embedded text track, stored cues) is sync-checked against the audio; whisper only if none fits.
    Only the dialogue fields are written (tmp + os.replace) - always, also when nothing is heard, so the subtitle
    files just checked are remembered (dialogue_info.checked) and the job does not come back in a loop."""
    import shutil as _sh, tempfile as _tf
    from . import catalog_refine as R
    progress = progress or (lambda *a: None)
    cat = os.path.splitext(video)[0] + ".catalog.json"
    if not catalog._is_complete(_lp(cat)):
        return "skip: catalogue not complete"
    data = json.load(open(_lp(cat), encoding="utf-8"))
    shots = data.get("shots", [])
    src = str(data.get("dialogue_source", ""))
    if share(shots) >= min_share and not src.startswith("whisper") and src not in ("sidecar", "embedded", ""):
        return "skip: already has verified dialogue"
    meta = R.probe(video)
    work = _tf.mkdtemp(prefix="dlg_")
    cues, kind, info = [], "none (no audio)", {}
    try:
        tr, sb = data.get("audio_track", R.pick_audio(meta)), R.pick_text_sub(meta)     # refine chose the track by ear
        if tr is not None:
            cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", _lp(video), "-map", f"0:a:{tr}", "-af", "aresample=async=1:first_pts=0", "-ac", "1", "-ar", "16000",
                   "a.wav"]
            if sb is not None:
                cmd += ["-map", f"0:s:{sb}", "-c:s", "srt", "emb.srt"]
            wav = os.path.join(work, "a.wav")
            rc = _extract(cmd, work, wav, progress)
            if rc != 0 and sb is not None:
                rc = _extract(cmd[:cmd.index("a.wav") + 1], work, wav, progress)
            if rc != 0 or not os.path.exists(wav) or len(R._audio(wav)) / 16000 < 0.97 * meta["duration"] - 5:
                raise RuntimeError("incomplete read: audio could not be read completely")
            tagged = next((a["lang"] for a in meta["audio"] if a["n"] == tr), "und")
            cues, kind, info = R.sync_dialogue(video, data, work, meta["duration"], tagged, progress)
        else:
            info = dict(checked=[[os.path.basename(p), int(os.stat(_lp(p)).st_mtime), os.stat(_lp(p)).st_size]
                                 for p in subtitles.sidecar_candidates(video)])
    finally:
        R._AUDIO.clear()
        _sh.rmtree(work, ignore_errors=True)
    for s in shots:
        s["dialogue"] = catalog.dialogue_for(cues, s["start"], s["end"]) if cues else ""
    data["dialogue_source"], data["dialogue_info"] = kind, info
    if cues:
        data["cues"] = [[round(c.start_ms / 1000, 2), round(c.end_ms / 1000, 2), c.text] for c in cues]
    else:
        data.pop("cues", None)
    tmp = cat + ".tmp"
    with open(_lp(tmp), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(_lp(tmp), _lp(cat))
    if not cues:
        return f"no speech found ({kind}) - recorded"
    return f"ok {kind}: {share(shots):.0%} of shots now carry dialogue ({len(cues)} lines)"


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--min-share", type=float, default=0.05)
    ap.add_argument("--whisper-only", action="store_true")
    a = ap.parse_args()
    if a.src.lower().endswith(".json"):
        vids = json.load(open(a.src, encoding="utf-8"))
    else:
        vids = sorted(v for e in ("mkv", "mp4", "avi", "m4v") for v in glob.glob(os.path.join(a.src, "**", f"*.{e}"), recursive=True))
    t0 = time.time()
    for n, v in enumerate(vids, 1):
        t = time.time()
        try:
            r = fill(v, a.min_share, a.whisper_only)
        except Exception as e:
            r = f"FAILED {type(e).__name__}: {str(e)[:120]}"
        print(f"{time.strftime('%H:%M:%S')} [{n}/{len(vids)}] {os.path.basename(v)[:60]}  {r}  ({time.time() - t:.0f}s)", flush=True)
    print(f"done {len(vids)} in {(time.time() - t0) / 60:.0f} min")


if __name__ == "__main__":
    main()
