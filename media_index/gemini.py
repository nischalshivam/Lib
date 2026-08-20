"""A vision model that looks at candidate frames and picks the right one.

This is the one thing local retrieval genuinely cannot do, proven on a real
build. The "Why Gus Killed Victor Without a Word" essay is 36 beats, and
twenty of them are **silent** — a man changes his clothes, picks up a box
cutter, cuts a throat, rings a bell, straightens his tie. No dialogue means
no anchor, so those twenty beats were placed by interpolating between distant
spoken lines, and they landed minutes from where they belong: the bell,
really at 37:42, was laid at 32:25.

Local picture search (SigLIP) was asked and abstained — "the picture has no
opinion about where this run happens." It scores a frame against a caption
and, on these scenes, the right frame does not beat the noise floor. That is
its honest ceiling.

A large vision model does not have that ceiling. Shown twelve frames and
asked "which one shows an old man's hand striking a small bell", it answers.
So the division of labour is exact, and it is the one the GPT handoff argued
for:

    local dialogue search   -> WHICH scene, to the millisecond, when a line
                               of that scene was spoken
    this module             -> WHICH frame inside a candidate window, when
                               nothing was spoken

It is a **verifier and reranker, never a source of truth.** It is never
asked "where in this movie is X" — that fails, it is slow, and it
hallucinates. It is only ever handed a bounded window that local retrieval
already chose, a handful of frames sampled from it, and a yes/which
question. If it is not configured, errors, or abstains, the build is exactly
the build that ran without it. Nothing here can ever make a build worse than
interpolation already was; it can only move a guessed shot onto a frame a
model actually looked at.

## Configuration

Read from `settings.txt` beside the tool (or environment), never from code:

    gemini_key=<your-secret-key>
    gemini_base=https://.../v1
    gemini_model=gemini-2.5-flash

The key is a secret and lives only in that file, which is not in the
repository. This module never logs it and never writes it anywhere.
"""
from __future__ import annotations

import base64
import json
import os
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

# OpenAI-compatible chat-completions is what the proxy speaks, so the request
# shape here is the same one every such endpoint accepts: a model, a list of
# messages, and image parts carried as data: URIs.
DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_TIMEOUT_S = 60

# Transient-failure retry. Cataloguing fires many calls concurrently, so a
# short rate-limit (429) or a blip (500/502/503, timeout) is expected and must
# not turn into a silently blank shot. Retry those a few times with growing
# backoff; a real error (bad key, 400) is returned at once, never retried.
MAX_RETRIES = 6
RETRY_BASE_S = 2.0
_RETRY_ON = ("HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504",
             "network", "timed out")
# A verdict below this is treated as "not sure", and the shot keeps the
# position interpolation already gave it rather than moving on a weak guess.
MIN_CONFIDENCE = 0.55


def _settings_file() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "settings.txt")


def _from_settings() -> dict:
    """The `key=value` lines of settings.txt, or {} if there is no file.

    Parsed the same way start.bat writes it, so a value with an `=` in it
    (a URL query, say) keeps everything after the first `=`.
    """
    out: dict = {}
    try:
        with open(_settings_file(), "r", encoding="utf-8-sig") as f:
            for line in f:
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    out[k.strip().lower()] = v.strip()
    except OSError:
        return {}
    return out


@dataclass
class Config:
    key: str = ""
    base: str = ""
    model: str = DEFAULT_MODEL

    @property
    def ok(self) -> bool:
        return bool(self.key and self.base)

    @property
    def endpoint(self) -> str:
        return self.base.rstrip("/") + "/chat/completions"


def config() -> Config:
    """settings.txt first, then environment. Secrets never come from code.

    The file is the surface the user edits and reasonably expects to win. An
    environment variable is the classic silent trap: a stale, short-lived
    token (a Google `AQ.` key that dies in an hour) left in the shell from an
    earlier attempt, which then overrides a perfectly good key in the file and
    fails every request with "invalid token" — a real user lost a long time to
    exactly this. So the file wins wherever it defines a value, and the
    environment is only a fallback for whatever the file leaves blank.
    """
    s = _from_settings()
    return Config(
        key=s.get("gemini_key") or os.environ.get("GEMINI_API_KEY", ""),
        base=s.get("gemini_base") or os.environ.get("GEMINI_BASE_URL", ""),
        model=s.get("gemini_model") or os.environ.get("GEMINI_MODEL")
        or DEFAULT_MODEL,
    )


def key_source() -> str:
    """Where the active key comes from: 'settings.txt' | 'environment' | ''."""
    if _from_settings().get("gemini_key"):
        return "settings.txt"
    if os.environ.get("GEMINI_API_KEY"):
        return "environment"
    return ""


