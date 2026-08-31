# VText — Master Prompt: Clean Script → Text Instruction File

> **How to use (for the creator):** Copy everything below the line into a new
> Claude chat, then paste your CLEAN SCRIPT at the bottom where marked. Claude
> will return a `.txt` instruction file. Save it and upload it to VText along
> with the video and the clean script. Do not edit the format by hand unless
> you keep the field names exactly as they are.

---

You are an **editorial director for on-screen kinetic typography** in
YouTube video essays (the style used by top movie-essay and documentary
channels: short bold phrases that pop onto the screen in sync with the
narration, with one accent-colored keyword, placed in empty areas of the
frame).

I will give you the CLEAN NARRATION SCRIPT of my video. Your job is to
produce a **TEXT INSTRUCTION FILE** — a precise, machine-parseable plan of
every on-screen text moment. A rendering tool (VText) will read your file,
force-align my script to the video's audio, and composite the texts
automatically. The tool decides position, font, size, color shades,
animation and duration on its own — **you only decide WHAT text appears,
WHEN (by quoting the narration), and WHY (by tagging the moment).**

## OUTPUT FORMAT — follow it EXACTLY

Return ONLY the instruction file, no commentary before or after. Structure:

```
=== VTEXT INSTRUCTION FILE v1 ===
VIDEO_TITLE: <short title>
NICHE: <one of: CARTOON_ESSAY | MOVIE_ESSAY | CLASSIC_MOVIE | SITCOM_ESSAY | DARK_PSYCHOLOGY | HISTORY_DOC | TRUE_CRIME | SPORTS>
LANGUAGE: <two-letter code, e.g. en>
TOTAL_EVENTS: <number of EVENT blocks below>

--- EVENT 001 ---
NARRATION_CUE: "<5-12 words copied VERBATIM from the clean script>"
EVENT_TYPE: <one tag, see list>
DISPLAY_TEXT: <line one / line two / line three>
EMPHASIS_WORDS: <words to color-accent, must appear inside DISPLAY_TEXT>
INTENSITY: <HIGH | MEDIUM | LOW>
TEXT_ROLE: <IMPACT | INFORMATION | EMOTION | CONTEXT | TRANSITION>
VISUAL_FREEDOM: <LOW | MEDIUM | HIGH>
SEQUENCE_GROUP: <NONE, or a 2-digit id shared by related events, e.g. 04>

--- EVENT 002 ---
...
```

### Field rules (the tool parses these mechanically — no deviations)

1. **NARRATION_CUE** — the single most important field. Copy a contiguous
   phrase of **5-12 words, VERBATIM, letter-for-letter** from the clean
   script (punctuation may be dropped, words may not change). The tool
   string-matches this cue against the force-aligned audio to find the exact
   timestamp — a paraphrased cue breaks the sync. The text appears when the
   FIRST word of the cue is spoken. If the phrase occurs more than once in
   the script, extend it with more words until it is unique.
2. **EVENT_TYPE** — exactly one of:
   `HOOK, REVELATION, EMOTIONAL_PEAK, CONTRAST, QUESTION, IMPORTANT_FACT,
   NUMBER_OR_DATE, CHARACTER_INSIGHT, QUOTE, CHAPTER_TRANSITION, SETUP,
   NORMAL_EXPLANATION, BREATHING_MOMENT`
3. **DISPLAY_TEXT** — what the viewer reads. `/` = line break.
   - 1-4 lines, 1-4 words per line, **max 9 words total**
   - Title Case (the tool restyles capitalization per niche)
   - Condense, don't subtitle: narration "He was burned out from constantly
     cleaning up everyone else's mistakes" → display `Not Angry. / Burned Out.`
   - Numbers/dates stay as digits: `1985`, `14 October 1066`
   - For BREATHING_MOMENT write exactly: `DISPLAY_TEXT: NONE`
4. **EMPHASIS_WORDS** — 1-4 words that get the accent color. Must be an
   exact substring of DISPLAY_TEXT. Usually the payoff words (`Burned Out`,
   `About To Die`, `1985`). Write `NONE` only for BREATHING_MOMENT.
5. **INTENSITY** — how hard the moment should hit: HIGH (hook, twist,
   revelation), MEDIUM (facts, insights), LOW (setup, context).
6. **TEXT_ROLE** — what the text is doing: IMPACT (punch), INFORMATION
   (fact/data), EMOTION (feeling), CONTEXT (orienting), TRANSITION (chapter).
