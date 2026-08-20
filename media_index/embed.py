"""Turn a picture and a sentence into numbers that can be compared.

Everything upstream of this file locates footage by *proxy*. Dialogue search
knows where a line is spoken. Alignment knows the order the shots come in.
Neither has ever looked at a frame and asked the only question that actually
matters: **is this the picture the script described?**

That gap is why one wrong anchor has been able to ruin a whole run three
separate times. A guard can refuse a placement that is obviously absurd — a
run stretched across a sixth of an hour — but it cannot tell a correct frame
from a plausible one, because nothing in the pipeline can see.

This module is the eye. It loads an image-text model (SigLIP by default) and
gives two operations:

    encode_images(pixels) -> unit vectors, one per frame
    encode_texts(strings) -> unit vectors, one per description

A dot product between the two is then a similarity, and the whole
verification layer is built on that one number.

Three things are deliberate:

**No Pillow.** ffmpeg already hands over RGB at exactly the resolution the
model wants, so the preprocessing is `x / 127.5 - 1` on a numpy array — which
is precisely what SigLIP's own image processor does (resize to a square,
rescale by 1/255, normalise with mean 0.5 and std 0.5). Going through an
image library would mean encoding to JPEG and decoding it again for no gain.

**Padding to a fixed length.** SigLIP was trained with every caption padded to
64 tokens, and it is unusually sensitive to that — tokenising a short phrase
without the padding measurably changes its vector. This is the single most
common way to use the model wrongly, so it is not left to a caller.

**A swappable backend.** The real model needs ~2 GB of packages that have no
business being a hard dependency of a subtitle indexer. `available()` reports
honestly, everything that uses this module degrades to its old behaviour when
the answer is no, and the tests install a deterministic fake so the logic
around the model is covered without downloading a byte.
"""
from __future__ import annotations

import os
import zlib
from dataclasses import dataclass

import numpy as np

# Base is the right default. so400m scores better but runs roughly ten times
# slower on a CPU, and indexing a 47-minute episode at one frame every two
# seconds is 1,400 frames — minutes with base, most of an hour without.
# Anyone with a GPU can pass the larger name and will not notice.
DEFAULT_MODEL = "google/siglip-base-patch16-224"
IMAGE_SIZE = 224
TEXT_TOKENS = 64        # what SigLIP was trained with; see the docstring
IMAGE_BATCH = 16
TEXT_BATCH = 32


class EmbedError(RuntimeError):
    """The model could not be loaded or run."""


def models_dir() -> str:
    """Where downloaded weights live.

    Beside the tool rather than in a hidden cache under the user profile, so
    that "where did my two gigabytes go" has an answer you can point at, and
    so moving the folder to another machine moves the model with it.
    """
    override = os.environ.get("MEDIA_INDEX_MODELS")
    if override:
        return os.path.abspath(override)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "models")


def available() -> tuple[bool, str]:
    """(usable, reason). Never raises, never imports torch for real."""
    import importlib.util
    missing = [m for m in ("torch", "transformers")
               if importlib.util.find_spec(m) is None]
    if missing:
        return False, ("needs " + " and ".join(missing)
                       + " — run setup.bat and answer yes to the picture model")
    return True, "ready"


# ---------------------------------------------------------------------------
# backends
# ---------------------------------------------------------------------------

@dataclass
class Backend:
    """What the rest of the tool is allowed to assume about a model."""
    name: str
    dim: int
    device: str = "cpu"

    def encode_images(self, pixels: np.ndarray) -> np.ndarray:
        raise NotImplementedError

    def encode_texts(self, texts: list) -> np.ndarray:
        raise NotImplementedError


def unit(v: np.ndarray) -> np.ndarray:
    """L2-normalise rows, leaving a zero row as zeros rather than NaN."""
    v = np.asarray(v, dtype=np.float32)
    if v.ndim == 1:
        v = v[None, :]
    n = np.linalg.norm(v, axis=1, keepdims=True)
    return v / np.maximum(n, 1e-8)


