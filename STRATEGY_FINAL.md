# Final Strategy — the one plan the tool is built around

## Evidence status (read first — corrects the earlier over-claim)

An earlier version of this file stated "Gus = 100% usable, Hank = ~15%" as if
both were gold-measured. That was not honest and GPT was right to flag it.
The accurate position:

- **Gus:** the user labelled a Gus build by hand (screenshot). Reconstructing
  those labels gives ~100% *usable* (exact+ok) with 0 wrong — but this needs
  the user's own `mi gold --score` output + the labelled `gold.csv` to stand
  as evidence. Until that raw output is attached, treat it as *indicative,
  not proven*.
- **Hank:** "~15%" is the user's eyeball estimate. It has **not** been
  gold-labelled. It is not a measured number.

No claim of "100% usable", "works for every essay", or "fully automatic" is
made until a frozen, human-labelled set proves it.

## The product promise (this is the GOAL, not the current state)

> Every video is watchable. Where the tool is sure, it places the exact
> clip. Where it is not, it places a clean still of the character or scene
> the narration is talking about — from the same movie. It never fills the
> timeline with wrong moving footage.

**Current state does NOT yet meet this.** Today, a guessed (interpolated /
paced) shot that the vision model does not verify still ships as a moving
clip in Balanced mode. Closing that gap — unverified motion must become a
verified still or a card, never confident wrong motion — is the very next
work, ahead of everything else (see "Build order").

This is the user's Option 2, and it is what GPT's recovery strategy also
concludes. It works for every kind of essay the user makes:

- **"Why Jesse keeps choosing pain"** (one character across the whole
  series) → when the exact moment isn't found, a clean Jesse still keeps the
  right face on screen.
- **"The cruelest thing Walt ever said"** (one specific scene) → the quoted
  line anchors it exactly. Already works.
- **"Why Gus killed Victor"** (one long scene) → already 100% usable.

## The four-layer placement, per beat

Tried in order; the first that succeeds wins:

1. **Located clip (dialogue).** A line of dialogue from the beat is found in
   the local subtitles → the exact millisecond *of the line*. This is a
   **locator, not proof** — it does not by itself prove the required
   character is on screen, that the speaker is visible, that the action is
   happening then, or that it is not a recap / offscreen line. A true Tier A
   needs: locator **+** correct occurrence **+** required character verified
   **+** requested action verified **+** the exported 4–6s crop verified.
   Today the tool has the locator only; the rest is P2/P3. So a dialogue
   match is currently "located", not "verified".
2. **Located clip.** No line, but the character/scene is known → Gemini
   locates the exact moment inside the local movie by a **coarse→dense frame
   search** (wide sample to find the region, then a dense sample inside it).
   This fixes the "16 frames across 8 minutes miss the 2-second moment"
   problem. (Tier B.) *— to build (P2)*
3. **Character / scene still.** Exact moment not found, but the character IS
   known → Gemini picks a clean, sharp still of that character (or the
   location) from the local movie. A right face beats wrong motion. (Tier C
   — safe.) *— to build next (P1)*
4. **NEEDS VISUAL card.** Even the character is unknown → an editor card.
   Only here, and rarely.

## Where the pixels come from — decided

- **Local movie files ONLY.** Lawful, high quality, reproducible, and they
  contain exactly the scene the essay is about.
- **NOT YouTube / yt-dlp as the source.** GPT analysed this and rejected it,
  and this project agrees: YouTube search finds titles, not exact frames;
  competitor clips are cropped/subtitled/watermarked; copyright and API
  policy forbid downloading/re-cutting; famous clips repeat. YouTube stays
  **optional**, only ever to discover a scene's name or a licensed asset —
  never as the footage that ends up in the video.
- **Gemini API = the brain, not the source.** The user's key locates and
  verifies; the pixels always come from the local movie. A **browser-
  automation hack of the Gemini Pro website is explicitly not built** — it is
  fragile, breaks constantly, and violates terms. The proper API (already
  connected) does everything needed, including custom frame-rate and clipping
  video understanding.

## What makes a movie usable by the tool

One folder per title, containing:

- the movie file (`.mp4`, `.mkv`, `.avi`, `.mov`, …), and
- a subtitle file with the **same name** (`.srt`, `.vtt`, `.ass`).

Example:
```
D:\Movies\Joker (2019)\
    Joker (2019).mp4
    Joker (2019).srt
```

In the tool: **Library → Add title → paste that folder's path**. It reads
the subtitles (fast), then reads the frames for picture search (slow, one
time). After that every video about that movie uses it for free. Subtitles
are what make dialogue anchoring — the tool's strongest signal — work, so a
movie without an `.srt` will be much weaker.

## Build order (measured at each step against the gold set)

- **P0.5 — Fail-closed runtime (FIRST, before P1).** GPT is right that the
  promise is violated while guessed motion still ships. So the immediate
  change: a shot placed by interpolation/pacing that the vision model did
  **not** verify must not render as a confident moving clip. It becomes a
  still (a frozen frame is honest — "roughly this scene" — not a claim of the
  moment) or, if even the scene is unknown, a card. This holds the promise
  without spamming black cards, and it lands before any new placement work.

- **P1 — Character-still safety net, WITH identity verification.** GPT
  correctly caught the circularity: a still cannot be called "safe Jesse"
  without confirming it is Jesse. So P1 is not a blind Gemini pick. It
  requires, at minimum: **user-provided reference portraits per character**
  (the tool's existing Characters folder), a face/quality filter, an
  explicit *unknown → reject → NEEDS VISUAL*, a sharpness/no-subtitle filter,
  the source timestamp stored, and a repeat limit. A candidate still is only
  placed when it is confirmed to be that character against the references.

- **P2 — Hierarchical exact-clip location.** Not sparse coarse frames alone —
  GPT is right that a coarse pass can also miss a 2-second moment. Full flow:
  scene/shot boundaries → subtitle/face/action/location candidates → top-K
  diverse regions → medium video pass → dense 3–5 FPS pass → FFmpeg crop →
  Gemini final-crop verification, with a mandatory **NONE OF THESE**.

- **P3 — Full face tracking.** Multi-frame track-level identity; makes the
  still bank and the required-character filter fully reliable.

Face-name captions today are not face recognition. Minimum identity
verification (reference portraits + match + reject) is required for P1;
full tracking is P3.

## Input authority (GPT's point, adopted)

Genspark's visual script is a **proposal**, not truth — prior audits showed
it gets speaker, character, range and chronology wrong. Every request carries
a status: `VERIFIED` (locally grounded) / `SUPPORTED` (consistent, not
proven) / `UNVERIFIED` / `CONTRADICTED` (local evidence disagrees). Authority
order: clean narration = truth; clue script = clues; Genspark = proposal;
local subtitles/frames = evidence. A `CONTRADICTED` request is never forced —
it abstains to fallback.

## How progress is proven from here

The gold set is the answer key. After each change the same labelled videos
are re-scored, so "better/worse" is a measured delta, never a guess. The
user never labels again — that was one-time, developer-side. Production
videos are fully automatic: script + clue in, video out.
