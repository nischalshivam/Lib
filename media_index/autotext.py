"""Auto-generate a VText instruction file from the clean narration (+ clue),
so a video gets competitor-style kinetic text WITHOUT a hand-made GPT file.

It is a heuristic editor, not an LLM: it walks the narration, times each
sentence against the voiceover length, picks the strongest ~1-per-9-seconds
moments, and condenses each into a short punchy on-screen phrase (proper nouns,
numbers, and the highest-content words — never a subtitle). The NARRATION_CUE is
copied verbatim so VText's forced-aligner lands the text on the spoken words.

Good enough to run every video with zero extra work; a hand-written instruction
file (if the user supplies one) still wins on pure punch and takes over when
present. Both feed the same VText renderer.
"""
from __future__ import annotations

import os
import re

STOP = set("""a an the and or but so of to in on at by for with from into over
under as is are was were be been being it its this that these those he she they
them his her their our your you we i my me him himself herself themselves not no
only just very much more most also then than when while because if about after
before between during through against out up down off again once here there what
which who whom whose why how all any both each few other some such own same too
can will would could should may might must does did done has have had having
what's it's he's she's they're there's who's""".split())

# words that make a sentence worth a text card
PUNCHY = set("""empire power money death control kill blood war family betrayal
loyalty fear truth lie secret revenge legacy collapse rise fall never always
everything nothing alone trapped guilt shame innocent guilty ownership""".split())

EVENT_FIELDS = ("NARRATION_CUE", "EVENT_TYPE", "DISPLAY_TEXT", "EMPHASIS_WORDS",
                "INTENSITY", "TEXT_ROLE", "VISUAL_FREEDOM", "SEQUENCE_GROUP")
_WORD = re.compile(r"[A-Za-z0-9'’]+")
_SENT = re.compile(r"[^.!?]+[.!?]?")


def _sentences(text: str) -> list:
    out = []
    for raw in _SENT.findall(text or ""):
        s = " ".join(raw.split())
        if len(_WORD.findall(s)) >= 4:
            out.append(s)
    return out


def _proper_nouns(words: list, idx: int) -> list:
    """Capitalised words that are NOT sentence-initial (real names/places)."""
    keep = []
    for i, w in enumerate(words):
        bare = w.strip(".,;:!?'\"")
        if not bare:
            continue
        if bare[0].isupper() and (i > 0) and bare.lower() not in STOP:
            keep.append(bare)
    return keep


def _display_text(sentence: str) -> tuple:
    """(display_text, emphasis) — condense the sentence to <=6 punchy words.
    Priority: a number/date, then proper nouns, then the highest-content words.
    """
    words = sentence.split()
    lower = [w.strip(".,;:!?'\"").lower() for w in words]
    # a number/date wins (shown as digits)
    for w in words:
        b = w.strip(".,;:!?'\"")
        if b.isdigit() and len(b) >= 3:
            near = [x.strip(".,;:!?'\"") for x in words
                    if x.strip(".,;:!?'\"").lower() not in STOP
                    and x.strip(".,;:!?'\"").isalpha()][:2]
            disp = " ".join(near[:1] + [b]) if near else b
            return _titlecase(disp), b
    props = _proper_nouns(words, 0)
    if props:
        disp = " ".join(props[:3])
        return _titlecase(disp), props[0]
    # otherwise: the most content-heavy words in order (punchy > long nouns)
    scored = []
    for i, (w, lw) in enumerate(zip(words, lower)):
        b = w.strip(".,;:!?'\"")
        if not b.isalpha() or lw in STOP or len(b) < 3:
            continue
        score = (3 if lw in PUNCHY else 0) + min(len(b), 9) / 9.0
        scored.append((score, i, b))
    scored.sort(reverse=True)
    picked = sorted(scored[:3], key=lambda t: t[1])         # keep reading order
    if not picked:
        return "", ""
    disp = " ".join(b for _, _, b in picked)
    emph = max(scored, key=lambda t: t[0])[2]
    return _titlecase(disp), _titlecase(emph)


def _titlecase(s: str) -> str:
    return " ".join(w[:1].upper() + w[1:] for w in s.split())