def available() -> tuple:
    """(usable, why-not). Never raises, so a caller can guard cheaply."""
    cfg = config()
    if not cfg.key:
        return False, "gemini_key settings.txt me nahi hai"
    if not cfg.base:
        return False, "gemini_base (endpoint URL) settings.txt me nahi hai"
    # The single most likely paste mistake: the API *documentation* page
    # instead of the API host. apifox.cn hosts docs; requests to it return
    # HTML, not a completion, and the failure would look like a dead model.
    # Cheaper to name it here than to let a build silently skip the step.
    if "apifox" in cfg.base.lower():
        return False, ("gemini_base me documentation page ka link hai "
                       "(apifox.cn). Asli API base URL chahiye — yunwu ke "
                       "liye aksar https://yunwu.ai/v1")
    return True, ""


# ---------------------------------------------------------------------------
# the question, and the answer
# ---------------------------------------------------------------------------

@dataclass
class Frame:
    """One candidate the model may choose. `at_s` is real episode time."""
    at_s: float
    jpeg: bytes


@dataclass
class Choice:
    """What the model decided, already sanity-checked."""
    index: int = -1                 # which frame, or -1 for "none of them"
    at_s: float = 0.0
    confidence: float = 0.0
    reason: str = ""

    @property
    def chose(self) -> bool:
        return self.index >= 0 and self.confidence >= MIN_CONFIDENCE


