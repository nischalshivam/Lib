# Movie Essay Editor — Complete State & Handoff for Analysis

This document is written to be handed to another model (GPT) for a fresh,
honest analysis of what to do next. It describes exactly what the tool is,
every major thing that has been built and fixed, what is measured to work,
what is measured to fail, and the one core problem that remains unsolved.

Nothing here is aspirational. Every number is from a real build.

---

## 1. What the tool is

A local, offline tool for faceless YouTube video essays (movie/series
analysis). The user owns the episodes on disk. They write a narration
script and record a voiceover. The tool's job:

> Given a narration and the local episodes, automatically place the RIGHT
> clip/still under each sentence, so the finished video shows what the
> narration is talking about.

It is **not** a stock-footage tool. Accuracy means: the footage on screen is
the exact moment the narration describes.

## 2. The three input files

1. **Clean narration script** (`.txt`) — what the voiceover says. Source of
   truth for timing.
2. **Clue script** (`.json`, schema `clue-1`) — written by Claude/GPT from
   the clean script. For each scene it recalls the **dialogue** spoken
   (never a timestamp). Its whole purpose: dialogue can be looked up in the
   local subtitles and become an exact anchor; a recalled timestamp cannot.
3. **Visual script** (`.json`) — written by Genspark. Beats → shots, each
   shot with `season_episode`, `scene_range`, `exact_dialogue`/
   `nearest_dialogue`, `visual` description, `characters`, `duration`.

## 3. The full pipeline (what happens on a build)

In order, from `runner.run_job`:

1. **Clue script enrichment** (`clues.py`) — each recalled line is searched
   in the local subtitles. A found line becomes `exact_dialogue` on a shot;
   two bracketing lines bound a silent scene; a wrongly-recalled episode is
   corrected by where the line was actually found.
2. **Dialogue alignment** (`align.py`) — shots are grouped into RUNS by
   `(source, episode, scene-sequence)`. For each run, every quoted line is
   searched in the local subtitles of that episode. A found line = an
   **anchor**, exact to the millisecond (Tier A). Shots between two anchors
   are **interpolated** (Tier B). This is the workhorse and it is reliable.
3. **Picture verify** (`verify.py` + `embed.py`) — a local image model
   (SigLIP-base-patch16-224) scores each shot's `visual` description against
   sampled frames of the run, to move a shot onto a better-matching frame.
4. **Stated timings + clue windows** (`timings.py`) — a time the user typed,
   or a `scene_range` from the script, becomes a window nothing looks
   outside of. A typed window contradicted by a real quoted line is dropped,
   loudly.
5. **Picture placement / pacing** (`verify.py`) — shots with no anchor are
   placed by picture if it has an opinion, else **paced** in script order
   across the run's span.
6. **VLM refinement** (`refine.py` + `gemini.py`) — NEW. Every GUESSED shot
   (interpolated / paced / homeless) that has a window is handed to a vision
   model (Gemini 2.5 Flash): sampled frames + the shot's description →
   "which frame is this moment". A confident answer moves the shot onto that
   frame.
7. **Filler** — any shot still unplaced gets a still from somewhere in the
   right episode. This is "right episode, nothing more" — usually wrong.
8. **Timeline + render** — timing against the voiceover, then ffmpeg render.

## 4. The tiers (how much each placement is trusted)

- **Tier A** — a quoted line found in local subtitles, OR a time the user
  typed. Exact.
- **Tier B** — interpolated between two anchors, a picture match above the
  noise floor, or a frame the VLM chose.
- **Tier C** — right episode only, or paced/filler. No evidence of the
  moment.

Modes: **Balanced** (default) places A+B, fills C. **Strict** places only A,
everything else becomes a black "NEEDS VISUAL" card. **Draft** places all.

## 5. Everything built and fixed (chronological, with the bug each fixed)

Each of these was a real, measured failure found on a real build:

1. **`(beat, shot)` window keying.** Windows were keyed per beat, but 24 of
   34 beats draw from several episodes, so one episode's window overwrote
   another's. Fixed: windows keyed by `(beat, shot)`.
2. **Anchor clustering** (`align._densest`). A scene is contiguous, so
   correct anchors cluster and wrong ones scatter; keep the largest cluster.
3. **`timings.honour()`** — a typed window contradicted by a real quoted
   line is dropped.
4. **Evidence tiers + Strict/Balanced/Draft modes + NEEDS VISUAL cards.**
5. **GPU check** (`gpu.py`) — measures whether the card can actually run the
   model (a Quadro P1000 is sm_61; modern torch wheels ship no sm_61
   kernels, so `is_available()` lies — a real 64×64 multiply is run to be
   sure). Not accuracy-relevant; the tool runs on CPU.
6. **Clue script** (`clues.py`) — the whole third-input path.
7. **Clue lines spread over shots, once each** — a clue covering 10 beats
   was writing its 3 lines into all 10, so each line claimed 10 positions
   and the aligner threw them out (31 shots → 1 anchor). Fixed: each line
   placed once, spread across the scene's empty shots.
8. **THE BIG ONE — subtitle mis-linking** (`subtitles.py`). "Breaking Bad
   Season 4 Episode 1.mp4" was being indexed against "...Episode 13.srt"
   because the sidecar glob `stem + "*"` matches episodes 1, 10-13 and the
   tie-break preferred the largest file. Every quoted line was found — at a
   real millisecond of the WRONG episode — so the dashboard read "99% found"
   while the video was 95% wrong. This was the single biggest cause of bad
   builds. Fixed: a subtitle whose own episode number contradicts the
   video's is refused.
