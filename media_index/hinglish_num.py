"""Numbers -> spoken Hinglish words (Devanagari), for the Hindi voiceover.

OmniVoice reads digits in a Hindi script badly: on a measured 1-min test
(2026-10-01) "$80,000" came out as "अतोज अजाजार", "99.1%" as gibberish and
"S02E12" as noise, while the same script with the numbers WRITTEN AS WORDS
("अस्सी हज़ार डॉलर", "निन्यानवे पॉइंट एक परसेंट", "सीज़न टू, एपिसोड ट्वेल्व")
was spoken perfectly. The num2words library has no Hindi, and the OmniVoice
wrapper's normaliser even splits "80,000" into "80, 000". So the studio spells
every number out itself before a Hindi script goes to the voice.

Only the text SENT TO THE VOICE changes — the clean script on disk is untouched.
"""
from __future__ import annotations

import re

_HI = ("शून्य एक दो तीन चार पाँच छह सात आठ नौ दस ग्यारह बारह तेरह चौदह पंद्रह "
       "सोलह सत्रह अठारह उन्नीस बीस इक्कीस बाईस तेईस चौबीस पच्चीस छब्बीस सत्ताईस "
       "अट्ठाईस उनतीस तीस इकतीस बत्तीस तैंतीस चौंतीस पैंतीस छत्तीस सैंतीस अड़तीस "
       "उनतालीस चालीस इकतालीस बयालीस तैंतालीस चौवालीस पैंतालीस छियालीस सैंतालीस "
       "अड़तालीस उनचास पचास इक्यावन बावन तिरेपन चौवन पचपन छप्पन सत्तावन अट्ठावन उनसठ "
       "साठ इकसठ बासठ तिरेसठ चौंसठ पैंसठ छियासठ सड़सठ अड़सठ उनहत्तर सत्तर इकहत्तर "
       "बहत्तर तिहत्तर चौहत्तर पचहत्तर छिहत्तर सतहत्तर अठहत्तर उन्यासी अस्सी इक्यासी "
       "बयासी तिरासी चौरासी पचासी छियासी सत्तासी अट्ठासी नवासी नब्बे इक्यानवे बानवे "
       "तिरानवे चौरानवे पंचानवे छियानवे सत्तानवे अट्ठानवे निन्यानवे").split()
assert len(_HI) == 100

# How a Hinglish narrator says small counts after "season"/"episode": English.
_EN = ("ज़ीरो वन टू थ्री फ़ोर फ़ाइव सिक्स सेवन एट नाइन टेन इलेवन ट्वेल्व थर्टीन "
       "फ़ोर्टीन फ़िफ़्टीन सिक्सटीन सेवनटीन एटीन नाइनटीन ट्वेंटी").split()
_ORD = {1: "पहला", 2: "दूसरा", 3: "तीसरा", 4: "चौथा", 6: "छठा"}
_SCALE = {"million": "मिलियन", "मिलियन": "मिलियन", "m": "मिलियन",
          "billion": "बिलियन", "बिलियन": "बिलियन", "bn": "बिलियन", "b": "बिलियन",
          "thousand": "हज़ार", "हज़ार": "हज़ार", "हजार": "हज़ार", "k": "हज़ार",
          "lakh": "लाख", "लाख": "लाख", "crore": "करोड़", "करोड़": "करोड़"}
_CUR = {"$": "डॉलर", "₹": "रुपये", "£": "पाउंड", "€": "यूरो"}
_EN_CONTEXT = re.compile(r"(सीज़न|सीजन|season|एपिसोड|episode|पार्ट|part|चैप्टर|chapter)\s*$",
                         re.I)


