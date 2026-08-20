# Changelog — what Claude changed (for GPT to review)

This file is kept up to date so it can be handed to GPT. Each entry says
what changed, why, and — where it can be honestly measured — how much it
helped or hurt. Newest first.

The single most important honest note: until the **gold set** (below) is
filled in by a human, every "% better/worse" is an estimate from logs, not a
measured fact. That is exactly the gap GPT's recovery strategy calls out,
and the gold evaluator is the first thing built to close it.

---

## 2026-08-03 — BREAKTHROUGH: the multi-episode "greatest hits" case works

The case that failed for weeks — a Hank/Gus-type essay whose shots are
scattered across many episodes of a series — now resolves correctly. Setup:
Breaking Bad Seasons 3+4 fully catalogued (**15,216 shots**), and the real
"Gus Fring's Wordless Kill" genspark script (36 beats, 75 shots spanning
S03E13, S04E01, S04E08, S04E11, S04E13) run through `mi plan` against the whole
`E:\Movies\Breaking Bad` folder.

Result: **75/75 shots placed, 37 by exact dialogue anchor**, and every shot in
the CORRECT episode (S04E01×31, S03E13×8, S04E08×13, S04E11×2, S04E13×21).
Spot-checked anchors land on the right second: "How's it coming?" on the cold
open (00:41), "Well? Get back to work." at the box-cutter scene's end (37:06),
"Look at him. You did this to him." in the S04E08 pool flashback, Gus's
tie-straighten at his S04E13 death. This is the whole thesis proven on real
data: episode + scene_range + exact_dialogue hints from the genspark script,
matched against a language-described catalogue, place footage accurately
across a series.

What made it work, in order of leverage: (1) per-shot **episode scoping** from
`season_episode` (stops a line matching the same words three episodes away);
(2) **dialogue anchoring** on `exact_dialogue` against the shots' own subtitle
text (37/75 pinned to an exact second); (3) **scene_range windowing**, now
carried across a whole run so description-only shots don't drift within their
episode (was ~11 drifting, now pinned).

Honest remaining: accuracy is eyeballed on anchors + episode distribution, not
yet a frozen gold pass; description-only shots inside a correct scene window
are "right scene", not verified "right frame"; and this is a shot-LIST, not
yet a cut video (Stage 3).

---

## 2026-08-02 — Stage 2: script → catalogue retrieval (plan.py), working on real data

With the Joker 15-min catalogue complete (descriptions + canonical characters
+ dialogue on 116/182 shots), the retrieval half is in. New `plan.py` matches
each shot-request from a visual script to a catalogued shot, precision-first:
(1) **dialogue anchor** — the request quotes a line and a shot's own subtitle
contains it (the exact "money moment"); (2) **description + character** — the
visual sentence matched against descriptions/tags, filtered to the named
person; (3) **none → NEEDS VISUAL** (fail-closed). `mi plan <script>
<catalog.json>` prints the shot list with the reason for each pick.

Run against the REAL Joker genspark script + the 15-min catalogue: 103/103
requests placed, and the sampled picks are genuinely right — the makeup-scene
shots resolved to 66–87s, "man in rust jacket walking down a Gotham street" to
574s, "old woman in a pink robe" (Penny) to 664s, "clown chasing teenagers
down an alley" to 184s. Honest caveat: "100% placed" ≠ 100% accurate — a
15-min catalogue forces some later-scene requests onto whatever is closest, so
the true number needs the FULL-movie catalogue and a gold pass. Structure and
signal are validated; accuracy is the next measurement.

Refactor: the stringified-list parser is now `catalog.list_entries`, shared by
`parse_tags` and `plan.requests_from_beats`. 7 new plan tests.

---

## 2026-08-02 — Catalog validated on real footage; character names canonicalized

First real run: Joker (2019), first 15 min, 182 scene-cut shots, 175 described
by Gemini 2.5 Flash. **Descriptions are genuinely strong** — the opening
dressing-room scene came back as "man in clown makeup applying white makeup at
a lit vanity", "forces his mouth into a wide painful smile with his fingers
while a tear runs down his face", shot_type correct (close-up/medium/wide),
and `safe=false` correctly caught a Warner Bros title-card overlay. The
approach is validated: language-vs-language beats embeddings on these shots.

Two rough edges fixed:
1. **Character labels were inconsistent** — the same man came back as "Joaquin
   Phoenix" (actor), "Joker" (persona), and "Arthur Fleck" (name) across three
   shots, which would fragment search. New `alias_map` + `canonicalize`
   collapse them to one canonical name from a `--characters` list (file or
   inline `Arthur = Arthur Fleck, Joker, Joaquin Phoenix; Murray = ...`), and
   that list also nudges the model to name only known people, else "unknown".
