# media_index — locate and cut

Turns a folder of owned movies / series into a searchable index of **every
spoken line**, so a quote from a script resolves to an exact file and
millisecond — then cuts that moment out as a clip or a still frame.

This is the foundation of the movie automation tool. It replaces the step that
is currently broken — asking an LLM for a YouTube link and timestamp, which it
cannot know and therefore invents.

> **The LLM says WHAT to look for. This module finds WHERE it is.**
> Every timestamp here comes from a real subtitle file. Nothing is guessed.

---

## Quick start

```bash
# 0. FIRST: will this folder even work?  (seconds, no index built)
python -m media_index check "D:/Breaking Bad Season 2"

# 1. build the index (subtitles only — no video is decoded)
python -m media_index build "D:/Media" --db library.db

#    …and verify every subtitle against the audio while indexing
python -m media_index build "D:/Media" --db library.db --verify-sync

# 2. find a line
python -m media_index find "I never wanted the harvest" --db library.db

# 3. cut it — locate, snap to the shot, write clip + still
python -m media_index cut "I never wanted the harvest" --db library.db \
        --out clip.mp4 --seconds 4 --still frame.jpg

# 4. pre-flight a whole script before rendering anything
python -m media_index resolve script.json --db library.db --out report.json

# 5. what titles does this script need, and what is still missing?
python -m media_index sources script.json --db library.db

# 6. queue 25 videos: check them all, then build the ones that passed
python -m media_index preflight jobs.json          # check only
python -m media_index run jobs.json                # check, then build

# check one file's subtitle timing;  what is in the library
python -m media_index sync "D:/Media/Movie/Movie.mkv"
python -m media_index stats --db library.db
```

Try it with no media at all:

```bash
python -m media_index.demo.make_demo_library demo_media
python -m media_index build demo_media --db demo.db
python -m media_index resolve media_index/demo/demo_script.json --db demo.db
```

---

## Measured performance

A synthetic 73-episode series (8 seasons, ~800 lines per episode):

| | |
|---|---|
| Dialogue lines indexed | **59,200** |
| Index build time | **3.0 s** |
| `library.db` size | **9.4 MB** |
| Single search | **~150 ms** |

Building is incremental — a re-scan of unchanged files touches nothing, so
adding a new season costs seconds. **Indexing is one-time per file, not per
video project.**

## Dependencies

**None required for the index.** `rapidfuzz` is used when installed and gives a
faster fuzzy match; without it the module falls back to stdlib `difflib`, and
the suite passes on both paths.

**`ffmpeg` is required for sync detection and cutting** (and to read subtitles
embedded inside a video). `ffprobe` is used when present; when it is missing,
`probe.py` parses `ffmpeg -i` output instead, so nothing breaks.

---

## Subtitle sync detection (`sync.py`)

A downloaded `.srt` is very often timed for a *different release* — another
cut, another framerate, with or without a distributor intro. It then runs
seconds early or late, and every clip lands next to the line instead of on it.
The dangerous part is that it fails **silently**: the index looks healthy.

Detection needs no ML:

1. `ffmpeg silencedetect` gives where the audio is speaking.
2. The cues give where it *should* be speaking.
3. Slide one against the other; keep the offset with the best agreement.

Both timelines are packed into Python big integers, so testing one offset is a
shift + AND + popcount. That is fast enough in pure Python — no numpy.

Framerate conversion (23.976 vs 25 fps) shows up as **stretch**, not shift, so
nine standard ratios are searched alongside the offset.

### Measured on planted drift

| Case | Detected | Error |
|---|---|---|
| clean, +3000 ms | −3000 ms | **0 ms** |
| audio has 4 unsubtitled sounds, −4500 ms | +4500 ms | **0 ms** |
| subtitles have 4 lines with no audio, +2000 ms | −2000 ms | **0 ms** |
| both messy, +6000 ms | −6000 ms | **0 ms** |
| framerate 25→23.976 + 1200 ms | scale + offset | **−44 ms** |
| **subtitles from a different film** | — | **refused: `low`** |

Confidence comes from **peak prominence** — how far the winning offset beats
every rival more than 2 s away. Real matches scored 0.20–0.32; wrong subtitles
scored **0.01**. That clean separation is what makes the last row safe.

A `high`/`medium` result is applied to the cues **before** they are stored, so
every timestamp in the index is already true against the video. A `low` result
is recorded and reported but **never applied** — guessing a shift is worse than
leaving it alone.

---

## Cutting (`cutter.py`)

Two things stand between "the line is at 14:32.5" and a usable clip:

