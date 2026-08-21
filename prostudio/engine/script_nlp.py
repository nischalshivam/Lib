"""Script analysis — the text brain.

- splits narration into 2-3 word LOGICAL chunks (not robotic single words)
- finds CRUCIAL moments: names, numbers/years, quotes, emphasis words
- text policy: first 60s = dense; after that only crucial moments, with a
  forced refresher if 35s pass with nothing (user rule: 30-40s cadence)
- keyword colorization (psychology): danger=RED, success=YELLOW/GREEN,
  names=GOLD (toggleable)
- scene mood detection -> sentiment color grade
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import GOLD, GREEN, RED, WHITE, YELLOW

DANGER = {"dead", "death", "die", "dies", "kill", "killed", "murders", "murder",
          "blood", "war", "gun", "enemy", "criminal", "ruthless", "terror",
          "terrifying", "danger", "dangerous", "alone", "fear", "scare",
          "scares", "villain", "loss", "lose", "broke", "breaking", "cartel",
          "mafia", "laundering", "regime", "warning"}
SUCCESS = {"popular", "hit", "empire", "power", "powerful", "control", "gold",
           "rich", "money", "success", "win", "wins", "king", "kingpin",
           "dream", "famous", "great", "top", "profit", "world"}
STOP = {"the", "a", "an", "and", "or", "of", "in", "on", "to", "is", "was",
        "has", "have", "had", "he", "she", "it", "his", "her", "that", "this",
        "with", "for", "at", "by", "be", "been", "are", "were", "you", "they",
        "them", "there", "their", "its", "but", "not", "from", "into", "out"}


@dataclass
class Chunk:
    text: str
    word_index: int          # index of first word within the scene narration
    n_words: int
    crucial: bool = False
    colors: list = field(default_factory=list)   # per word


def _clean_word(w: str) -> str:
    return re.sub(r"[^\w'-]", "", w)


def _is_name(word: str, prev: str) -> bool:
    """Capitalised mid-sentence = probably a name/place."""
    w = _clean_word(word)
    return (len(w) > 1 and w[0].isupper() and not w.isupper()
            and prev not in ("", ".", "!", "?", "—", "-"))


def word_color(word: str, colorize: bool) -> str:
    if not colorize:
        return WHITE
    w = _clean_word(word).lower()
    if w in DANGER:
        return RED
    if w in SUCCESS:
        return YELLOW if w not in ("profit", "money", "rich") else GREEN
    return WHITE


def chunk_scene(narration: str, colorize=True, max_words=3):
    """Break narration into 2-3 word chunks on natural boundaries; mark
    crucial chunks and per-word colors."""
    tokens = narration.split()
    chunks, cur, cur_idx, prev = [], [], 0, ""
    for i, tok in enumerate(tokens):
        if not cur:
            cur_idx = i
        cur.append(tok)
        w = _clean_word(tok)
        end_punct = tok.rstrip('"”’').endswith((".", ",", "!", "?",
                                                          ";", ":", "—"))
        if len(cur) >= max_words or end_punct or i == len(tokens) - 1:
            text = " ".join(cur)
            colors = [word_color(t, colorize) for t in cur]
            crucial = False
            for j, t in enumerate(cur):
                cw = _clean_word(t)
                p = _clean_word(cur[j - 1]) if j else prev
                if _is_name(t, p):
                    crucial = True
                    if colorize and colors[j] == WHITE:
                        colors[j] = GOLD
                if re.fullmatch(r"(19|20)\d\d|\d+[%$]?|\$\d+\S*", cw or " "):
                    crucial = True
                    if colorize and colors[j] == WHITE:
                        colors[j] = YELLOW
                if colorize and colors[j] in (RED, YELLOW, GREEN):
                    crucial = True
            chunks.append(Chunk(" ".join(cur), cur_idx, len(cur),
                                crucial, colors))
            prev = cur[-1]
            cur = []
    return chunks


def scene_mood(narration: str) -> str:
    """danger | success | neutral — drives the sentiment color grade."""
    words = {_clean_word(w).lower() for w in narration.split()}
    d = len(words & DANGER)
    s = len(words & SUCCESS)
    if d >= 2 and d > s:
        return "danger"
    if s >= 2 and s > d:
        return "success"
    return "neutral"


def forced_scene_events(text, window, si, colorize=True, hold=(1.8, 4.2)):
    """Turn an editor's PINNED on-screen text for a scene into placed events,
    spread evenly across the scene window. These are guaranteed to appear."""
    chunks = chunk_scene(text, colorize=colorize)
    for ch in chunks:
        ch.crucial = True
    if not chunks:
        return []
    w0, w1 = window
    span = max(0.1, (w1 - w0) - 0.6)
    step = span / len(chunks)
    out = []
    for k, ch in enumerate(chunks):
        t = w0 + 0.3 + k * step
        dur = min(hold[1], max(hold[0], 0.45 * ch.n_words + 1.2))
        t_end = min(t + dur, w1 - 0.15)
        if t_end - t >= 0.55:
            out.append([t, t_end, si, ch])
    return out


def select_text_events(scene_chunks, windows, first_dense_secs=60.0,
                       max_quiet=38.0, min_gap=6.0, dense_gap=3.4,
                       hold=(1.8, 4.2)):
    """Decide WHICH chunks become on-screen text and WHEN.

    Returns [(t_start, t_end, scene_i, Chunk)]. Policy (NOT captions):
      - first `first_dense_secs`: punchy text but spaced >= dense_gap apart
        (crucial words can appear sooner) — an accent, not a transcript
      - after: crucial moments only, >= min_gap apart
      - forced refresher if > max_quiet with nothing on screen
    """
    events, last_end = [], -min_gap
    for si, (chunks, win, ntext, wtimes) in enumerate(scene_chunks):
        for ch in chunks:
            # exact spoken time of this chunk's first word (whisper) or interp
            idx = min(ch.word_index, len(wtimes) - 1) if wtimes else 0
            t = wtimes[idx] if wtimes else (win[0] + (win[1] - win[0])
                                            * ch.word_index / max(1, len(ntext.split())))
            dense = t < first_dense_secs
            due = (t - last_end) >= max_quiet
            if not (dense or ch.crucial or due):
                continue
            spacing = dense_gap if dense else min_gap
            if (t - last_end) < spacing and not ch.crucial:
                continue
            dur = min(hold[1], max(hold[0], 0.45 * ch.n_words + 1.2))
            t_end = min(t + dur, win[1] - 0.15)
            if t_end - t < 0.8:
                continue
            events.append([t, t_end, si, ch])
            last_end = t_end
    # HARD RULE: events never overlap in time — the previous text rolls off
    # the moment the next one lands (like the Gus Fring reference edit)
    events.sort(key=lambda e: e[0])
    for a, b in zip(events, events[1:]):
        if a[1] > b[0] - 0.08:
            a[1] = max(a[0] + 0.7, b[0] - 0.08)
    return [tuple(e) for e in events if e[1] - e[0] >= 0.55]
