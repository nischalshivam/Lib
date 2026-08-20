# The visual-script prompt

Give this to Genspark / ChatGPT / Claude along with the clean narration script.

Every rule below is here because of something measured on a real 71-beat
script run against a real 62-episode library — not because it sounded sensible.

| Measured | Rule it produced |
|---|---|
| 106 shots, 5 quoted lines, **1** of them found in the subtitles | one **verbatim** line per 10 shots |
| the other 4 were paraphrases | verbatim or nothing — never approximate |
| the closing line was also quoted at shot 1 as a hook, which pinned the end of the scene to the start of the run and put the whole sequence four minutes late | a hook quote is marked, not ordered |
| 70 shots hung off 1 anchor, so shot 1 sat 221 s from the only known point | anchors spread through the run, not clustered |
| 12 shots in runs that quoted nothing anywhere | every run needs a line |
| planned 47% of the video's length | a duration budget |
| "real-world press photo" searched for as if it were a film | `type` decides where an image comes from |
| 274 of 287 assets placed by inference from 13 that were checked | `visual` is now searched against the picture — Rule 0 |
| a run of **85 shots with no quoted line**, where only 2 of 84 descriptions beat what a caption about *nothing* scores in the same episode, and the picture had "no opinion" about where the run happens | `scene_range` — Rule 1B |
| a sentence about Gus and Walter playing over Walt's wife and son | `characters` is now matched against reference photographs — Rule 6 |

---

## The prompt

````
You are a visual researcher for a documentary-style video essay. I will give
you a CLEAN NARRATION SCRIPT. You will return a VISUAL SCRIPT as JSON.

## What happens to your answer

A tool takes it and does two separate searches against the real film.

  1. Every line you quote word for word is looked up in the real subtitle
     file. A match becomes an exact millisecond.
  2. Every `visual` description is compared against every frame of the
     episode by an image-text model, and the shot is placed on the frame
     that matches it best.

The two check each other. A quoted line says WHEN. A visual description says
WHAT, and it is the only thing that can catch a quote that matched the wrong
moment.

You have NO access to any video. Never output a URL or a video ID.

Timestamps are the one exception, and they have their own rule — see Rule
1B. Read it before you write one. The short version: an APPROXIMATE RANGE
for a whole scene is wanted and useful; an exact per-shot timestamp is not,
because you would be inventing it.

## RULE 0 — `visual` is a caption, not a note to yourself

This is the field the picture search reads. Write what a person would see if
the sound were off and they had never watched the show.

Concrete and visible:

    "a man in a red hazmat suit and apron holding a box cutter"
    "a bald man in a blue shirt pressed back against a white tiled wall"
    "two men standing in a bright underground laboratory, one in a suit"

Not visible, and worth nothing to the search:

    "the moment everything changes for Walt"      <- an idea, not a picture
    "Gus asserting dominance"                     <- a judgement
    "the scene everyone remembers"                <- a fact about the audience

Rules that follow from that:

  - name what is WORN and what is HELD. Colour, clothing and objects are
    what an image model actually keys on.
  - describe the ROOM: bright lab, dark desert at night, a kitchen, a car
    interior. Two shots of the same face in different rooms are told apart
    by the room.
  - one sentence, plain words, present tense. Fifteen words is plenty.
  - character names may be included, but never INSTEAD of the description.
    "Gus Fring" tells the search nothing. "a calm man in glasses and a
    yellow shirt" tells it everything.
  - if two shots would get the same caption, they are the same shot. Give
    one of them a different detail or merge them with `count`.

A beat whose narration has no picture still needs a real caption — see Rule
4. Describe the face, the object or the room you chose, not the idea.

## RULE 1 — THREE verbatim lines in every run, and never fewer than two

A "run" is all the shots you take from one episode, however scattered they
are across the script. **Every run needs at least two quoted lines, and three
if it has more than twenty shots** — one near its first shot, one near its
middle, one near its last.

This is the single highest-value thing in this document, and it is worth
being blunt about why. On a measured build:

| what the run had | what happened |
| --- | --- |
| two or more quoted lines | shots landed where they belong |
| one quoted line, at the last shot | 90 shots hung off one point at the far end |
| no quoted line at all | 9 scenes of the finished video had nothing to show |

The quoted line is the only *exact* evidence in the whole tool — 89 of 89
were found in the real subtitles, to the millisecond. The picture search is
a fallback for the shots between them, and on ordinary interior drama it
frequently cannot tell one room from another. **Do not rely on it. Quote.**

If a run genuinely has no dialogue anywhere in it — a silent flashback, a
montage — quote the last line spoken BEFORE the sequence and the first line
spoken AFTER it, and mark neither as a hook. Two lines bracketing a silence
place everything inside it.

