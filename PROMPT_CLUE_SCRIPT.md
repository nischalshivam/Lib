# The clue-script prompt

Give this to **Claude** (not Genspark) along with your clean narration
script. It runs *before* the visual script — its output is what makes the
visual script accurate.

## Why this exists, in one paragraph

Every accuracy problem this tool has ever had comes down to one question:
*which second of which episode is this shot?* The tool has exactly one way
to answer it with certainty — find a line of dialogue in the local subtitle
file. Measured across four real builds, the picture search placed **2 shots
out of 354**. Dialogue placed the rest.

So the clue script has one job, and it is not what you would guess:

> **Remember the dialogue. Do not guess the timestamp.**

A remembered timestamp was wrong 4 times out of 5 on a real script — by
seven to fifteen minutes. A remembered *line of dialogue* can be looked up
in the subtitles and becomes exact, or fails loudly. One is a guess wearing
a number; the other is a search key.

---

## The prompt

````
You are preparing research notes for a video essay about a film or series.
I will give you a CLEAN NARRATION SCRIPT. You will return a CLUE SCRIPT as
JSON.

Your notes will be read by a tool that has the actual episodes on disk,
with subtitles, indexed and searchable. The tool cannot search the internet
and cannot watch anything. It can do exactly one thing extremely well:

    look up a line of dialogue in the real subtitle file and return the
    millisecond it is spoken at.

Everything you write is judged by whether it helps that lookup succeed.

## THE ONE RULE ABOVE ALL OTHERS

**Recall dialogue. Never invent a timestamp.**

You do not have the file. Any minute:second you write is a guess. On a real
script, four of five guessed timestamps were wrong by seven to fifteen
minutes, and every shot that depended on them went to the wrong part of the
episode.

A line of dialogue is different. If you remember it correctly, the tool
finds it exactly. If you remember it wrongly, the tool says so and nothing
breaks. Wrong dialogue is safe; wrong timestamps are not.

So for every scene the narration refers to, your primary job is:

  **write down what is actually SAID in that scene, word for word.**

## RULE 1 — three lines per scene, verbatim

For every distinct scene the essay refers to, give:

  - `dialogue_in_scene` — 1 to 3 lines spoken DURING the scene
  - `dialogue_before` — the last memorable line spoken shortly BEFORE it
  - `dialogue_after` — the first memorable line spoken shortly AFTER it

Word for word, as the subtitles would have them. Not a paraphrase.

    GOOD:  "A guy that clean has gotta be dirty."
    BAD:   "Hank says something about how the clean guy must be dirty."

Two lines bracketing a silent moment are worth more than ten sentences of
description, because they turn a wordless scene into a bounded stretch of
film the tool can search inside.

**If a scene has no dialogue at all** (a montage, a wordless killing, a
flashback with only music), that is exactly when `dialogue_before` and
`dialogue_after` matter most. Give them. Say `"silent": true`.

**If you cannot recall a line exactly, leave the field empty.** An empty
field is honest. A near-miss finds nothing and wastes the lookup. Do not
fill fields to look complete.

## RULE 2 — say how sure you are, honestly

Every clue carries:

    "confidence": "high" | "medium" | "low"

  - `high`  — a famous line you are certain of, word for word
  - `medium`— you are confident of the wording but not the exact scene
  - `low`   — you think this is roughly right

Write `low` freely. The tool treats `low` clues as hypotheses to be checked,
not as facts. A clue marked `low` and correct is useful. A clue marked
`high` and wrong is damaging.

## RULE 3 — who is on screen, not who is being discussed

    "characters_on_screen": ["Hank", "Walt"]

Only people VISIBLE in that scene. The tool matches these against reference
photographs. A character the narration talks about but who is not in the
room must go in `characters_mentioned` instead — putting them in
`characters_on_screen` makes the tool reject the correct footage.

First names as the show uses them. "Hank", not "Hank Schrader (DEA)".

## RULE 4 — episode as a guess, clearly labelled

    "episode": "S05E08",
    "episode_confidence": "high" | "medium" | "low"

If you are not reasonably sure which episode, write `"episode": ""`. The
tool has other ways to find it. A confident wrong episode sends every shot
of that scene into a different hour of television.

## RULE 5 — describe what is SEEN, not what it means

    GOOD:  "a man sitting on a toilet holding an open hardback book,
            reading a handwritten note on the inside cover"
    BAD:   "the moment Hank's world falls apart"

The tool compares your description against actual frames. It cannot see a
world falling apart. It can see a book, a bathroom, a seated man.

Name what is worn, what is held, and what room it is. Colour and objects
are what an image model keys on.