- **Shot boundaries.** A clip that runs across a camera change looks like a
  mistake. Boundaries around the line are detected with ffmpeg's own `scene`
  score, and the clip is pulled inside a single shot where it fits. When it
  cannot fit, `crossed_shots` says so rather than hiding it.
- **Seek accuracy.** Stream copy can only start on a keyframe. The default
  re-encodes and is frame-accurate; `--mode fast` stream-copies when speed
  matters more.

`target_seconds` is honoured even when the matched line runs longer. The clip
is silent b-roll under your own narration, so its length is an editing
decision — a 5.5 s quote must not silently become an 8 s clip. Pass
`--full-line` when the whole line really is wanted.

### A note on the scene threshold

Measured across identical hard cuts, ffmpeg's scene score ranged from **0.03
to 0.74** — the score reflects how different two frames happen to look, not
whether an edit occurred. The default is 0.15. **It needs re-validating on real
footage**, where camera motion (absent from synthetic tests) creates false
positives that solid-colour test video cannot show.

### Stills are the images half of the pipeline

`extract_frame()` pulls a still straight out of the scene you already located.
For a scene that exists in your own library this beats searching the web for an
image of it: right shot by construction, source resolution, no watermark, and
consistent in look with the clips around it.

---

## What it handles

Built and tested against the shapes real libraries actually have:

| Case | Handled |
|---|---|
| Release filenames (`Show.S01E02.1080p.WEB-DL.x265-GRP.mkv`) | ✅ |
| `2x01`, `Season 3 Episode 12`, title from folder | ✅ |
| Sidecar `.srt`, `.en.srt`, `Subs/<episode>/2_English.srt` | ✅ |
| `.ass` / `.ssa` / `.vtt` subtitles | ✅ |
| Embedded subtitle track (via ffmpeg) | ✅ |
| `<i>` tags, `{\an8}`, `SPEAKER:` labels, `[SOUND FX]`, `♪` | stripped |
| utf-8 / utf-8-sig / cp1252 / latin-1 encodings | ✅ |
| **A quote split across two or three cues** | ✅ — see below |
| Contractions (`doesn't` ≡ `does not`) | ✅ |
| The same line spoken in two episodes | flagged `ambiguous` |
| A file with no subtitles at all | reported, never silently skipped |

### The detail that makes it work

Subtitles break on reading speed, not on sentences:

```
285  00:14:31,220 --> 00:14:33,900   I am the Armored Titan
286  00:14:34,010 --> 00:14:36,480   and he is the Colossal Titan.
```

Matching cue-by-cue would fail on almost every real quote. Instead a window of
1–4 **consecutive** cues is merged and matched, and the tightest window that
scores well wins.

Two guards keep that honest:

- **`MAX_CUE_GAP_MS`** — cues that are consecutive by *index* may be minutes
  apart in *time*. Without this guard a merge produced a nine-minute "clip".
- **fragment retry** — if a long quote spans a pause too big to merge, the
  search retries with the leading fragment, which is what a human would do.

---

## Confidence, and why it matters

Every result carries a confidence band. This is the mechanism that lets the
pipeline run unattended:

| Band | Meaning | What the pipeline does |
|---|---|---|
| `high` | score ≥ 88 and ≥ 80% of the query's words present | use it |
| `medium` | score ≥ 72, ≥ 55% coverage | use it, but flag for review |
| `low` | below that | do not trust — fall back to visual search |

A misquoted line still finds the right moment but is **downgraded, not
accepted silently**. That is the whole point: the LLM is allowed to be wrong,
because being wrong is now visible.

## Batch pre-flight

`resolve` takes the JSON from the visual-script prompt and resolves every shot
before a single frame is rendered. Each shot lands in exactly one bucket:

| Status | Meaning |
|---|---|
| `resolved` | exact, unambiguous match |
| `ambiguous` | matched equally well in more than one place |
| `weak` | found, but the wording differs — verify |
| `not_found` | no dialogue match — needs visual search or is missing from the library |
| `no_query` | no dialogue supplied — goes to visual search |

The command exits non-zero when anything is `not_found`, so a queue runner can
refuse to start a render that would fail halfway through.

A wrong `season_episode` hint in the script does **not** override the library —
the real match wins and the disagreement is reported.

---

## Tests

```bash
cd shared && python -m unittest discover tests -v
```

**52 tests in ~10 s.** The index tests need no media at all. The video tests
render a small file with ffmpeg (known scene-cut times, known audio timings,
known colour per segment) and are skipped when ffmpeg is absent.

The end-to-end test is the one that matters: it indexes a file whose subtitles
are deliberately **3500 ms out of sync**, and asserts that the drift is
corrected during indexing, the quote resolves to its *true* position, and the
resulting clip shows the *correct scene* — verified by sampling the frame
colour, not by trusting the timestamps.