Within a run, at least one shot in every ten must carry `exact_dialogue`
quoted word for word as it is spoken.

Word for word means word for word. "Whatever it is you think I've done, you
have to let me explain" does not match a subtitle reading "Look, whatever you
think I did, let me explain." A near-miss finds nothing, and is worse than an
empty field, because an empty field is honest.

If you cannot recall a line exactly, leave `exact_dialogue` empty and quote a
DIFFERENT line in the same stretch that you do remember exactly. Short lines
are fine. Five plain words really said beat a fine sentence that was not.

Spread them. A line at shot 1 and nothing after gives that run one fixed
point. One near the start, one near the middle and one near the end gives it
a shape that cannot drift.

## RULE 1B — `scene_range`: where in the episode the scene is

**This is now the single most valuable field in the file, ahead of the
quoted lines.** Give it for every run.

    "scene_range": "29:30-33:40"

It means: everything this run draws from that episode happens between those
two times. The tool then takes the run's shots, in your order, and lays them
across that stretch — so the scene plays through instead of being searched
for shot by shot.

Why it beats everything else you can write: on a real build, a 85-shot run
from a scene where nobody speaks had no quoted line to anchor to, and the
picture model placed **2 of 84** shots better than chance. There was nothing
left for the tool to work with. A single range would have placed all 85.

### How to write it

  - **Approximate is fine, and expected.** The tool pads what you give it.
    Within a minute is plenty. Do not agonise.
  - **Size it to the SCENE, plus two minutes of slack.** A four-minute
    sequence gets about a six-minute range. This matters, and a real script
    got it wrong in a specific way: asked to "err wide", it returned
    `29:30-40:00` — ten and a half minutes — for a four-minute scene, and
    round numbers (`20:00-30:00`, `40:00-50:00`) for four others. A range
    that wide is barely better than no range at all, because the shots then
    spread across it.
  - Narrow is still the worse mistake of the two: `31:00-31:30` for that
    scene throws most of it away. Aim for scene length + 2 minutes and stop
    thinking about it.
  - **Round numbers are a warning sign.** If every range you have written
    starts and ends on a multiple of five minutes, you are not recalling
    anything — you are filling in boxes. Mark those `low`, or leave them
    out.
  - **One range per run**, written on the run's FIRST shot. Repeating it on
    every shot of the run is harmless.
  - **Say how sure you are:**

        "range_confidence": "high" | "medium" | "low"

    `high` = a famous scene you know the position of. `low` = a guess from
    the shape of the episode. Write `low` freely; it is still useful, and it
    tells the person whether to check it.
  - **If you genuinely do not know, OMIT THE FIELD.** An empty field is
    honest and the tool has other ways to try. A number you made up is a
    confident wrong answer, and it will move an entire run to the wrong
    place with nothing to reveal the mistake. This is the one field where
    inventing is worse than leaving blank.

### What NOT to do

  - **Never put a timestamp on an individual shot** (`at`, `timestamp`).
    The tool accepts that field, and it is for a HUMAN who has scrubbed to
    the exact frame in their player. You have not. Shot-level precision is
    beyond anything you can know.
  - Never state a range for an episode you are not drawing shots from.

## RULE 2 — a hook quote is not part of the scene

Essays open by quoting the ending. That is good writing, and it breaks the
tool, because the tool reads your order as the scene's order — the closing
line placed at shot 1 says the scene ENDS where it BEGINS, and the whole
sequence lands minutes late.

When you quote a line before the moment it belongs to, set:

    "hook": true

Keep the quote. It will be used for the picture and ignored for the ordering.

## RULE 3 — order is the scene's order, never the essay's

Within one episode, list shots in the order they happen ON SCREEN. If the
narration doubles back, name the episode again later in the file rather than
moving a shot out of sequence.

## RULE 4 — every beat gets something, including the abstract ones

Some narration has no obvious picture: "most people read this as rage", "he
had already decided". Do not skip these, and do not invent a shot.

Use the nearest CONCRETE thing the sentence is about, in this order:

  1. the face of the person the sentence is about, in that scene
  2. the object the sentence turns on — the box cutter, the phone, the door
  3. the room itself, wide or empty
  4. another shot from the same scene carrying the same feeling

Then describe THAT, as a picture, per Rule 0. Not "Gus's face, held, while
the narration argues about his motive" — the search cannot see a narration
argument. Write "a calm man in glasses and a yellow shirt, close on his face,
saying nothing". A held face under an argument is what a real editor cuts,
and it is always available.

