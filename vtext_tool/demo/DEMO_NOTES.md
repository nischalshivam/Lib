# Text Overlay Tool — proof-of-concept notes (TOOL #3/finishing tool)

Goal: user uploads (1) a finished/near-finished video, (2) the clean narration
script, (3) an LLM-written overlay instruction script → the tool composites
competitor-style kinetic text overlays (yellow/white/gray Poppins, line-by-line
pop-in, placed in empty frame areas) onto the video. Audio and the edit itself
stay untouched.

## What the demo proved (2026-07-18, in-session test on a 60s Gus Fring clip)

Pipeline used by `demo_render.py` (event times/positions hardcoded for the
demo clip; the real tool derives them):

1. **Timing — forced alignment, not free transcription.** The clean script is
   force-aligned to the extracted audio (demo: pocketsphinx `set_align_text`,
   because the sandbox blocks HuggingFace; production should prefer
   faster-whisper word timestamps + script alignment, same as prostudio's
   `audio_sync`, with pocketsphinx alignment as an offline fallback).
   Result: every script word got an exact start/end time; all 12 overlay
   events landed on the spoken word. This kills the classic failure mode
   "text says X while narration says Y".
2. **Zero text mistakes by construction.** Display text is taken from the
   script/instruction file, never from ASR output — ASR is used only for
   timing, so transcription errors cannot reach the screen.
3. **Style replication.** Poppins ExtraBold (Google Fonts TTF fetch worked via
   the CSS API with an old User-Agent; bundle the TTFs in production),
   colors: white #FFFFFF / yellow #FFD60A / dim gray #9E9E9E, soft gaussian
   shadow, per-line pop-in (scale 0.70→1.0 cubic ease-out over 0.20s, alpha in
   over 0.12s), whole-event fade-out 0.25s. Stacked left- or right-aligned
   lines, mixed sizes (34–50px at 854×480 — scale with resolution).
4. **Rendering approach that worked well:** pre-render each text line once as
   RGBA (PIL), then per-frame scale/alpha composite into transparent PNG
   frames (hardlink a shared blank for empty frames), single ffmpeg pass:
   `overlay` + `-c:a copy` (audio untouched), libx264 crf 18.
5. **Placement** was chosen manually per shot for the demo (frames extracted
   at 0.5 fps and inspected). Production: automatic zone scoring — face
   detect (OpenCV, exists in prostudio `subjects.py`) + brightness/edge-density
   maps → place text in quiet/dark negative space; keep 5 % safe margins.

## Agreed instruction-file format (from user's GPT/Gemini research)

Per event, 8 fields; the tool decides position/font/size/color/animation/
duration itself:

```
NARRATION_CUE:   "But Benson wasn't angry. He was burned out."
EVENT_TYPE:      CONTRAST   # HOOK, REVELATION, EMOTIONAL_PEAK, CONTRAST,
                            # QUESTION, IMPORTANT_FACT, NUMBER_OR_DATE,
                            # CHARACTER_INSIGHT, QUOTE, CHAPTER_TRANSITION,
                            # SETUP, NORMAL_EXPLANATION, BREATHING_MOMENT
DISPLAY_TEXT:    NOT ANGRY. / BURNED OUT.       # "/" = line break
EMPHASIS_WORDS:  BURNED OUT                      # rendered in accent color
INTENSITY:       HIGH | MEDIUM | LOW
TEXT_ROLE:       IMPACT | INFORMATION | EMOTION | CONTEXT | TRANSITION
VISUAL_FREEDOM:  LOW | MEDIUM | HIGH
SEQUENCE_GROUP:  07        # events in one group share treatment
```

BREATHING_MOMENT ⇒ render nothing. NARRATION_CUE is matched against the
aligned script to get the timestamp (no manual timestamps needed).

## Planned architecture (approved direction)

- Niche presets (cartoon essay / movie essay / classic movie / sitcom / dark
  psychology / history / true crime / sports …) set the typography DNA;
  EVENT_TYPE sets behavior; niche sets look. 70 % consistent, 30 % variable;
  repetition memory over last 5–10 events (vary position/animation).
- Shot-cut protection: don't let a text float across an unrelated cut
  (demo's last event crosses a cut at ~58.8s — visible, acceptable, fixable).
- GUI + queue like prostudio (`gui.py`), Windows-first (`run.bat`/`setup.bat`),
  reuse prostudio engine pieces: `audio_sync`, `textlayout`, `subjects`, QC.
- Bundle fonts (Poppins/Montserrat ExtraBold + DejaVu fallback).

Demo output was delivered to the user in-session (gus_text_demo.mp4); the
uploaded source clip and renders are not committed.