def _data_uri(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")


def _img_uri(data: bytes) -> str:
    """A data URI whose mime is guessed from the bytes — reference photos come
    as jpg/png/webp, and a jpeg label on a png makes some proxies reject it."""
    if data[:8].startswith(b"\x89PNG"):
        mime = "image/png"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        mime = "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def build_messages(intent: str, must_be_visible: list, frames: list) -> list:
    """The prompt. One system rule, then the frames, numbered, then the ask.

    The numbering matters: the model is told to answer with the frame's
    number and nothing generative decides position — the number maps back to
    a real timestamp this module already knows.
    """
    people = ", ".join(must_be_visible) if must_be_visible else ""
    rules = (
        "You are choosing which of several still frames best matches a "
        "moment described for a video essay. The frames are numbered, in "
        "order, and are all from the correct scene. Choose the ONE frame "
        "that best shows the described moment.\n\n"
        "Answer ONLY with strict JSON:\n"
        '{\"frame\": <number or -1>, \"confidence\": <0..1>, '
        '\"reason\": \"<short>\"}\n\n'
        "Rules:\n"
        "- Judge only what is visibly in the frame. Do not guess.\n"
        "- If NONE of the frames clearly shows the moment, answer frame -1.\n"
        "- confidence is how sure you are the chosen frame shows it."
    )
    ask = f"The moment: {intent}"
    if people:
        ask += f"\nMust be visible on screen: {people}"
    ask += f"\n\nThere are {len(frames)} frames, numbered 1 to {len(frames)}."

    content = [{"type": "text", "text": ask}]
    for i, fr in enumerate(frames, 1):
        content.append({"type": "text", "text": f"Frame {i}:"})
        content.append({"type": "image_url",
                        "image_url": {"url": _data_uri(fr.jpeg)}})
    return [
        {"role": "system", "content": rules},
        {"role": "user", "content": content},
    ]


def parse_verdict(text: str, frames: list) -> Choice:
    """The model's JSON answer, mapped back to a real timestamp.

    Tolerant of the usual chat-model wrapping — a fenced block, a stray
    sentence before the brace — because the useful content is the object and
    losing a good answer to a code fence would be a poor trade.
    """
    raw = (text or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return Choice()
    try:
        obj = json.loads(raw[start:end + 1])
    except (ValueError, TypeError):
        return Choice()

    try:
        n = int(obj.get("frame", -1))
    except (TypeError, ValueError):
        n = -1
    try:
        conf = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    reason = str(obj.get("reason") or "")[:200]

    if n < 1 or n > len(frames):
        return Choice(index=-1, confidence=conf, reason=reason)
    fr = frames[n - 1]
    return Choice(index=n - 1, at_s=fr.at_s, confidence=conf, reason=reason)


def call(cfg: Config, messages: list) -> tuple:
    """A model call that survives transient failure. Returns (text_or_None,
    detail). Retries a rate-limit or a server blip with growing backoff — the
    thing that lets the catalogue fan many calls out at once without turning
    every throttled one into a blank shot — and returns a real error at once.
    """
    detail = ""
    for attempt in range(MAX_RETRIES):
        text, detail = _call_once(cfg, messages)
        if text is not None:
            return text, detail
        if not any(sig in detail for sig in _RETRY_ON):
            return None, detail                      # real error — do not retry
        if attempt < MAX_RETRIES - 1:
            back = RETRY_BASE_S * (2 ** attempt)     # 2, 4, 8, 16, 32s
            # Jitter: several workers rate-limited at once must not all retry on
            # the same beat, or they just re-collide and burn attempts for
            # nothing. A random slice spreads them out.
            time.sleep(back + random.uniform(0, back))
    return None, detail


def _call_once(cfg: Config, messages: list) -> tuple:
    """One HTTP call. Returns (assistant_text_or_None, detail).

    `detail` is a short human string naming exactly what went wrong — the
    HTTP status and the first of the body, or the network error. `verify`
    throws the detail away because a verifier must never break a build; the
    `mi gemini` check keeps it, because "koi jawab nahi aaya" with no reason
    is exactly what left a real user stuck with a working key.

    The response is read defensively: an OpenAI-compatible proxy returns
    `choices[0].message.content`, but an error comes back as `{"error": ...}`
    with a 200 on some proxies, so both shapes are recognised.
    """
    payload = json.dumps({
        "model": cfg.model,
        "messages": messages,
        "temperature": 0,
        # 2000, not 300: gemini-2.5-flash is a THINKING model — it spends
        # tokens reasoning BEFORE the answer, and that counts against this
        # ceiling. At 300, a shot with many reference faces used the whole
        # budget thinking and the JSON came back cut off (finish_reason
        # "length") — parsed to nothing, stored as a blank shot. A higher
        # ceiling costs nothing extra when unused (the model still stops at
        # "stop" when the answer is done); it just stops truncating.
        "max_tokens": 2000,
    }).encode("utf-8")
    req = urllib.request.Request(
        cfg.endpoint, data=payload, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg.key}"})
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8", "replace")
            code = resp.getcode()
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return None, f"HTTP {exc.code} — {body or exc.reason}"
    except (urllib.error.URLError, OSError) as exc:
        return None, f"network: {getattr(exc, 'reason', exc)}"

    try:
        data = json.loads(raw)
    except ValueError:
        return None, f"HTTP {code} par jawab JSON nahi tha: {raw[:200]}"
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        msg = err.get("message") if isinstance(err, dict) else err
        return None, f"API error: {str(msg)[:250]}"
    try:
        return data["choices"][0]["message"]["content"], ""
    except (KeyError, IndexError, TypeError):
        return None, f"jawab ka shape anjaan tha: {raw[:200]}"


def _post(cfg: Config, messages: list) -> str:
    """The assistant text, or '' on any failure — the build-safe wrapper.

    Swallows the detail on purpose: a verifier that raises, or that makes a
    build wait on a reason nobody will read, is worse than one that quietly
    contributes nothing.
    """
    text, _detail = call(cfg, messages)
    return text or ""


# The smallest valid JPEG there is: a 1x1 white pixel. Enough to prove the
# image path of a request works without shipping a real frame.
_PIXEL = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUG"
    "CQgKCgkICQkKDA8MCgsOCwkJDRENDg8QEBEQCgwSExIQEw8QEBD/2wBDAQMDAwQDBAgE"
    "BAgQCwkLEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQ"
    "EBAQEBD/wAARCAABAAEDASIAAhEBAxEB/8QAFAABAAAAAAAAAAAAAAAAAAAAAv/EABQQ"
    "AQAAAAAAAAAAAAAAAAAAAAD/xAAUAQEAAAAAAAAAAAAAAAAAAAAA/8QAFBEBAAAAAAAA"
    "AAAAAAAAAAAAAAD/2gAMAwEAAhEDEQA/AL+AAf/Z")


def ping(cfg: Config, with_image: bool = False) -> tuple:
    """A tiny real request, for the check. Returns (ok, detail).

    Text first so auth and endpoint are proven before the image path is
    tried — if a key works for text and fails for an image, that narrows the
    fault to the multimodal request, which is a completely different fix from
    a bad key.
    """
    if with_image:
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "Reply with the single word OK."},
            {"type": "image_url",
             "image_url": {"url": _data_uri(_PIXEL)}}]}]
    else:
        msgs = [{"role": "user", "content": "Reply with the single word OK."}]
    text, detail = call(cfg, msgs)
    if text is not None:
        return True, text.strip()[:120]
    return False, detail