## RULE 6 — `characters` is now matched against photographs

`characters` used to be decoration. It is now read: the person can supply a
folder of reference stills per character, and a shot naming them is placed
only among frames those people are actually in.

That makes it the fix for a specific, common and very visible failure — a
sentence about Gus and Walter playing over Walter's wife and son, because
the tool had no notion of who anybody was.

So:

  - **name every character visible in the shot**, using the name the show
    uses. First name alone is fine and preferred: `"Gus"`, `"Walter"`,
    `"Jesse"`, `"Mike"`, `"Hector"`.
  - name only who is ON SCREEN in that shot, not who the sentence is about.
    A shot of Walt listening while Gus speaks names Walt.
  - leave it empty for a shot with no people in it — an object, a room, a
    landscape. An empty list is read as "no opinion", never as "nobody".
  - it does NOT replace `visual`. Rule 0 still applies in full.

## RULE 5 — the duration budget

1. Count the words in the narration.
2. Spoken seconds = words / 150 * 60.
3. Every beat's visuals must cover its own narration.
4. State the totals at the end and confirm they match.

A visual script covering half the narration is not half-finished. It is
unusable — the other half of the video has nothing on screen.

## Shot length and the image split

Roughly 55% of screen time on STILLS, 45% on CLIPS.
  - a still holds for about 5 seconds
  - a clip runs 3 to 5 seconds, never longer

A 12-second beat is one clip (4 s) + one still (5 s) + one clip (3 s). Write
them separately. Never one 12-second shot.

Where several stills come from the same moment, use `count` instead of
repeating near-identical entries.

## Pace

  - argument, analysis, setup  ->  4-6 second visuals, calmer
  - a list, a montage, a turn  ->  2-3 second visuals, rapid
  - one dramatic beat          ->  a single held 6-8 second clip

## OUTPUT — valid JSON only, no commentary

[
  {
    "beat": 1,
    "header": "SHORT ALL-CAPS LABEL",
    "narration": "<the exact sentence(s) from my script>",
    "narration_seconds": 12,
    "shots": [
      {
        "kind": "clip",
        "source": "Breaking Bad",
        "season_episode": "S04E01",
        "se_confidence": "high",
        "scene_range": "29:30-33:40",
        "range_confidence": "medium",

        "exact_dialogue": "Well? Get back to work.",
        "speaker": "Gus Fring",
        "dialogue_confidence": "high",
        "hook": true,

        "nearest_dialogue": "",
        "nearest_dialogue_position": "",

        "visual": "a man in a red hazmat suit and a blood-stained white apron standing in a bright underground laboratory, two men against the wall",
        "characters": ["Gus Fring", "Walter White", "Jesse Pinkman"],
        "setting": "underground superlab, fluorescent light",
        "must_not_have": ["talking head commentary", "burned-in subtitles",
                          "reaction cam", "fan art"],
        "duration_target_sec": 4
      },
      {
        "kind": "still",
        "count": 2,
        "source": "Breaking Bad",
        "season_episode": "S04E01",
        "nearest_dialogue": "Well? Get back to work.",
        "nearest_dialogue_position": "before",
        "visual": "close on a blood-stained white apron and a green box cutter held in a gloved hand",
        "duration_target_sec": 5
      }
    ],
    "images": [
      {
        "subject": "Giancarlo Esposito, formal press portrait",
        "type": "real_world"
      }
    ]
  }
]

## Field rules

**visual** — REQUIRED on every shot. The caption the picture search reads.
See Rule 0. A shot with a vague `visual` is placed by arithmetic alone, which
is the failure this whole field exists to prevent.

**kind** — "clip" for moving footage, "still" for a held frame. Required.

**type** — REQUIRED on any shot that does NOT come out of the film: an
actor's press portrait, a photograph of a cinema, a writer at a desk. Set
`"type": "real_world"` and leave `season_episode` empty.

Six shots of a real script were press portraits — Vince Gilligan, an actor
at a premiere, rows of cinema seats — written as ordinary shots with a
`source` of "Vince Gilligan press portrait". The tool then went looking for
a film of that name, reported it missing, and the beats came out empty. A
shot from the film and a shot of the world are different searches, and the
only thing that tells them apart is this field.

**scene_range** — "MM:SS-MM:SS" into the episode, on the first shot of every
run. See Rule 1B. Approximate and wide beats precise and narrow; omitted
beats invented.

**range_confidence** — "high", "medium" or "low". How sure you are of
`scene_range`. Write "low" freely — it is still worth having, and it tells
the person which ranges to check in their own player before building.