## RULE 6 — one entry per SCENE, not per sentence

The narration may spend six sentences on one scene. That is one clue entry.
The narration may cover four different scenes in one paragraph. That is
four clue entries.

Group by "what moment of the film is this", not by sentence.

## OUTPUT — valid JSON only, no commentary

{
  "schema": "clue-1",
  "title": "Breaking Bad",
  "clues": [
    {
      "clue_id": "C01",
      "narration_covered": "<the sentence(s) from my script this is about>",
      "what_happens": "Hank finds Walt's copy of Leaves of Grass in the bathroom and reads Gale's inscription",
      "episode": "S05E08",
      "episode_confidence": "high",
      "silent": false,

      "dialogue_in_scene": [],
      "dialogue_before": "I'm gonna go use your bathroom.",
      "dialogue_after": "",
      "dialogue_confidence": "medium",

      "characters_on_screen": ["Hank"],
      "characters_mentioned": ["Walt", "Gale"],

      "location": "Walt's bathroom",
      "visible": "a seated man in a shirt holding an open hardback book, close on a handwritten inscription inside the cover",
      "objects": ["book", "handwritten note"],

      "notes": "The inscription reads 'To my other favorite W.W.' — an insert shot of the page is the key visual."
    }
  ]
}

## BEFORE YOU FINISH

Append this, as a SEPARATE JSON document after the closing brace:

{
  "summary": {
    "clues": 0,
    "clues_with_dialogue": 0,
    "clues_with_no_dialogue_at_all": 0,
    "episodes_named": 0,
    "episodes_left_blank": 0
  }
}

Then, in one or two plain sentences outside the JSON, say which clues you
are least sure about and why. That note is shown to the person before they
build, and it is the most useful thing you can tell them.

Fix and re-answer if:

  - `clues_with_no_dialogue_at_all` is more than a fifth of `clues` —
    go back and recall the lines before and after those scenes
  - any clue has an episode marked `high` that you would not bet on
  - any `visible` field describes a feeling rather than a picture

Now here is my script:
````

---

## Where it goes in the tool

New Video → **Clue script** (just under Script). Optional; a build without
one behaves exactly as it always did. What it changes is visible at Check,
in a line that says how many of the remembered lines were actually found:

    clue script: 61/82 line subtitle me mili (74%) · 38 shot ko asli quote
                 mila · 15 run ko dono taraf se bandha

## How the three scripts fit together

| # | File | Who writes it | Its one job |
|---|---|---|---|
| 1 | **Clean narration** `.txt` | you / Claude | what the audience hears; times the video against your voiceover |
| 2 | **Clue script** `.json` | **Claude**, with this prompt | remembered **dialogue** → the tool turns it into exact milliseconds |
| 3 | **Visual script** `.json` | Genspark, with `PROMPT_VISUAL_SCRIPT.md` | which shot on which beat, how long |

Order matters: **write the clue script second and give it to Genspark** when
you ask for the visual script. Genspark then has real dialogue to quote
instead of inventing quotes, which is where its `exact_dialogue` fields have
been failing.

### Measured, on the same essay written both ways

One clean narration script, and two visual scripts from it — one written
straight from the clean script, one written from a clue script first:

| | from the clean script | from the clue script |
|---|---|---|
| distinct quoted lines | 9 | 19 |
| narration covered | 926 s | 154 s |
| **one anchor per** | **103 s** | **8 s** |
| shots with no line at all | 10 | 0 |

Twelve times the anchor density. Not a better model — the same model, given
the one thing it can be accurate about.

Two things that measurement also showed, both worth knowing before you
follow it:

  - The clue-script run **stopped at 24 beats** and covered a quarter of
    the narration. Feeding Genspark a clue script uses up its output budget.
    Ask for the visual script **in halves** — beats 1–30, then 31–60 — or
    you will get an excellent script for the first three minutes only.
  - The clue script is worth giving to the tool **whichever** way the visual
    script was written. Against the clean-script version it still matched 25
    of 59 beats and offered a real quote to 56 shots that had none. Giving
    Genspark the clue script and giving the tool the clue script are two
    separate wins, and you should take both.

## What the tool does with it

Every clue is a **hypothesis**, never a fact:

1. Each remembered line is looked up in your local subtitles.
2. A line that is found becomes an **anchor** — exact to the millisecond,
   and Tier A.
3. Two found lines bracketing a silent scene bound that scene, and every
   shot inside it is laid out in order between them.
4. A line that is not found is reported and ignored. Nothing breaks.
5. A remembered episode is only used if a line from that scene is found in
   it.

An LLM's memory never becomes evidence. It becomes a **search key**, and
the subtitle file decides.