2. **Subtitle diagnostic** — dialogue came back empty (subtitle not matched at
   catalog time). `run` now lists the .srt files actually beside the video, so
   a present-but-unmatched subtitle is visible instead of a silent "none".

Also fixed a shipped bug: `real_grab` used `tempfile` without importing it, so
every frame grab failed on the first real run (0/182 described); the injected-
fake tests never touched `real_grab`. Two new tests now run the real body.

Still open: dialogue signal (subtitle matching at catalog time) and the
character *verification* pass against reference photos — both feed Stage 2
(wiring the catalogue into retrieval + build).

---

## 2026-08-02 — Catalog layer: the whole title becomes a searchable tagged library

**The strategic pivot.** After two over-engineered attempts (this tool's
precision solver + a separate "SceneBrain" repo) both failed to hit accuracy,
a working competitor's method was obtained (a friend's "Westeros Autopilot"
brief + a course): index the ENTIRE series ONCE into a searchable catalogue of
tagged shots, then every video reuses it. The engine is not clever — break the
source into short shots, have a vision model describe each (who / what / shot
type / clean?), store it beside the exact subtitle timing — and it works
because most of a character essay needs *a good shot of the right person in the
right mood* (many acceptable answers), not one exact frame. The few
"money moments" stay covered by dialogue anchoring. The honest reframe:
chasing frame-perfect exactness on EVERY shot was the mistake.

**Change.** New `media_index/catalog.py`:
- `shots_from_cuts` / `fixed_windows` / `detect_cuts` — segment a video into
  1.5–8s shots from ffmpeg scene-cut detection, falling back to even windows.