**characters** — everyone visible IN THAT SHOT, by the name the show uses.
See Rule 6. Matched against reference photographs, so a first name is enough
and an empty list is read as "no opinion".

**count** — stills only. How many distinct frames to take from that moment.

**exact_dialogue** — spoken during this shot, word for word, or empty.

**hook** — true when the line is quoted out of sequence (Rule 2).

**nearest_dialogue** — REQUIRED whenever exact_dialogue is empty. The closest
line before or after, word for word, and which side it falls on.

  A silent shot with a nearby quoted line can still be found — the tool
  locates the line and walks outward. A silent shot with nothing quoted
  anywhere near it cannot be found at all.

  If a whole sequence is silent, quote the last line before it and the first
  line after it and attach those to the first and last shots. Two lines will
  place a dozen silent shots between them.

**season_episode** — "S04E01", or "unknown". Never invent one; set
se_confidence to "high", "guess" or "unknown" and let the tool verify.

**source** — the exact title, every time, no abbreviations. When a character
appears across several titles, say which title each individual shot is from.

**duration_target_sec** — how long the finished CLIP runs, not how long the
moment lasts on screen.

**images / type** — "from_source" (a frame of the film, and then `source` must
be the film's real title), "real_world" (an actor, writer, place, event) or
"stock". Never give a URL, and never put a description where a title goes:
`"source": "real-world press photo"` is not a film, and will be searched for
in a library of films.

## Before you finish

Append one final JSON object. It is a SEPARATE JSON document, after the
closing `]` of the array — not an extra element inside it, and not merged
into it. The tool reads both, and reads any plain-English note you put after
them too:

{
  "summary": {
    "narration_words": 0,
    "narration_seconds": 0,
    "visual_seconds_planned": 0,
    "coverage_percent": 0,
    "beats": 0,
    "clips": 0,
    "stills": 0,
    "verbatim_lines": 0,
    "longest_gap_between_verbatim_lines": 0,
    "runs_without_any_verbatim_line": 0,
    "runs_with_only_one_verbatim_line": 0,
    "shots_with_a_visible_caption": 0,
    "shots_with_characters_named": 0,
    "runs_total": 0,
    "runs_with_a_scene_range": 0,
    "shots_total": 0
  }
}

Fix and re-answer if any of these is true:
  - coverage_percent below 95
  - longest_gap_between_verbatim_lines above 10
  - runs_without_any_verbatim_line above 0
  - runs_with_only_one_verbatim_line above 0
  - shots_with_a_visible_caption below shots_total
  - runs_with_a_scene_range below runs_total, UNLESS you genuinely do not
    know where that scene falls — in which case leave the field out and say
    so in one line after the JSON, naming the episode. Never fill it in to
    make this number go up. A made-up range is the most damaging thing you
    can put in this file.

After the summary you may write one or two plain sentences — outside the
JSON — about which `scene_range` values are guesses and should be checked in
a player. That note is shown to the person before they build, and it is the
most useful thing you can say to them.

Now here is my script:
````

---

## Reading what comes back

```
mi.bat sources  script.json --db library.db     which titles are needed
mi.bat align    script.json --db library.db     how many shots can be placed
```

### The summary above is the model marking its own homework

It reported fifteen verbatim lines once. Six of them existed. The rest were
paraphrases — close enough to read as quotes, not close enough to be found —
and nothing said so until three stages later, when a hundred-shot run came
back hanging off a single anchor at its far end.

So the tool counts them itself. Building one script prints:

```
  ABOUT THE QUOTED LINES
  6/15 quoted line(s) found, 1 of 4 run(s) have none at all, longest
  stretch without one: 34 shots

      beat 12 shot 2: not in the subtitles — "Whatever it is you think..."
      These read like quotes but are not word for word. Copy them from the
      subtitle file, or drop them.
```

Take those lines back to the prompt and fix them there. It is a retry, not a
rebuild.

### Then read the run lines

```
Breaking Bad S04E01: 70 shot(s), 1 anchor(s), span 2010s-2264s
  only one line matched, at shot 62 of 70...
  Breaking Bad S04E01: 54/70 shot(s) found in the picture, 31 moved
```

Two numbers, and they mean different things:

**Anchors** are lines proven to be spoken at that millisecond. One anchor in
a run of seventy means the other sixty-nine are arithmetic hanging off it.

**Found in the picture** is how many shots the image model could actually
locate from their description. This is the number that survives a bad anchor,
and the one to push on: a run with two anchors and fifty-four confirmed
pictures is sound; a run with two anchors and four is not, and the fix is
better `visual` captions, not more quotes.

Coverage is neither. 89% placed with one anchor and no picture index is 89%
of the shots sharing one guess.
