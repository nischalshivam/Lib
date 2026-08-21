# ProStudio — automatic documentary video editor

Scene folders (from the Footage Collector) + narration audio → finished
ready-to-upload **16:9 4K MP4**, edited like a pro human editor.

## Quick start (Windows)
1. `setup.bat` (once)
2. `run.bat` → GUI opens with ONE video card:
   - Scenes folder (scene_001, scene_002, …)
   - Narration audio (one mp3 for the whole video)
   - Optional clean script (.txt)
   - **Optional visual-editor file** (.txt/.md) — your per-scene plan;
     overrides scene narration and pins exact on-screen text (see below)
   - Format (Auto-Rotate / Random / F1..F10), Language, Niche,
     Keyword-colors toggle
   - **+ Add Video** → queue up to 15 videos (overnight bulk)
3. **Start Queue** → a live **% progress bar** shows each stage
   (footage check → audio sync → shot plan → render → compositing);
   videos land in the output folder with a report each.

## Visual-editor file (optional per-scene guide)
Give the tool your own editing sheet and it follows it. Plain text, one
block per scene — labels are flexible:
```
Scene 1
NARRATION / TEXT: Tony arrived in Miami in 1980 with nothing.
ON-SCREEN TEXT: 1980 — MIAMI

Scene 2
Script Cue: He built an empire on fear.
On-Screen Text: THE EMPIRE
```
- `NARRATION` / `Script Cue` / `Narration` → overrides that scene's narration.
- `ON-SCREEN TEXT` → the exact words to show on screen for that scene
  (guaranteed to appear; `none` = let the tool auto-pick). Ignored if
  on-screen text is turned OFF.

## Preview & Edit in your browser (before the slow final render)
Click **🔍 Preview & Edit (Video 1)** instead of Start Queue. ProStudio plans
the video, renders a **fast low-res draft**, and opens a page in your browser:
- **Watch the draft** (plays in the browser) to see if clips match the narration.
- Every shot is a card showing its **thumbnail, time range, and the exact
  narration spoken under it**, so you can spot a mismatch instantly.
- Fix it right there: **Replace** a clip with your own file from your device,
  **Trim** (±), **Delete**, or reorder (↑ ↓). Timing stays locked to the audio
  so nothing drifts out of sync.
- **↻ Rebuild draft** to re-watch after changes.
- **✔ Export final** renders the full-quality 4K MP4 from your edited plan.

This is the ~5-minute check to make sure the footage fits the voiceover before
committing to the long 4K export. (CLI: `python review_server.py --queue jobs.json`)

## On-screen text (optional — OFF by default)
The focus is premium clips, animation and transitions — **no on-screen text by
default**. It stays available as an option per video:
- **Default (text OFF)** — clean footage, no captions.
- **On-screen text ON** (tick "On-screen text" / `--text`) — text synced to the
  narration. With `faster-whisper` you get **word-perfect** sync; without it, a
  silence-based fallback (~90%).

## Languages
- **Any language's audio + footage works** for the video itself.
- **Text ON** works out of the box for **Latin-script languages** (English,
  French, German, Spanish, Italian, Portuguese, Polish, Czech, Hungarian,
  Dutch — accents included). Whisper syncs 90+ languages. Fonts are **bundled**
  in `assets/fonts/`, so on-screen text works on any OS without installing
  anything.
- **Non-Latin scripts** (Hindi/Devanagari, Arabic, Chinese, …) need a matching
  font: set `PS_FONT_SANS` / `PS_FONT_SERIF` / `PS_FONT_MONO` to a Unicode TTF
  (the tool warns you). Or just turn text OFF for those.

## What it does automatically
- rejects junk media (black / blurry / duplicate / low-res)
- syncs every cut and every text to the real narration timing
- clips 2-5s carry the story; images 3-7s with Ken Burns + human camera drift
- J/L cuts, punch-ins, sentiment color grade per scene mood
- text: dense first minute, then crucial moments only (names/numbers/danger
  words highlighted), always inside the frame, never overlapping
- 10 distinct formats so bulk videos never look repeated

## CLI (same engine)
```
python prostudio.py --scenes DIR --audio narration.mp3 --out video.mp4 \
    --format F2 --niche "Movie Essay" --language en --resolution 4K
python prostudio.py --queue jobs.json
```

Docs for future changes: `HANDOFF/`.
