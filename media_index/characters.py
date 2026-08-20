"""Who this essay is about — read straight from the script.

Before a single frame is placed, a person should be told exactly whose face
the tool will need reference photos of. On a Gus essay that was Walter,
Jesse, Mike, Gus, Victor; on a Joker essay it is Arthur, Murray, Penny. The
script already knows — every shot the visual model wrote carries a
`characters` list, and the ones it did not name still mention the people in
their `visual` captions and in the narration. This module reads that and
hands back a ranked list, most central first, so the New Video page can say
"make a folder of photos for these people" the moment the script is chosen —
not forty minutes into a build, and not left for the user to guess.

It is deliberately model-free: counting names the script already wrote is
instant, offline, and cannot hallucinate a character who is not in the file.
A later pass can ask a vision/chat model to merge aliases ("Arthur"/"Joker"
are one man), but the list a person needs to start gathering photos should
never wait on a network call.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

# Words that ride along in a `characters` field or a caption but are not a
# person anyone photographs. Kept small and specific: the cost of dropping a
# real name is worse than the cost of listing one crowd noun, so this only
# removes things that are never a named character.
_NOT_A_PERSON = {
    "crowd", "crowds", "people", "person", "man", "woman", "men", "women",
    "kid", "kids", "child", "children", "boy", "girl", "guy", "guys",
    "audience", "everyone", "someone", "nobody", "group", "others",
    "the kids", "a group", "a man", "a woman", "strangers", "stranger",
    "narrator", "viewer", "you", "them", "they", "us", "we",
    "voiceover", "commentary", "text", "title", "caption", "none", "n a",
}

# A plausible character name: one to four capitalised words, letters only
# (apostrophes and hyphens allowed inside). Rules out "02:00-05:00", stray
# sentence fragments, and lowercase common nouns that slipped into the field.
_NAME = re.compile(r"[A-Z][A-Za-z'’\-]+(?:\s+[A-Z][A-Za-z'’\-]+){0,3}")


@dataclass
class Character:
    name: str                 # the label shown to the user (best-seen form)
    mentions: int = 0         # how many shots/lines reference this person
    from_field: int = 0       # of those, how many were an explicit list entry
    aliases: set = field(default_factory=set)

    def as_dict(self) -> dict:
        return {"name": self.name, "mentions": self.mentions,
                "named_directly": self.from_field,
                "aliases": sorted(a for a in self.aliases if a != self.name),
                "photos_wanted": PHOTOS_WANTED}


# Enough angles/expressions to recognise a face across a film, few enough that
# a person will actually gather them. Matches the cast-folder guidance.
PHOTOS_WANTED = 7

# Below this share of the most-mentioned character, a name is a bit-part the
# essay glances at once, not someone whose face has to be right repeatedly.
# A reference folder for a one-line extra is effort spent where it will not
# move the video.
_MINOR_SHARE = 0.08


def _list_entries(value) -> list:
    """The names inside a `characters`/`people` field, however it was written.

    Visual scripts have carried this as a real JSON list, as a Python-literal
    string `"['Arthur', 'Murray']"`, and as a plain `"Arthur, Murray"`. All
    three mean the same thing and all three have to be read.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    text = str(value).strip()
    if not text:
        return []
    if text[:1] in "[(" and text[-1:] in ")]":
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, (list, tuple)):
                return [str(v) for v in parsed]
        except (ValueError, SyntaxError):
            pass
    return [p for p in re.split(r"[,;/&]| and ", text) if p.strip()]


def _clean(name: str) -> str:
    return re.sub(r"\s{2,}", " ", str(name).replace("’", "'")).strip(" '\"-.")


def _is_person(name: str) -> bool:
    low = name.lower()
    if not name or low in _NOT_A_PERSON or len(name) < 2:
        return False
    return bool(_NAME.fullmatch(name))


def from_beats(beats: list) -> list:
    """Ranked `Character` list for a parsed script, most central first.

    Reads the explicit `characters` field of every shot first, because that
    is the model saying who is *in the frame*. Then it looks for those same
    names in narration and captions, so a person mentioned ten times but
    listed twice still ranks by their real weight in the essay.
    """
    people: dict = {}

    def bump(raw_name: str, *, direct: bool) -> None:
        name = _clean(raw_name)
        if not _is_person(name):
            return
        key = name.lower()
        person = people.get(key)
        if person is None:
            person = people[key] = Character(name=name)
        person.mentions += 1
        if direct:
            person.from_field += 1
        person.aliases.add(name)

    # 1) explicit per-shot lists — the strongest signal, "who is on screen".
    for beat in beats:
        for shot in (beat.get("shots") or []):
            for entry in _list_entries(
                    shot.get("characters") or shot.get("people")):
                bump(entry, direct=True)

    if not people:
        return []

    # 2) the same names again wherever the prose mentions them, so ranking
    # reflects how much the essay actually leans on each face. Only names we
    # already know are counted here — free-text is too noisy to mint new
    # characters from without a model.
    known = {p.name.lower(): p for p in people.values()}
    for beat in beats:
        haystacks = [beat.get("narration") or "", beat.get("header") or ""]
        for shot in (beat.get("shots") or []):
            haystacks.append(str(shot.get("visual") or ""))
            haystacks.append(str(shot.get("note") or ""))
        blob = " " + re.sub(r"\s+", " ", " ".join(haystacks)).lower() + " "
        for key, person in known.items():
            # word-boundary count, so "Art" inside "Arthur" is not a hit
            person.mentions += len(re.findall(
                r"(?<![a-z])" + re.escape(key) + r"(?![a-z])", blob))

    ranked = sorted(people.values(),
                    key=lambda p: (-p.mentions, -p.from_field, p.name))
    top = ranked[0].mentions if ranked else 0
    cutoff = max(1, int(top * _MINOR_SHARE))
    return [p for p in ranked if p.mentions >= cutoff]


def needed(beats: list) -> dict:
    """The New Video page's answer to 'whose photos will you need?'.

    `main` are the people worth a reference folder; `minor` are named but too
    peripheral to be worth the effort, surfaced only so the user knows the
    tool saw them and chose to skip them.
    """
    ranked = from_beats(beats)
    return {
        "photos_each": PHOTOS_WANTED,
        "main": [c.as_dict() for c in ranked],
        "count": len(ranked),
    }
