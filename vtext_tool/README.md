# VText — the finishing tool: script-driven text overlays for video essays

Upload three things, get back the same video with competitor-grade kinetic
text overlays — synced word-perfect to the narration, placed automatically in
empty frame areas, styled per niche. Audio and the edit itself are never
touched.

**Inputs (per video):**
1. Final edited video (MP4/MOV — any length/size)
2. Clean narration script (`.txt`)
3. Text instruction file (`.txt`) — generate it with
   `PROMPTS/SCRIPT_TO_INSTRUCTIONS_PROMPT.md` (paste prompt + your script
   into Claude, save the reply)

## Quick start (Windows)

1. Install Python 3.10+ (python.org, tick "Add to PATH")
2. Double-click `setup.bat` (once)
3. Double-click `run.bat` → pick the 3 files, choose the niche, **+ Add to
   queue** (up to 15 videos), **Start Queue**
4. Output lands next to each video as `<name>_vtext.mp4` plus a
   `<name>_vtext.report.json` QC report (what was rendered where, what was
   skipped and why)

Optional, best sync quality: `python -m pip install faster-whisper`
(first run downloads a ~150MB model). Without it, VText falls back to the
bundled offline pocketsphinx engine.

## CLI (same engine)

```
python vtext.py --video final.mp4 --script clean.txt \
    --instructions overlay.txt --out final_texted.mp4 \
    [--niche MOVIE_ESSAY] [--pack bold_geometric] [--accent "#FFD60A"] \
    [--energy 3] [--text-scale auto] [--density file|medium|light]
python vtext.py --queue jobs.json
```

## How it stays in sync (and never typos)

- The clean script is force-aligned to the video's audio (faster-whisper →
  pocketsphinx fallback) giving every script word a timestamp. Each event's
  `NARRATION_CUE` is matched against those words — text pops exactly on the
  spoken phrase. ASR is used **only for timing**; what appears on screen
  always comes from the instruction file, so transcription errors can never
  reach the screen.
- Frames around each moment are analyzed (faces, brightness, busyness) and
  the text goes to the best of 9 zones — never on a face, never outside the
  frame, varied zone-to-zone.
- Shot-cut protection ends a text before an unrelated cut; repetition memory
  keeps consecutive texts from sharing zone/animation; `BREATHING_MOMENT`
  windows stay completely clean.

## Layout

- `PROMPTS/` — the master LLM prompt + file format
- `docs/CONTROLS_AND_VARIATIONS.md` — product spec: manual controls,
  automatic decisions, variation space
- `engine/` — parser, align, cues, scenes, zones, styles, render, compose,
  qc, pipeline
- `presets/niches.json` — 8 niche presets; `assets/fonts/` — 11 bundled fonts
- `demo/` — proof-of-concept notes from the design session

Verified end-to-end on a real 60s clip + a real 96-event instruction file
(11 texts rendered in sync; out-of-clip events skipped cleanly — see the
report format).
