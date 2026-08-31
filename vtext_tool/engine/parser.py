"""Instruction-file parser + validator (format: VTEXT INSTRUCTION FILE v1)."""
from __future__ import annotations

import re

EVENT_TYPES = {
    "HOOK", "REVELATION", "EMOTIONAL_PEAK", "CONTRAST", "QUESTION",
    "IMPORTANT_FACT", "NUMBER_OR_DATE", "CHARACTER_INSIGHT", "QUOTE",
    "CHAPTER_TRANSITION", "SETUP", "NORMAL_EXPLANATION", "BREATHING_MOMENT",
}
INTENSITIES = {"HIGH", "MEDIUM", "LOW"}
TEXT_ROLES = {"IMPACT", "INFORMATION", "EMOTION", "CONTEXT", "TRANSITION"}
FREEDOMS = {"LOW", "MEDIUM", "HIGH"}
NICHES = {"CARTOON_ESSAY", "MOVIE_ESSAY", "CLASSIC_MOVIE", "SITCOM_ESSAY",
          "DARK_PSYCHOLOGY", "HISTORY_DOC", "TRUE_CRIME", "SPORTS"}

_FIELDS = ("NARRATION_CUE", "EVENT_TYPE", "DISPLAY_TEXT", "EMPHASIS_WORDS",
           "INTENSITY", "TEXT_ROLE", "VISUAL_FREEDOM", "SEQUENCE_GROUP")


def _clean(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1]
    return v.strip()


def parse_instruction_file(path: str):
    """Returns (header: dict, events: list[dict], problems: list[str]).

    Parsing is forgiving (extra blank lines, stray spacing, case of field
    names) but validation is strict enough to catch broken files early.
    """
    text = open(path, encoding="utf-8-sig").read()
    problems, header, events = [], {}, []

    if "VTEXT INSTRUCTION FILE" not in text.split("\n", 1)[0].upper():
        problems.append("Header line '=== VTEXT INSTRUCTION FILE v1 ===' missing "
                        "(continuing anyway).")

    blocks = re.split(r"^---\s*EVENT\s+(\d+)\s*---\s*$", text,
                      flags=re.M | re.I)
    head_txt = blocks[0]
    for line in head_txt.splitlines():
        m = re.match(r"^\s*(VIDEO_TITLE|NICHE|LANGUAGE|TOTAL_EVENTS)\s*:\s*(.+)$",
                     line, re.I)
        if m:
            header[m.group(1).upper()] = _clean(m.group(2))
    header.setdefault("LANGUAGE", "en")
    niche = header.get("NICHE", "").upper().replace(" ", "_")
    if niche and niche not in NICHES:
        problems.append(f"Unknown NICHE '{niche}' — falling back to MOVIE_ESSAY.")
        niche = "MOVIE_ESSAY"
    header["NICHE"] = niche or "MOVIE_ESSAY"

    # blocks: [head, num, body, num, body, ...]
    for i in range(1, len(blocks) - 1, 2):
        num, body = int(blocks[i]), blocks[i + 1]
        ev = {"num": num}
        for line in body.splitlines():
            m = re.match(r"^\s*([A-Z_]+)\s*:\s*(.*)$", line.strip(), re.I)
            if m and m.group(1).upper() in _FIELDS:
                ev[m.group(1).upper()] = _clean(m.group(2))
        missing = [f for f in _FIELDS if f not in ev]
        if missing:
            problems.append(f"EVENT {num:03d}: missing fields {missing} — skipped.")
            continue

        et = ev["EVENT_TYPE"].upper()
        if et not in EVENT_TYPES:
            problems.append(f"EVENT {num:03d}: unknown EVENT_TYPE '{et}' — "
                            f"treated as NORMAL_EXPLANATION.")
            et = "NORMAL_EXPLANATION"
        ev["EVENT_TYPE"] = et
        ev["INTENSITY"] = (ev["INTENSITY"].upper()
                           if ev["INTENSITY"].upper() in INTENSITIES else "MEDIUM")
        ev["TEXT_ROLE"] = (ev["TEXT_ROLE"].upper()
                           if ev["TEXT_ROLE"].upper() in TEXT_ROLES else "IMPACT")
        ev["VISUAL_FREEDOM"] = (ev["VISUAL_FREEDOM"].upper()
                                if ev["VISUAL_FREEDOM"].upper() in FREEDOMS
                                else "MEDIUM")
        sg = ev["SEQUENCE_GROUP"].upper()
        ev["SEQUENCE_GROUP"] = None if sg in ("NONE", "") else sg

        cue_words = ev["NARRATION_CUE"].split()
        if not (3 <= len(cue_words) <= 16):
            problems.append(f"EVENT {num:03d}: NARRATION_CUE has {len(cue_words)} "
                            f"words (expected 5-12) — will still try to match.")

        if et == "BREATHING_MOMENT" or ev["DISPLAY_TEXT"].upper() == "NONE":
            ev["lines"] = []
            ev["EVENT_TYPE"] = "BREATHING_MOMENT"
        else:
            lines = [l.strip() for l in ev["DISPLAY_TEXT"].split("/") if l.strip()]
            if not lines:
                problems.append(f"EVENT {num:03d}: empty DISPLAY_TEXT — skipped.")
                continue
            if len(lines) > 4:
                problems.append(f"EVENT {num:03d}: {len(lines)} lines "
                                f"(max 4) — extra lines merged.")
                lines = lines[:3] + [" ".join(lines[3:])]
            total_words = sum(len(l.split()) for l in lines)
            if total_words > 12:
                problems.append(f"EVENT {num:03d}: DISPLAY_TEXT has {total_words} "
                                f"words (recommended <=9).")
            ev["lines"] = lines

            emph = ev["EMPHASIS_WORDS"]
            if emph.upper() == "NONE":
                ev["EMPHASIS_WORDS"] = ""
            else:
                joined = " ".join(" ".join(lines).lower().split())
                if emph.lower() not in joined:
                    # tolerate emphasis word-set instead of exact substring
                    dwords = set(joined.replace(".", "").replace(",", "").split())
                    ewords = [w.strip(".,").lower() for w in emph.split()]
                    if not all(w in dwords for w in ewords):
                        problems.append(f"EVENT {num:03d}: EMPHASIS_WORDS "
                                        f"'{emph}' not found inside DISPLAY_TEXT "
                                        f"— accent skipped.")
                        ev["EMPHASIS_WORDS"] = ""
        events.append(ev)

    try:
        declared = int(header.get("TOTAL_EVENTS", len(events)))
        if declared != len(events):
            problems.append(f"TOTAL_EVENTS says {declared} but "
                            f"{len(events)} valid events parsed.")
    except ValueError:
        pass
    if not events:
        problems.append("No valid events parsed — nothing to render.")
    return header, events, problems