def confirm_messages(description: str, characters: list, frames: list,
                     refs: dict | None = None) -> list:
    """Ask, yes/no, whether these frames show what the script described.

    `refs` is {character_name: [reference_jpeg, ...]} — a few real photos of
    each required person. Shown FIRST, they turn "is this plausibly Victor?"
    (a guess) into "is the man in the frame the SAME man as these reference
    photos?" (a comparison), which is the only way to tell Victor from Hank.
    """
    refs = refs or {}
    who = ", ".join(characters) if characters else ""
    id_rule = (
        "- IDENTITY (the whole point of this check): reference photos of the "
        "required people are given first, labelled by name. The required "
        "person must be the SAME person as their reference photos. A DIFFERENT "
        "actor/character standing in for them is match=false — this is the one "
        "thing you must catch. But judge identity by the FACE/person, not by "
        "wardrobe: the same person in different clothes, a hazmat suit, a "
        "different haircut, or a different scene is STILL the same person → "
        "match=true."
        if refs else
        "- If specific people are named, they should be plausibly on screen. "
        "A clearly different, identifiable person in their place is match=false.")
    rules = (
        "You are quality-checking footage for a video essay. The clip plays "
        "UNDER a narrator's line — it does NOT need to match the description "
        "frame-for-frame. Your only job is to catch clips that would MISLEAD "
        "the viewer: the wrong person, or a clearly unrelated scene. You are "
        "NOT enforcing exact wardrobe, props, camera angle, or micro-action.\n\n"
        "Answer ONLY strict JSON:\n"
        '{"match": true|false, "confidence": <0..1>, "reason": "<short>"}\n\n'
        "Rules:\n"
        "- match=TRUE if the required person/subject is present and the setting "
        "is broadly consistent with the moment — EVEN IF clothing, exact "
        "action, angle, or small details differ. Playing a shot of the right "
        "character under a line about them is correct and expected.\n"
        "- match=FALSE only for a genuine mismatch: the WRONG person, a clearly "
        "unrelated scene/location, an empty/black frame, a blurry unusable "
        "frame, or a text/logo card.\n"
        f"{id_rule}\n"
        "- Do NOT reject a correct person for different clothes or a slightly "
        "different action. When the right person is plausibly on screen, "
        "prefer match=true."
    )
    content = []
    for name, imgs in refs.items():
        for photo in imgs[:3]:
            content.append({"type": "text", "text": f"Reference — {name}:"})
            content.append({"type": "image_url",
                            "image_url": {"url": _img_uri(photo)}})
    ask = f"Described moment: {description}"
    if who:
        ask += f"\nMust be the SAME person(s) as the reference photos: {who}" \
            if refs else f"\nMust plausibly show: {who}"
    content.append({"type": "text", "text": ask})
    for i, jpeg in enumerate(frames, 1):
        content.append({"type": "text", "text": f"Candidate frame {i}:"})
        content.append({"type": "image_url",
                        "image_url": {"url": _data_uri(jpeg)}})
    return [{"role": "system", "content": rules},
            {"role": "user", "content": content}]


def confirm_shot(description: str, characters: list, frames: list,
                 refs: dict | None = None, cfg: Config | None = None) -> tuple:
    """(matches, confidence, reason). Never raises.

    The visual verification pass a friend's brief calls the thing that keeps
    the final videos accurate rather than approximate: before a clip is used,
    a second look confirms it actually shows what the line is about — and, with
    `refs` (reference photos per character), that it is the RIGHT person, not
    just a plausible one. Fails OPEN by design — not configured, network error,
    or an unparseable answer returns (True, 0.0, ...) so a verifier that is
    down never blocks a build; only a clear, confident "no" rejects a shot.
    """
    if not frames:
        return True, 0.0, "no frames to check"
    cfg = cfg or config()
    if not cfg.ok:
        return True, 0.0, "gemini not configured"
    text, detail = call(cfg, confirm_messages(description, characters, frames,
                                              refs=refs))
    if text is None:
        return True, 0.0, f"verifier unreachable ({detail[:40]})"
    raw = text.strip()
    a, b = raw.find("{"), raw.rfind("}")
    if a < 0 or b <= a:
        return True, 0.0, "unparseable"
    try:
        obj = json.loads(raw[a:b + 1])
    except (ValueError, TypeError):
        return True, 0.0, "unparseable"
    match = bool(obj.get("match", True))
    try:
        conf = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    return match, conf, str(obj.get("reason") or "")[:120]


def verify(intent: str, frames: list, must_be_visible=None,
           cfg: Config | None = None) -> Choice:
    """Ask the model which frame is the moment. Never raises.

    Returns a Choice whose `.chose` is True only when the model both picked a
    frame and was confident enough. Everything else — not configured, network
    error, abstention, low confidence — comes back as a Choice that does not
    choose, and the caller leaves the shot exactly where it was.
    """
    if not frames:
        return Choice()
    cfg = cfg or config()
    if not cfg.ok:
        return Choice()
    text = _post(cfg, build_messages(intent, must_be_visible or [], frames))
    if not text:
        return Choice()
    return parse_verdict(text, frames)