9. **One episode is not one scene** — S04E01's 31 shots were three different
   sequences (cold open, apartment, box cutter); as one run their anchors
   couldn't all increase in time, so line after line was dropped. Fixed:
   runs split by `scene_range` into scene-sequences.
10. **Default was Strict** — produced half-black videos by design. Changed
    default to Balanced.
11. **Gemini error surfacing** — every failure was swallowed into "no
    answer"; now the real HTTP status/error is shown, and a text ping +
    image ping isolate auth vs vision faults.
12. **Gemini given every guessed shot** — was only offered wide interpolated
    shots (20 of ~45 wrong ones on Hank); now offered all
    interpolated/paced/homeless shots down to a 30-second window.

Test suite: **772 tests, all passing.** But see §8 — none test semantic
correctness.

## 6. What is MEASURED to work

- **Dialogue anchoring is reliable.** When a shot quotes a line that exists
  in the local subtitles, it lands on the millisecond. Every "anchor" /
  Tier A shot is correct.
- **The subtitle fix worked.** After it, S04E01 went from 1 anchor holding
  31 shots to proper multi-anchor scenes.
- **Gemini works and is improving.** On the Hank build: 20 shots → 7 moved
  (before broadening), then 36 shots → 13 moved (after). The logged picks
  look correct (Walt driving the Aztek with Hank; the family dinner with all
  four at the table; the garage confrontation).
- **The "Gus" essay came out usable** ("theek theek") in Balanced mode.

## 7. What is MEASURED to FAIL — the core problem

The tool works well on essays that **walk through a few long scenes** (Gus:
the box-cutter sequence is one run with 7+ dialogue anchors, so interpolation
between them is short and right).

It works poorly on **"greatest hits" essays** (Hank: 25 beats across 15
different episodes, each a brief 2-6 shot reference). On this shape:

- Most runs have **one anchor or none**. From the Hank log: 8 of 16 runs had
  "no quoted line at all"; several had "only one line matched... everything
  else is placed relative to it".
- **Local picture search placed 0 of 21** shots that had no quoted line —
  SigLIP's best match did not beat its noise floor ("a caption about nothing
  scores 2.9 here, so 2.9 is what counts as found").
- So most shots are **interpolated across a wide span, paced, or filler** —
  right episode, wrong exact moment.
- **Gemini helps but cannot fully rescue it.** Measured: 36 shots offered,
  13 moved, 23 left. Final tally: **Tier A 27, Tier B 90, Tier C 0, 23%
  exact.** The video plays (91 segments, no black cards) but 78% is stills
  and, by the user's eye, only ~10-15% of shots show the right moment.

### Why Gemini leaves 23 of 36

This is the specific, unsolved mechanism. A shot's window is often the
script's `scene_range`, which is **5-8 minutes wide**. Gemini is shown **16
frames** across it — one every ~20-30 seconds. If the moment the narration
means is a 2-3 second beat, **the right frame is often not among the 16
sampled**, so Gemini honestly abstains or picks a "close enough" frame that
is still wrong. The bottleneck is no longer "does the model understand the
picture" — it is "were the right frames even shown to it".

## 8. The honest gaps

1. **No gold set / no measurement.** There is no human-labelled set of
   "this shot is right / wrong", so every accuracy % is a guess and no
   change can be proven to help. 772 tests all check mechanics, none check
   whether a placed clip is correct.
2. **Frame sampling is too sparse for wide windows** (§7). The VLM can only
   choose among frames it is shown.
3. **No real face recognition** — characters are matched by caption text,
   not by detecting faces.
4. **Silent/action beats** (a bell rings, a book is opened) have no dialogue
   and weak picture signal; they depend entirely on the VLM.

## 9. Open questions for analysis

1. Given local picture search (SigLIP) contributes almost nothing (0/21 on
   Hank, 2/354 on earlier builds), should it be **replaced entirely** by the
   VLM as the primary visual retriever, rather than used as a pre-filter?
2. For a wide window, is the answer **hierarchical**: VLM coarse-pass over
   frames 30s apart to pick a ~1-minute region, then a dense second pass
   (frames 1-2s apart) inside that region? This directly attacks the §7
   bottleneck.
3. Should the tool **restrict itself to the essay shapes it is good at**
   (dense-scene) and honestly refuse/flag greatest-hits scripts, rather than
   place-everything-badly?
4. Is a per-shot VLM call over a dense contact sheet cheap enough to make the
   VLM the primary retriever for **every** shot (cost per Gemini-Flash call
   is fractions of a cent; a 60-shot video is <$0.50)?
5. What is the minimum gold set to make progress measurable — and should it
   be built before any more solver changes?

## 10. Two honest directions

**A. Finish the current architecture.** Dialogue is the fast-path for shots
that quote a findable line; the VLM becomes the primary placer for the rest,
with a hierarchical coarse→dense frame search to fix §7. Add a gold set to
measure. This is a bounded amount of work, not open-ended — the failure is
now a single, named mechanism (sparse frames over wide windows).

**B. Change direction.** If the target is "any essay, fully automatic, 99%",
the honest truth is that a greatest-hits essay referencing 15 episodes in
brief may need a fundamentally retrieval-heavy design (dense per-shot VLM
search, or a pre-built scene/shot index of the whole series with
descriptions), which is a larger build than the current dialogue-first one.

The question for GPT: given §7 is the specific bottleneck and §6 shows
dialogue + VLM already place the anchored and dense-scene shots correctly,
is the coarse→dense VLM search (option A) sufficient, or is a full
series-level visual index (option B) required?