---

## When a download has no subtitles (`transcribe.py`)

Some releases ship with none — the season this was written against had none
across all thirteen episodes. Rather than hunting an `.srt` per episode
against tight free-API limits, the audio can be transcribed:

```
mi.bat transcribe "D:\Breaking Bad Season 2"
```

Needs `faster-whisper` (`setup.bat` offers to install it). The result is
written as an ordinary `.srt` beside each video, so nothing downstream knows
it was machine-made: it is cached forever, the index picks it up through the
normal sidecar path, and you can open and fix a line by hand.

The **English** audio track is chosen explicitly. A dubbed release lists the
dub first, and transcribing that produces fluent Hindi against an English
script — a failure that looks like "nothing matches" rather than a mistake.

Built for an overnight run: files that already have subtitles are skipped, so
an interrupted pass resumes for free and one failure never stops the rest.
Rough cost at the default `base.en` on CPU: about 5x realtime, so a 48-minute
episode takes ~10 minutes and a 13-episode season runs in about two hours.
`--model small.en` is slower and more accurate.

> **Untested on real speech.** The model could not be downloaded in the
> environment this was written in, so the recognition itself is stubbed in the
> tests. Everything around it is covered — track selection, the `.srt`
> round-trip, index integration, resume, failure isolation. Whether Whisper
> hears the dialogue well enough is a question only a real file answers.

---

## Checking a download (`doctor.py`)

Run this the moment a download finishes. It opens each file, reads what is
actually inside, and gives a verdict — plus the exact fix, named.

```
MEDIA CHECK — 6 file(s)

  ✅ Breaking Bad S02E01   47m  1920x1080  subs: embedded     684
  ⚠️  Breaking Bad S02E04   47m  1920x1080  subs: embedded     651
        subtitles are in devanagari script
        → download an English .srt named 'Breaking Bad Season 2 Episode 4.en.srt'
  ⚠️  Breaking Bad S02E05   47m  1920x1080  subs: none
        no subtitles at all
        → download an English .srt named 'Breaking Bad Season 2 Episode 5.en.srt'

  4 ready · 1 need subtitles · 1 need English subtitles · 0 unreadable
  → fetch the missing .srt files, then re-run this check
```

It also catches the identity mistakes that are expensive precisely because
they are silent: two files resolving to the *same* episode, and files that
turn out to hold several episodes each.

---

## The queue (`jobs.py`, `runner.py`)

```json
{
  "defaults": {"db": "library.db", "clip_seconds": 4.0, "height": 1080},
  "jobs": [
    {"name": "Why Walter Broke Bad", "script": "scripts/walter.json",
     "audio": "audio/walter.mp3", "out": "output/walter"},
    {"name": "The Red Wedding",     "script": "scripts/rw.json",
     "audio": "audio/rw.mp3",     "out": "output/rw"}
  ]
}
```

`run` does its checking **for every job first**, then builds. The failure that
must never happen — "job 8 died at 3 a.m., so jobs 9-25 never ran" — is
designed out: a job that cannot be built is named up front and skipped, and
every job is isolated, so a crash inside one leaves the rest untouched.

### Three outcomes, not two

| Status | Meaning | What happens |
|---|---|---|
| `READY` | every check passed | builds |
| `GAPS` | builds, but some scenes need attention | **builds anyway**, gaps reported |
| `BLOCKED` | cannot produce anything useful | skipped, reason printed |

The middle tier is the one that matters. A 50-scene video with two soft scenes
is still a video; refusing to build it is the wrong answer for someone queueing
twenty-five overnight. So resolution has a **target** (80%, below which the job
is a GAPS build) and a separate **floor** (50%, below which it is genuinely not
worth an hour of rendering). A missing title blocks only when it costs 30%+ of
the shots — one missing title out of five is a gap, not a blocker.

### Output layout

Written in the shape the existing editor tools already read:

```
out/
  scene_001/
    clip_01.mp4        cut on shot boundaries
    image_01_1.jpg     a still from the same moment
    scene.txt          the narration for this beat
  scene_002/
  manifest.json        every asset with its score and provenance
```

Re-running resumes: a scene whose folder already holds its assets is skipped,
so an interrupted queue picks up where it stopped rather than starting over.

## Not in scope (yet)

- **Visual index** — shot detection + embeddings for shots with no dialogue.
  That is Ladder 2, a separate module.
- **Frame quality scoring** — picking the sharpest, best-composed frame within
  a shot rather than the midpoint.
- **Frame quality scoring** — picking the sharpest, best-composed frame within
  a shot rather than the midpoint.
- **Visual index** — shot embeddings for scenes that have no dialogue at all.