7. **VISUAL_FREEDOM** — how much creative liberty the renderer gets:
   LOW (dates, quotes, facts — keep it clean), MEDIUM (default),
   HIGH (hooks and peaks — renderer may go bigger/bolder).
8. **SEQUENCE_GROUP** — when 2-3 texts form one thought (a CONTRAST pair like
   `Not Angry.` → `Burned Out.`, or a build-up), give them the same 2-digit
   id so the tool styles them as one visual sentence (second text replaces
   the first, same zone, same family). Otherwise `NONE`.

## EDITORIAL RULES (how to choose the moments)

- **Hook phase (first 45-60 seconds of narration): dense.** One event roughly
  every 4-7 seconds of speech (~every 12-20 words). This is where retention
  is won.
- **After the hook: selective.** Only genuinely crucial moments — names,
  numbers, dates, danger words, reversals, revelations, chapter turns.
  Target one event per 25-40 words. Leave gaps of at least ~6 seconds of
  speech between events (≥ ~18 words between consecutive cues).
- **Never go more than ~90 words without any event** — if a stretch is
  quiet, add a small LOW-intensity SETUP/CONTEXT text or an explicit
  BREATHING_MOMENT marker.
- **BREATHING_MOMENT** — mark sections that must stay completely clean
  (emotional scene playing out, montage, music moment). The tool renders
  NOTHING there and will not auto-insert refresher texts inside it.
- **CONTRAST is your sharpest weapon** — whenever the script pivots
  ("Most people think X. That reading is wrong."), make it a two-event
  SEQUENCE_GROUP.
- **Don't caption dialogue** — if the narration quotes a character, use
  EVENT_TYPE QUOTE sparingly and only for iconic lines.
- **CHAPTER_TRANSITION** — at every major section turn of the essay
  (the tool renders these bigger, may use a semi-title-card treatment).
- Total events for a typical 10-minute script: **35-60**. Never exceed 8
  events per minute anywhere.

## SELF-CHECK before you output (do this silently)

1. Every NARRATION_CUE is verbatim, contiguous, 5-12 words, and unique in
   the script.
2. Cues appear in script order, no two events share overlapping cues.
3. Every EMPHASIS_WORDS string appears exactly inside its DISPLAY_TEXT.
4. No DISPLAY_TEXT exceeds 4 lines / 9 words.
5. TOTAL_EVENTS matches the number of blocks.
6. Field names, spelling and `--- EVENT NNN ---` separators are exact.

## WORKED EXAMPLE (from a Breaking Bad essay script)

Script excerpt: *"A man walks into a room where two people think they're
about to die. He doesn't look at them. He doesn't say a single word."*

```
--- EVENT 001 ---
NARRATION_CUE: "a room where two people think they're about to die"
EVENT_TYPE: HOOK
DISPLAY_TEXT: Two People / Think They're / About To Die
EMPHASIS_WORDS: About To Die
INTENSITY: HIGH
TEXT_ROLE: IMPACT
VISUAL_FREEDOM: HIGH
SEQUENCE_GROUP: NONE

--- EVENT 002 ---
NARRATION_CUE: "he doesn't say a single word"
EVENT_TYPE: CHARACTER_INSIGHT
DISPLAY_TEXT: He Doesn't Say / A Single Word
EMPHASIS_WORDS: A Single Word
INTENSITY: MEDIUM
TEXT_ROLE: IMPACT
VISUAL_FREEDOM: MEDIUM
SEQUENCE_GROUP: NONE
```

Example of a CONTRAST pair with a sequence group, from *"Most people watch
this scene and read it as rage. … That reading is wrong."*:

```
--- EVENT 003 ---
NARRATION_CUE: "watch this scene and read it as rage"
EVENT_TYPE: CONTRAST
DISPLAY_TEXT: They Read It / As Rage
EMPHASIS_WORDS: Rage
INTENSITY: MEDIUM
TEXT_ROLE: EMOTION
VISUAL_FREEDOM: MEDIUM
SEQUENCE_GROUP: 03

--- EVENT 004 ---
NARRATION_CUE: "that reading is wrong and it misses"
EVENT_TYPE: REVELATION
DISPLAY_TEXT: That Reading / Is Wrong
EMPHASIS_WORDS: Wrong
INTENSITY: HIGH
TEXT_ROLE: IMPACT
VISUAL_FREEDOM: HIGH
SEQUENCE_GROUP: 03
```

---

## MY CLEAN SCRIPT

NICHE: <write your niche here, e.g. MOVIE_ESSAY>

<PASTE YOUR CLEAN SCRIPT HERE>
