# VText — User Controls, Automatic Decisions, Variation Space

This is the agreed product spec for the VText finishing tool: what the
creator controls, what the engine decides, and how many distinct treatments
the engine can produce.

## 1. What the user gives the tool (per video)

| # | Input | Required | Notes |
|---|-------|----------|-------|
| 1 | Final video file (MP4/MOV, any size) | ✅ | The edited video that only needs text |
| 2 | Clean script (.txt) | ✅ | Exact narration text — used for forced alignment |
| 3 | Text instruction file (.txt) | ✅ | Generated with `PROMPTS/SCRIPT_TO_INSTRUCTIONS_PROMPT.md` |

## 2. Manual controls (Simple mode — the whole main screen)

| Control | Options | Default |
|---------|---------|---------|
| Niche preset | Cartoon Essay, Movie Essay, Classic Movie, Sitcom Essay, Dark Psychology, History Doc, True Crime, Sports | Movie Essay |
| Typography pack | Auto (from niche), Bold Geometric, Modern Clean, Editorial Serif, Classic Cinema, Condensed Impact, Playful Premium | Auto |
| Accent color | Auto-from-video, Niche default, Custom picker | Niche default |
| Motion energy | 1 Very Subtle → 5 High Energy | Niche default (2.5-3.5) |
| Text scale | Small, Balanced, Large, Auto | Auto |
| Text density | Follow instruction file, Cap at Medium, Cap at Light | Follow file |
| Brand lock | ON / OFF (fonts + accent stay identical across queue) | ON |
| Output | 1080p / 4K, quality preset | 1080p |
| Queue | Up to 15 videos, each with its own 3 inputs | — |

## 3. Manual controls (Advanced mode — collapsed by default)

Safe margins %, max lines per event, min/max hold duration, capitalization
style (Title Case / ALL CAPS / sentence), face-avoidance ON/OFF, shot-cut
protection ON/OFF, readability treatment override (auto/shadow/stroke/plate),
animation family allow-list, custom font upload (.ttf/.otf), custom
accent + base colors, repetition-memory window size.

## 4. What the engine decides automatically (never asked)

1. **Exact timestamp of every text** — clean script force-aligned to the
   audio (faster-whisper word timestamps → pocketsphinx alignment fallback);
   NARRATION_CUE matched against the aligned words. Entry synced to the
   spoken word; one of 4 timing modes chosen by EVENT_TYPE
   (Word-Hit, Phrase-Build, Pre-Reveal, Post-Impact).
2. **Position** — 9-zone grid scored per event from the actual frames:
   face detection, brightness map, edge density (busyness), existing
   text/logo detection → text goes to quiet negative space, never on faces.
3. **Font size + line breaks** — from text length, resolution, available
   zone size; semantic line breaking (no orphan words).
4. **Colors per word** — base/dim/accent split from EMPHASIS_WORDS; accent
   auto-derived or from preset; WCAG-ish contrast check against the local
   background region.
5. **Readability treatment** — none / soft shadow / thin stroke / dark
   plate / gradient, chosen from local background busyness + luminance.
6. **Animation variant + direction** — from EVENT_TYPE behavior × niche
   energy × repetition memory; enter direction away from the subject.
7. **Hold duration** — reading-time model (word count, INTENSITY, QUOTE
   bonus), clamped by the next shot cut and the next event.
8. **Shot-cut protection** — text exits or re-anchors instead of floating
   across an unrelated cut.
9. **Repetition memory** — last 8 events' zone/animation/scale/accent are
   remembered; the engine never repeats the same combination back-to-back
   (70% brand-consistent, 30% variable).
10. **QC pass + report.json** — every event: on-screen? inside frame? over a
    face? contrast OK? timing drift? — written next to the output video.

## 5. Variation space (what "different every time" actually means)

| Dimension | Count |
|-----------|-------|
| Niche presets | 8 |
| Typography packs | 6 (+ custom upload) |
| Event types (behavior recipes) | 13 |
| Animation families | 7 (Fade-Rise, Scale-Pop, Mask Reveal, Slide-In, Word-Build, Replace/Swap, Static-Hold) |
| Energy levels per family | 3 → **21 motion variants** |
| Placement zones | 9 (dynamically scored) |
| Timing modes | 4 |
| Readability treatments | 5 |
| Text scales | 3 |
| Accent modes | 3 sources |

**Per single text event:** 21 × 9 × 4 × 5 × 3 = **11,340 distinct
treatments** before color/niche multipliers.
**Across the tool:** × 13 event types × 8 niches ≈ **1.18 million** distinct
(event-type, niche, treatment) combinations.
Within one video the repetition memory guarantees no two consecutive texts
share a treatment, while brand lock keeps fonts/accent consistent — that is
the 70/30 premium look.

## 6. Pipeline (build order)

```
inputs → parse instruction file → extract audio → force-align clean script
→ resolve NARRATION_CUEs to timestamps → sample frames around each event
→ zone scoring (faces/brightness/edges) → style resolve (niche × event ×
memory) → render RGBA overlay frames (PIL) → ffmpeg composite (-c:a copy)
→ QC report → output MP4
```

Proven in-session on a 60s Breaking Bad clip: see `demo/DEMO_NOTES.md` and
`demo/demo_render.py`.
