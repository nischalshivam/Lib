# Copyright-Safety Mode (ProStudio)

**Why:** YouTube Content ID matches *continuous copyrighted video/audio*. The more
uninterrupted show footage is on screen, the higher the claim risk (as seen with
the Warner Bros. visual claim). Still frames of the same scene carry almost no
Content-ID video-match risk, and short clips (≤5s) keep any matched segment tiny.

## What it does (ON by default)

Built into `prostudio/engine/planner.py` → `plan_shots()`.

- **First 60 seconds (the hook):** clips carry it — full retention, no change.
  The opening minute is where viewers decide to stay, so it stays clip-heavy.
- **After 60 seconds (safe zone):**
  - **~70% of screen time = STILL images** of the **exact scene being discussed**.
    If a scene folder has no separate images, the tool freezes distinct frames
    from that scene's own matched clips — so the still is always the right moment,
    not a look-alike.
  - **~30% = video clips**, and **every clip is capped at ≤5s** (already enforced
    everywhere; the safe zone also limits how many clips share a scene).
- The whole-video mix is printed in the render log, e.g.
  `copyright-safe ON: first 60s clip-heavy, then ~70% stills. Whole video:
  64% still / 36% clip on screen (…; every clip <= 5s)`.

## Knobs (environment variables, optional)

| Variable | Default | Meaning |
|---|---|---|
| `PS_SAFE` | `1` | `0` turns safety OFF (old clip-heavy behaviour). |
| `PS_IMG_SHARE` | `0.70` | Target still-image share in the safe zone (0.60–0.75). |
| `PS_HOOK_SECS` | `60` | How many opening seconds stay clip-heavy. |

Example (even safer, 75% stills, 45s hook):
`set PS_IMG_SHARE=0.75` and `set PS_HOOK_SECS=45` before running the tool.

## Nothing else to do

- No script/clue change needed — it works on every build automatically.
- Audio sync, scene order, character matching, formats: all unchanged.
- The still frames get Ken Burns motion (slow zoom/pan) so the video never looks
  static despite being image-heavy.

## Rule of thumb for max safety

1. Keep the strong hook in the first minute (clips OK there).
2. Let the body run image-heavy (default).
3. If a specific video still gets a claim, raise `PS_IMG_SHARE` to `0.80` for that
   one and re-render — more stills, less matched motion.