class SigLIP(Backend):
    """The real thing: transformers + torch, on the CPU unless CUDA is there."""

    def __init__(self, model_name: str = DEFAULT_MODEL):
        ok, why = available()
        if not ok:
            raise EmbedError(why)
        import torch                                    # noqa: PLC0415
        from transformers import AutoModel, AutoTokenizer   # noqa: PLC0415

        cache = models_dir()
        os.makedirs(cache, exist_ok=True)
        try:
            self._torch = torch
            self._tok = AutoTokenizer.from_pretrained(model_name,
                                                      cache_dir=cache)
            self._model = AutoModel.from_pretrained(model_name,
                                                    cache_dir=cache)
        except Exception as exc:                        # network, disk, name
            raise EmbedError(
                f"could not load {model_name}: {exc}\n"
                f"      weights are cached in {cache}") from exc
        self._model.eval()
        # Not `torch.cuda.is_available()`. That is True on a Pascal card
        # holding a wheel with no sm_61 kernels — allocation works, `.to()`
        # works, and the first real multiply dies with "no kernel image is
        # available for execution on the device", forty minutes into an
        # index that reported GPU on its first line. `usable_device` runs
        # that multiply here, where failing costs nothing.
        from . import gpu as gpu_mod                    # noqa: PLC0415
        device = gpu_mod.usable_device()
        self._model.to(device)
        # Measured, not read off the config. `projection_dim` exists on some
        # versions and not others, and a wrong guess here would not raise —
        # it would silently mis-shape every comparison downstream. Encoding
        # one word costs nothing and cannot be wrong.
        super().__init__(name=model_name, dim=1, device=device)
        self.dim = int(self._encode_texts_raw(["a"]).shape[1])

    def _vectors(self, out):
        """The embedding, whichever shape this version of transformers used.

        transformers 4 returned a plain tensor from get_*_features.
        transformers 5 returns a BaseModelOutputWithPooling, whose embedding
        is `pooler_output` — and the old code's `.cpu()` on that object
        raises AttributeError. Both are in the wild, this tool cannot pin the
        version a user's pip resolves, and the failure is at load time on a
        machine that has just spent twenty minutes downloading, so it is
        worth handling rather than documenting.
        """
        pooled = getattr(out, "pooler_output", None)
        if pooled is None and isinstance(out, (tuple, list)):
            pooled = next((x for x in out
                           if hasattr(x, "ndim") and x.ndim == 2), None)
        if pooled is None:
            pooled = out
        if not hasattr(pooled, "cpu"):
            raise EmbedError(
                "the model returned something this tool does not recognise "
                f"({type(out).__name__}) — transformers may have changed")
        return pooled.detach().cpu().numpy()

    def encode_images(self, pixels: np.ndarray) -> np.ndarray:
        """(N, H, W, 3) uint8 -> (N, dim) unit vectors."""
        torch = self._torch
        pixels = np.asarray(pixels)
        if pixels.ndim == 3:
            pixels = pixels[None, ...]
        if not len(pixels):
            return np.zeros((0, self.dim), dtype=np.float32)
        out = []
        with torch.no_grad():
            for i in range(0, len(pixels), IMAGE_BATCH):
                chunk = pixels[i:i + IMAGE_BATCH].astype(np.float32)
                chunk = chunk / 127.5 - 1.0             # SigLIP's own recipe
                t = torch.from_numpy(chunk).permute(0, 3, 1, 2).to(self.device)
                out.append(self._vectors(
                    self._model.get_image_features(pixel_values=t)))
        return unit(np.concatenate(out, axis=0))

    def _encode_texts_raw(self, texts: list) -> np.ndarray:
        torch = self._torch
        out = []
        with torch.no_grad():
            for i in range(0, len(texts), TEXT_BATCH):
                batch = self._tok(texts[i:i + TEXT_BATCH],
                                  padding="max_length", max_length=TEXT_TOKENS,
                                  truncation=True, return_tensors="pt")
                batch = {k: v.to(self.device) for k, v in batch.items()}
                out.append(self._vectors(self._model.get_text_features(**batch)))
        return np.concatenate(out, axis=0)

    def encode_texts(self, texts: list) -> np.ndarray:
        texts = [str(t or "") for t in texts]
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return unit(self._encode_texts_raw(texts))