def _linebreak(disp: str) -> str:
    """1-4 words per line, at most two lines — VText reads '/' as a break."""
    ws = disp.split()
    if len(ws) <= 2:
        return disp
    mid = (len(ws) + 1) // 2
    return " ".join(ws[:mid]) + " / " + " ".join(ws[mid:])


def _event_type(sentence: str, first: bool) -> tuple:
    s = sentence.lower()
    if first:
        return "HOOK", "HIGH", "IMPACT"
    if sentence.rstrip().endswith("?"):
        return "QUESTION", "HIGH", "IMPACT"
    if re.search(r"\b\d{3,}\b", sentence):
        return "NUMBER_OR_DATE", "MEDIUM", "INFORMATION"
    if any(w in s for w in (" but ", " while ", " whereas ", " unlike ",
                            " instead ")):
        return "CONTRAST", "MEDIUM", "IMPACT"
    if any(w in s for w in PUNCHY):
        return "REVELATION", "HIGH", "EMOTION"
    return "CHARACTER_INSIGHT", "MEDIUM", "INFORMATION"


def _cue(sentence: str) -> str:
    """5-12 verbatim words for the aligner to match (punctuation dropped)."""
    words = _WORD.findall(sentence)
    return " ".join(words[:12]) if len(words) >= 5 else " ".join(words)


def generate(clean_text: str, beats: list, out_path: str,
             niche: str = "MOVIE_ESSAY", gap_s: float = 9.0,
             wpm: float = 150.0, log=lambda *a: None) -> str:
    """Write a VText instruction file for `clean_text`. Roughly one text every
    `gap_s` seconds, weighted to the strongest sentences. Returns out_path."""
    sents = _sentences(clean_text)
    if not sents:
        return ""
    total_words = sum(len(_WORD.findall(s)) for s in sents) or 1
    total_s = sum(float(b.get("narration_seconds") or 0) for b in (beats or [])) \
        or (total_words / wpm * 60.0)
    target = max(4, int(total_s / gap_s))
    # score each sentence, then pick spaced winners up to `target`
    scored = []
    wc = 0
    for i, s in enumerate(sents):
        n = len(_WORD.findall(s))
        t = (wc + n / 2.0) / total_words * total_s          # approx spoken time
        wc += n
        disp, emph = _display_text(s)
        if not disp:
            continue
        score = (2 if _proper_nouns(s.split(), 0) else 0) \
            + (2 if re.search(r"\b\d{3,}\b", s) else 0) \
            + sum(1 for w in s.lower().split() if w.strip(".,;:!?") in PUNCHY) \
            + (1 if s.rstrip().endswith("?") else 0) \
            + (1.5 if 6 <= n <= 20 else 0)                   # tidy length
        scored.append({"i": i, "t": t, "s": s, "disp": disp, "emph": emph,
                       "score": score})
    scored.sort(key=lambda e: e["score"], reverse=True)
    chosen, used_t = [], []
    for e in scored:
        if len(chosen) >= target:
            break
        if any(abs(e["t"] - u) < gap_s * 0.7 for u in used_t):
            continue                                          # keep them spaced
        used_t.append(e["t"])
        chosen.append(e)
    chosen.sort(key=lambda e: e["t"])

    lines = ["=== VTEXT INSTRUCTION FILE v1 ===",
             f"VIDEO_TITLE: {_titlecase(' '.join(sents[0].split()[:5]))}",
             f"NICHE: {niche}", "LANGUAGE: en",
             f"TOTAL_EVENTS: {len(chosen)}", ""]
    for n, e in enumerate(chosen, 1):
        et, inten, role = _event_type(e["s"], n == 1)
        vf = "HIGH" if inten == "HIGH" else "MEDIUM"
        lines += [f"--- EVENT {n:03d} ---",
                  f'NARRATION_CUE: "{_cue(e["s"])}"',
                  f"EVENT_TYPE: {et}",
                  f"DISPLAY_TEXT: {_linebreak(e['disp'])}",
                  f"EMPHASIS_WORDS: {e['emph']}",
                  f"INTENSITY: {inten}",
                  f"TEXT_ROLE: {role}",
                  f"VISUAL_FREEDOM: {vf}",
                  "SEQUENCE_GROUP: NONE", ""]
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log(f"  auto-text: {len(chosen)} text moment(s) picked from the narration "
        f"(~1 every {gap_s:.0f}s) -> {os.path.basename(out_path)}")
    return out_path