- `tag_messages` / `parse_tags` — ask Gemini to DESCRIBE a shot from real
  frames (description, tags, characters, action, shot_type, quality, safe),
  never to locate anything; "unknown" is required over a guessed name (names
  are claims for the later `cast.py` verification pass, per the friend's
  brief's "second check confirms the person is actually in the shot").
- `build_catalog` — the loop, with injectable frame-grab and `ask`, saved
  after EVERY shot (resume-safe / crash-safe over a 1500-shot film), dialogue
  attached from subtitle overlap. Output is `catalog.json` (the course's
  library.json schema): `{id, source, file, start, end, description, tags,
  characters, action, shot_type, quality, safe, dialogue}`.
- `real_grab` (best distinct frames via `frames.scan/pick`) + `gemini_ask`
  (reuses `gemini.call`) + `run` orchestrator with a `max_minutes` cap for a
  cheap quality check before tagging a whole film.
- `search` — lexical v1 retrieval over description+tags+dialogue with a
  decisive character filter. A description embedding is the next upgrade and
  slots in behind the same function.
- CLI `mi catalog <video> [--minutes N]` + `catalog.bat` (double-click).

**Why this is not a restart.** It reuses what already works — local movie as
the only source, subtitle timing, `frames` picker, `cast.py` character
verification, `gemini.py`. It adds the one missing retrieval signal (a
language description per shot) that CLIP embeddings could not provide on
silent scenes.

**Measured.** 16 new catalog tests (segmentation, tag-parse tolerance,
resume/crash-safety with injected fakes, search). Full related suite green
(65). Real accuracy awaits the user running it on Joker (2019) and a gold
pass — deliberately not claimed here.

**Still open (honest):** retrieval is lexical, not yet embedding-based;
character labels are Gemini claims not yet cross-checked against `cast.py`
reference photos at catalog time; and the catalogue is not yet wired into the
build/editor as the primary footage source (next step).

---

## 2026-08-01 — A dead indexer no longer locks a library for 30 minutes

**Change.** `lockfile.held_by` now checks whether the process that wrote the
lock is actually still running, and treats a lock owned by a gone process as
abandoned immediately (and tidies the file away). Cross-platform liveness
without disturbing the process: `OpenProcess`+`GetExitCodeProcess` on Windows
(never `os.kill`, which *terminates* on Windows — the reason this check was
avoided originally), `os.kill(pid, 0)` on POSIX.

**Why.** Real user report, and a bad one: a picture-index run was interrupted
(window closed / rebuild stopped), leaving a `.lock` whose 30-minute
heartbeat had not yet expired. Clicking **Update** on the title returned "is
library par pehle se 'pictures padhna (pid 28300)' chal raha hai" and refused
to do anything — for up to half an hour — even though the owning process was
gone. The user's words: "ek library update karna itna mushkil ho gaya."
Because Update (`force=False`) already *skips* frames that are current
(`visual.is_current`), the only thing standing between the user and a
one-second subtitle re-read was this ghost lock. Restarting the tool now
clears it at once (new pid, old pid dead → not held).

**Also clarified for the user (no code):** the index does NOT live on the
external SSD with the movie. `library.db` (subtitle/dialogue text) and
`<db>_visual/` (frame vectors, ~4 MB/1400 frames) sit next to the tool on the
internal drive; the movie file on E:\ is only referenced by path. So the
index survives unplugging the SSD, but a *build* needs the SSD connected
because frames are cut from the movie itself.

**Measured.** 2 new lockfile tests (dead pid → abandoned at once + tidied;
live pid → still held). Full lockfile suite green.

---

## 2026-08-01 — Genspark script now loads (curly delimiters + straight inner quotes) + P1 character detection

Two things, both foundation work the user asked for ("sabse pehle foundation
clear karo"), tested against the real Joker (2019) genspark script.

**1. The genspark script would not parse at all.** It used typographic
(curly) quotes as its JSON string *delimiters* but ordinary straight quotes
*inside* the text — `"narration": "You said "good" and smiled"`. `straighten`
converts the curly delimiters to straight quotes, which then collide with the
untouched `"good"`, and the file still failed to open. Every narration line
in the file broke this way, so the whole 27-beat script was unusable and the
Joker test could not even begin. Fix: `jobs._escape_inner_quotes` escapes the
straight quotes *first*, so they survive as content once the curly delimiters
become straight. `read_beats`/`script_extras` now try, in order: the untouched
file → `straighten` → escape-then-straighten, using the first that parses. A
valid file parses on the untouched try and is never touched by the repair
(test locks this in). Result: the real Joker genspark loads — 27 beats, 103
shots, summary + note extracted.

**2. P1 foundation — the tool now reads the script and says whose photos it
needs.** New `characters.py`: `needed(beats)` ranks the people a build will
need reference photos of, most central first, read straight from each shot's
`characters` field (with caption/narration text used only to *weight* names
already found — never to mint new ones, which would hallucinate). Wired into
`script_facts`, so the moment a script is chosen the New Video page shows
`cast\Arthur\`, `cast\Murray\` … with "~7 photos each". On the real Joker
script it correctly returns exactly Arthur (97 mentions) and Murray (25) —
the two faces this essay actually leans on. This closes the gap GPT/the user
flagged: the user no longer has to *guess* which cast folders to build; the
tool derives the list. The existing `cast.py` (embedding-based identity
match against those folders) is the engine that consumes them; identity
verification at placement time is the next step on top of this.

**Measured.** 6 new `characters` tests + 2 new script-repair tests, all green.
Model-free and offline — counting names the script already wrote cannot
invent a character who is not in the file.

**Still open (honest):** character detection reads *named* people; a face the
script only ever describes ("his mother") without a name is not offered yet
(alias-merge / model pass is later). And having the cast list is not yet
identity *verification* at placement — that is the P1b step this unblocks.

---

## 2026-08-01 — "subtitle present but empty" is now a distinct, honest state

**Change.** `subtitles.load_for_video` used to collapse two very different
situations into `"none"`: (a) no subtitle file exists, and (b) a subtitle
file sits right next to the video but parses to **zero** readable cues — the
classic broken ~1 KB download (an HTML error page or placeholder saved with
a `.srt` name). It now returns a new kind `"empty"` for case (b), carrying
the path of the file it found. `library.py` turns that into a precise
message: *"a subtitle file is present but has no readable lines — probably a
broken download (a real movie .srt is tens of KB, not ~1 KB); replace it and
re-index."* Also added a test proving a scene-release name with brackets —
`Joker.2019.1080p.WEBRip.x264-[YTS.LT].srt` — is still found by the sidecar
glob (`glob.escape` already handled it; the test locks it in).

**Why.** Real user report: a freshly downloaded Joker (2019) movie showed
"subtitle hai hi nahi" (no subtitle) even though a `.srt` named identically
to the `.mp4` was in the folder. The `.srt` was 1 KB — junk. The old message
sent the user looking for a missing file that was not missing. This is a
diagnosis fix, not a placement fix: it changes what the tool *says*, so the
user fixes the right thing (swap the broken .srt) in one step.

**Measured.** 2 new tests; full subtitle + web + queue suite green (105).

---

## 2026-08-01 — P0.5 fail-closed: a guess never ships as moving footage

**Change.** `runner.py`: a moving clip is now cut only for a placement whose
method is trusted — `anchor / stated / chosen / verified / vlm / picture`
(`MOTION_OK`). An interpolated, paced or filler guess no longer becomes a
moving clip; it is shown as a STILL (a frozen frame is an honest "roughly
this scene"; wrong motion is a confident lie). Stills still play with a slow
hold, so nothing goes black — the video just stops pretending a guessed
moment is real. This is GPT review point #2, implemented without reverting to
Strict / black cards.

**Effect.** Confident wrong MOTION can no longer ship. Guessed placements
survive only as stills, which are softer and, on a wrong-moment guess, far
less jarring. Trusted placements (dialogue-located, VLM-verified) still get
moving clips as before.

**Measured.** New test asserts every moving clip in a real build comes from a
MOTION_OK method and that an interpolated shot appears only as a still. 785
tests pass. The real precision delta will come from the next gold labelling
pass on a rebuilt video.

**Still open (honest):** a dialogue-located clip is still not fully verified
(character/action/crop) — that is P2/P3. And the still shown for a guess is
still from roughly-the-right scene, not yet a verified character still — that
is P1.

---

## 2026-08-01 — GPT review accepted; over-claims retracted

GPT's review of STRATEGY_FINAL.md + this changelog was correct on the
substance. Corrections made (STRATEGY_FINAL.md updated):

1. **Gus 100% / Hank 15% retracted as proven.** Gus labels were reconstructed
   from the user's screenshot (~100% usable, 0 wrong) but the raw
   `mi gold --score` output + labelled `gold.csv` are still needed to stand
   as evidence — treated as indicative, not proven. Hank "15%" is an eyeball
   estimate, NOT gold-labelled. No "100%/works for every essay/fully
   automatic" claim stands until frozen human labels prove it.
2. **"Never wrong footage" marked as GOAL, not current state.** Today, an
   unverified interpolated/paced shot still ships as a moving clip in
   Balanced. Fixing that (P0.5 fail-closed) now leads the build order.
3. **Dialogue match is a LOCATOR, not Tier A.** Strategy updated: a real
   Tier A needs locator + occurrence + required-character + action + final-
   crop verification. The tool has the locator only today.
4. **P1 circularity fixed.** Character-still fallback now requires minimum
   identity verification (user reference portraits + face/quality filter +
   unknown→reject), not blind Gemini picks. Full tracking stays P3.
5. **Gold should be per visual-request/shot, not per scene** — acknowledged;
   the per-scene sheet hides a 2-right-3-wrong scene under one "ok". Per-shot
   labelling to be added, plus dev/frozen/audit split.
6. **Input evidence statuses adopted:** VERIFIED / SUPPORTED / UNVERIFIED /
   CONTRADICTED; clean narration is authority, Genspark is a proposal.
7. **P2 is hierarchical retrieval,** not sparse coarse frames alone; NONE OF
   THESE mandatory.

Revised build order: **P0.5 fail-closed → P1 character-still with identity →
P2 hierarchical location → P3 face tracking.**

---

## 2026-08-01 — Final strategy decided (see STRATEGY_FINAL.md)

After the gold benchmark showed Gus = 100% usable / Hank = ~15%, the
architecture is locked to a **precision-first four-layer placement**: (1)
sure clip from dialogue anchor [works today], (2) Gemini locates the exact
moment in the LOCAL movie via coarse->dense frame search [P2], (3) clean
character/scene still from the local movie when the exact moment is not found
[P1, next], (4) NEEDS VISUAL card only if the character is unknown.

Decided and recorded: local movie files are the only footage source;
YouTube/yt-dlp is optional-only (copyright + quality + availability);
Gemini API is the brain, not the source; no browser-automation of the Gemini
website. Build order: P1 character-still safety net (next) -> P2 exact-clip
location -> P3 face recognition.

---

## 2026-08-01 — Gold benchmark & honest metrics (P0, per GPT's plan)

**Change.** New `media_index/gold.py` + `mi gold` command. It turns a
finished build's `manifest.json` into a labelling sheet (`gold.csv`), one row
per scene with the narration and what the tool placed. A person watches the
video once and writes a verdict per scene: `exact` / `ok` / `wrong` / `none`.
`mi gold --score gold.csv` then prints the only numbers that mean anything:

- **usable precision** = (exact + ok) / auto-placed
- **exact precision** = exact / auto-placed
- **coverage** = scenes filled / all scenes
- a **per-method breakdown** so a wrong "Tier B" can no longer hide inside a
  healthy-looking total.

**Why.** GPT's strategy §11 P0: *"Before solver changes, build a 40–50
request semantic gold set and evaluator. Never use placeable, moved,
rendered or non-black as accuracy."* This is that. Nothing here changes a
build — it measures one, and the solver may never again be tuned against a
number the solver itself produced.

**Measured.** 12 new tests. On the Gus-4 (Strict) manifest the evaluator
correctly separates 16 anchor-placed scenes from 20 declined (card) scenes.
Real accuracy numbers await the human labelling pass — that is the point.

**What the user must do:** run `mi gold --template <manifest.json>`, watch
the video, fill the `verdict` column, run `mi gold --score gold.csv`. That
produces the first honest accuracy number this project has ever had.

---

## 2026-08-01 — Vision model given every guessed shot

**Change.** `refine.py`: the VLM (Gemini) was offered only wide interpolated
shots. Now it is offered every GUESSED shot — interpolated, paced, and
homeless — down to a 30-second window, and a homeless shot it recognises is
rescued into a real placement instead of becoming filler.

**Measured.** Hank build: shots offered to the VLM went 20 → 36; shots moved
went 7 → 13. **But** the finished video was still poor by eye. This is the
evidence behind GPT's key point: the bottleneck is no longer how many shots
the model is *asked* about, it is that the right frame is often not among the
~16 sampled across a 5–8 minute window (retrieval recall, not model
intelligence). See GPT strategy §2.2 and §8.

---

## 2026-08-01 — Default mode changed Strict → Balanced

**Change.** New Video defaulted to Strict, which turns every non-dialogue
shot into a black card. A build came back >50% black cards with nothing
broken. Default is now Balanced.

**Note for GPT:** GPT's strategy §3 flags that Balanced can hide weak footage
inside a complete-looking timeline — this is correct, and the gold evaluator
above is what will expose it. The right end state (GPT §9) is a
precision-first ladder: exact clip → curated still → NEEDS VISUAL, never
wrong moving footage. That is the next architecture, not yet built.

---

## 2026-08-01 — Gemini (GPT/Gemini vision API) integration

**Change.** `gemini.py` + `refine.py`: OpenAI-compatible vision call, key
read from `settings.txt` (never committed). For silent/no-dialogue shots the
model is shown candidate frames of a window and picks the one matching the
shot's description. Graceful: not configured / network error / abstention
all leave the shot where it was. `mi gemini` diagnoses key + endpoint with a
text ping then an image ping, and shows the real HTTP error instead of
"no answer".

**Measured.** The picks the model logs look correct (e.g. Walt driving the
Aztek with Hank; the family dinner with all four at the table). Coverage
limited by the frame-sampling bottleneck above.

---

## 2026-07-31 → 08-01 — The correctness bugs that caused bad builds

Each was a real, measured failure, all now fixed and covered by tests:

1. **Subtitle mis-linking (the big one).** "…Season 4 Episode 1.mp4" was
   indexed against "…Episode 13.srt" (glob `stem + "*"` matched 1/10/11/12/13,
   tie-break preferred the largest file). Every quoted line was "found" at a
   real millisecond of the *wrong* episode — dashboard read 99% while the
   video was 95% wrong. Fixed: a subtitle whose own episode number
   contradicts the video's is refused.
2. **One episode ≠ one scene.** S04E01's 31 shots were three sequences; as
   one run their anchors couldn't all increase in time, so the solver
   dropped line after line (31 shots → 1 anchor). Fixed: runs split by
   `scene_range` into scene-sequences.
3. **Clue lines duplicated.** A clue covering 10 beats wrote its 3 lines into
   all 10, so each line claimed 10 positions and was thrown out. Fixed: each
   line placed once, spread across the scene's empty shots.
4. **`(beat, shot)` window keying**, **anchor clustering**, **contradicted-
   range rejection** — earlier fixes to the same family of "the window
   belonged to the wrong thing" bugs.

**Note for GPT:** the document `PROJECT_STATE_FOR_GPT.md` claimed "every
anchor/Tier A is correct". GPT correctly pushed back (§3): a matched subtitle
proves the *time of the line*, not that the required character/action is
on screen at that time. This is now treated as an open item — dialogue is a
locator, not proof of the visual — and is part of why the gold set matters.

---

## Baseline capabilities that work and should be preserved

Local episode ingest; subtitle sidecar matching (now episode-safe); dialogue
search; per-`(beat,shot)` windows; clue-script grounding; narration/voiceover
timeline; FFmpeg cutter/render; queue/resume/editor; Strict/Balanced/Draft;
NEEDS VISUAL placeholders; episode-scoped lookup; Gemini error surfacing.