class Deterministic(Backend):
    """A stand-in with no weights, for tests and for proving the plumbing.

    Words are the only signal: a description and a frame that were built from
    the same words agree, and unrelated ones do not. That is enough to test
    every decision made around the model — which frame wins, when a match is
    rejected, how a run is rescued — without downloading anything.
    """

    def __init__(self, dim: int = 32):
        super().__init__(name="deterministic", dim=dim)
        self._cache: dict = {}

    def _word_vec(self, word: str) -> np.ndarray:
        # crc32, not hash(): Python randomises string hashing per process, so
        # a "deterministic" backend built on hash() would return different
        # vectors on every run and a stored index would stop matching itself.
        #
        # A dense random direction per word, not a few raised slots. The
        # sparse version put every word on three of sixty-four positions,
        # which meant two unrelated words overlapped often enough to score
        # halfway to a match — an unrelated caption reached a lift of 2.4
        # against a thousand frames, higher than the 1.2 the real model needs
        # to call something found. Tests calibrated against that were
        # calibrating against noise. Random directions in the full dimension
        # behave like the real thing: identical words score 1.0, unrelated
        # ones score about zero, and the gap between them is wide enough that
        # a threshold means something.
        v = self._cache.get(word)
        if v is None:
            rng = np.random.default_rng(zlib.crc32(word.encode("utf-8")))
            v = rng.standard_normal(self.dim).astype(np.float32)
            self._cache[word] = v
        return v

    def _hash_vec(self, tokens) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        for t in tokens:
            v += self._word_vec(str(t))
        return v

    def encode_texts(self, texts: list) -> np.ndarray:
        rows = [self._hash_vec(str(t or "").lower().split()) for t in texts]
        return unit(np.array(rows, dtype=np.float32)) if rows else \
            np.zeros((0, self.dim), dtype=np.float32)

    def encode_images(self, pixels: np.ndarray) -> np.ndarray:
        """Read the words back out of the picture.

        A test frame is painted by writing byte values into the top row, so a
        fake frame can carry the description it is supposed to depict. Real
        footage produces something arbitrary but stable, which is exactly what
        a test of "does this frame match?" needs on the negative side.
        """
        pixels = np.asarray(pixels)
        if pixels.ndim == 3:
            pixels = pixels[None, ...]
        if not len(pixels):
            return np.zeros((0, self.dim), dtype=np.float32)
        rows = []
        for frame in pixels:
            raw = bytes(int(b) for b in frame[0, :, 0]).rstrip(b"\x00")
            words = raw.decode("utf-8", "ignore").lower().split()
            if words:
                rows.append(self._hash_vec(words))
            else:
                # no painted caption: fall back on the pixels themselves
                block = np.asarray(frame, dtype=np.float32)
                acc = np.zeros(self.dim, dtype=np.float32)
                flat = block.reshape(-1)
                for i in range(self.dim):
                    acc[i] = float(flat[i::self.dim].mean()) if len(flat) > i else 0.0
                rows.append(acc)
        return unit(np.array(rows, dtype=np.float32))


# ---------------------------------------------------------------------------
# one shared instance
# ---------------------------------------------------------------------------

_BACKEND: Backend | None = None
_FAILED = ""


def set_backend(backend: Backend | None) -> None:
    """Install a backend by hand. The tests' way in; also lets a caller
    hold one model open across many videos instead of reloading it."""
    global _BACKEND, _FAILED
    _BACKEND = backend
    _FAILED = ""


def load(model_name: str = DEFAULT_MODEL, log=lambda *a: None) -> Backend:
    """The shared backend, loaded once. Raises EmbedError if it cannot be."""
    global _BACKEND, _FAILED
    if _BACKEND is not None:
        return _BACKEND
    if _FAILED:
        raise EmbedError(_FAILED)
    log(f"    loading the picture model ({model_name})…")
    log(f"    first run downloads it to {models_dir()} — once, then offline")
    try:
        _BACKEND = SigLIP(model_name)
    except EmbedError as exc:
        # Remembered so a queue of twenty-five videos does not retry a
        # two-gigabyte download twenty-five times.
        _FAILED = str(exc)
        raise
    log(f"    picture model ready on {_BACKEND.device}, {_BACKEND.dim} dimensions")
    return _BACKEND


def loaded() -> Backend | None:
    return _BACKEND