def int_words(n: int) -> str:
    """Indian-system Hindi words: 2008 -> दो हज़ार आठ, 1999 -> उन्नीस सौ निन्यानवे."""
    if n < 0:
        return "माइनस " + int_words(-n)
    if n < 100:
        return _HI[n]
    if 1100 <= n <= 1999:                       # "उन्नीस सौ निन्यानवे", "पंद्रह सौ"
        rest = n % 100
        return _HI[n // 100] + " सौ" + (" " + _HI[rest] if rest else "")
    parts = []
    for size, word in ((10_000_000, "करोड़"), (100_000, "लाख"), (1000, "हज़ार"),
                       (100, "सौ")):
        q, n = divmod(n, size)
        if q:
            parts.append(int_words(q) + " " + word)
    if n:
        parts.append(_HI[n])
    return " ".join(parts)


def num_words(text: str) -> str:
    """'99.1' -> निन्यानवे पॉइंट एक ; '1,00,000' -> एक लाख."""
    text = text.replace(",", "")
    if "." in text:
        whole, frac = text.split(".", 1)
        frac = frac.rstrip("0") or "0"
        return (int_words(int(whole or 0)) + " पॉइंट "
                + " ".join(_HI[int(d)] for d in frac if d.isdigit()))
    return int_words(int(text))


_NUM = r"\d[\d,]*(?:\.\d+)?"
_SCALE_RE = r"(million|billion|thousand|lakh|crore|मिलियन|बिलियन|हज़ार|हजार|लाख|करोड़|bn|k|m|b)"
_RE_SE = re.compile(r"\bS(\d{1,2})\s*[-.]?\s*E(\d{1,3})\b", re.I)
_RE_MONEY = re.compile(r"([$₹£€])\s?(" + _NUM + r")(?:\s?" + _SCALE_RE + r"(?![A-Za-z]))?", re.I)
_RE_SCALED = re.compile(r"(" + _NUM + r")\s?" + _SCALE_RE + r"(?![A-Za-zऀ-ॿ])", re.I)
_RE_PCT = re.compile(r"(" + _NUM + r")\s?%")
_RE_ORD = re.compile(r"\b(\d+)(st|nd|rd|th)\b", re.I)
_RE_ANY = re.compile(_NUM)


_EN_TENS = {2: "ट्वेंटी", 3: "थर्टी", 4: "फ़ोर्टी", 5: "फ़िफ़्टी", 6: "सिक्सटी",
            7: "सेवेंटी", 8: "एटी", 9: "नाइंटी"}


def en_words(n: int) -> str:
    """English number words in Devanagari, as a Hinglish narrator says codes:
    16 -> सिक्सटीन, 47 -> फ़ोर्टी-सेवन; longer numbers digit by digit."""
    if 0 <= n < len(_EN):
        return _EN[n]
    if n < 100:
        return _EN_TENS[n // 10] + ("-" + _EN[n % 10] if n % 10 else "")
    return " ".join(_EN[int(d)] for d in str(n))


def _small_en(n: int) -> str:
    return _EN[n] if 0 <= n < len(_EN) else int_words(n)


# a product/model code: letters, a hyphen, digits — "टीएच-16", "AK-47", "F-16".
# Narrators read these in English ("टीएच-सिक्सटीन"), not as Hindi counts.
_RE_CODE = re.compile(r"(?<=[A-Za-zऀ-ॿ])-(\d{1,4})\b")


def normalize(text: str) -> tuple:
    """(text with every number spelled out, how many were replaced)."""
    count = 0

    def sub(rx, fn, s):
        nonlocal count

        def rep(m):
            nonlocal count
            count += 1
            return fn(m)
        return rx.sub(rep, s)

    t = sub(_RE_SE, lambda m: f"सीज़न {_small_en(int(m.group(1)))}, "
                              f"एपिसोड {_small_en(int(m.group(2)))}", text)
    t = sub(_RE_CODE, lambda m: "-" + en_words(int(m.group(1))), t)
    t = sub(_RE_MONEY, lambda m: " ".join(x for x in (
        num_words(m.group(2)), _SCALE.get((m.group(3) or "").lower(), ""),
        _CUR[m.group(1)]) if x), t)
    t = sub(_RE_SCALED, lambda m: num_words(m.group(1)) + " "
            + _SCALE[m.group(2).lower()], t)
    t = sub(_RE_PCT, lambda m: num_words(m.group(1)) + " परसेंट", t)
    t = sub(_RE_ORD, lambda m: _ORD.get(int(m.group(1)),
                                        int_words(int(m.group(1))) + "वां"), t)

    def plain(m):
        before = t_now[:m.start()]
        n = m.group(0)
        if "." not in n and _EN_CONTEXT.search(before):
            return _small_en(int(n.replace(",", "")))
        return num_words(n)

    t_now = t
    out, last = [], 0
    for m in _RE_ANY.finditer(t):
        out.append(t[last:m.start()])
        out.append(plain(m))
        count += 1
        last = m.end()
    out.append(t[last:])
    return "".join(out), count
