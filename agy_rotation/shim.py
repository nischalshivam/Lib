"""Auto-rotating OpenAI-compatible vision endpoint backed by Antigravity (agy).

The movie tool's cataloguer/verifier POSTs OpenAI multimodal chat-completions
to `gemini_base`. This shim answers them with `agy` (Gemini, on the Antigravity
subscription — free, no API key), and ROTATES across many Google accounts so a
full day of indexing never stalls on one account's 5-hour / weekly limit.

How rotation works (and why it's "sticky", not per-call):
  agy keeps exactly ONE logged-in account, in a single Windows Credential
  Manager slot (gemini:antigravity). Two accounts can't be active at once, so
  true parallel-across-accounts on one machine is impossible. Instead we run
  MANY concurrent calls on the CURRENT account (that's the speed), and only when
  that account starts returning quota/limit errors do we switch the slot to the
  next healthy account (that's the endless volume). The exhausted account is put
  on a cooldown (default 5h) and skipped until it resets. A switch drains all
  in-flight calls first, so no call ever runs against a half-written credential.

Accounts come from snapshots made with agy-accounts.ps1 (Credential Manager
blobs saved as <email>.cred). Add accounts once; this serves them all day.

Env:
  SHIM_PORT            (default 8788)
  SHIM_MODEL           agy model id (default gemini-3.6-flash-low)
  SHIM_TIMEOUT_S       per-call hard cap (default 300)
  SHIM_COOLDOWN_S      how long an exhausted account rests (default 18000 = 5h)
  AGY_ACCOUNTS_DIR     snapshot store (default ~/.model-router/agy-accounts)
  SHIM_MAX_WORKERS     server thread cap (default 16)
  SHIM_FORCE_QUOTA_AFTER  TEST ONLY: force a fake quota fail after N ok calls
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("SHIM_PORT", "8788"))
MODEL = os.environ.get("SHIM_MODEL", "gemini-3.6-flash-low")
TIMEOUT_S = int(os.environ.get("SHIM_TIMEOUT_S", "300"))
COOLDOWN_S = int(os.environ.get("SHIM_COOLDOWN_S", str(5 * 3600)))
ACCOUNTS_DIR = os.environ.get("AGY_ACCOUNTS_DIR") or os.path.join(
    os.path.expanduser("~"), ".model-router", "agy-accounts")
STATE_FILE = os.path.join(ACCOUNTS_DIR, "rotation_state.json")
MANAGER = os.path.join(HERE, "agy-accounts.ps1")
AGY = shutil.which("agy") or os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "agy", "bin", "agy.exe")
FORCE_QUOTA_AFTER = int(os.environ.get("SHIM_FORCE_QUOTA_AFTER", "0") or "0")

EXPECTED_KEYS = {"description", "characters", "match", "frame", "tags", "action"}
QUOTA_SIGNS = ("resource_exhausted", "429", "quota", "exceeded", "rate limit",
               "rate-limit", "ratelimit", "verify your account", "403",
               "permission_denied", "too many requests")
TRANSIENT_SIGNS = ("timeout", "timed out", "500", "502", "503", "unavailable",
                   "deadline", "network", "connection", "unreachable", "dial",
                   "socket", "eof", "temporarily", "try again", "reset by peer",
                   "no such host", "getting token source", "load code assist",
                   "context canceled", "i/o", "tls")

_ok_count = 0
_ok_lock = threading.Lock()


def _log(*a):
    print("[rot-shim]", *a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# account pool + sticky rotation
# --------------------------------------------------------------------------- #
class Pool:
    def __init__(self):
        self.lock = threading.Lock()
        self.cond = threading.Condition()      # guards switching/inflight
        self.inflight = 0
        self.switching = False
        self.emails: list[str] = []
        self.active: str | None = None
        self.cooldown: dict[str, float] = {}   # email -> epoch usable-again
        self.reload()
        self._load_state()
        if self.active not in self.emails:
            self.active = self._first_ready() or (self.emails[0] if self.emails else None)
        if self.active:
            self._cred_switch(self.active)

    def reload(self):
        try:
            self.emails = sorted(
                f[:-5] for f in os.listdir(ACCOUNTS_DIR) if f.endswith(".cred"))
        except OSError:
            self.emails = []

    def _auto_reload(self):
        """Re-scan the account folder every 60s so accounts added while indexing
        is running join the rotation live (cooldowns for existing ones are kept —
        the cooldown dict is keyed by email)."""
        while True:
            time.sleep(60)
            before = set(self.emails)
            with self.lock:
                self.reload()
            added = set(self.emails) - before
            if added:
                _log(f"pool grew: +{sorted(added)} (now {len(self.emails)})")

    def _load_state(self):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                s = json.load(f)
            self.active = s.get("active") or self.active
            self.cooldown = {k: float(v) for k, v in (s.get("cooldown") or {}).items()}
        except (OSError, ValueError):
            pass

    def _save_state(self):
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"active": self.active, "cooldown": self.cooldown,
                           "saved": time.time()}, f, indent=2)
        except OSError:
            pass

    def _ready(self, email: str) -> bool:
        return self.cooldown.get(email, 0) <= time.time()

    def _first_ready(self) -> str | None:
        for e in self.emails:
            if self._ready(e):
                return e
        return None

    def _next_ready(self, after: str) -> str | None:
        if not self.emails:
            return None
        try:
            i0 = self.emails.index(after)
        except ValueError:
            i0 = -1
        n = len(self.emails)
        for k in range(1, n + 1):
            e = self.emails[(i0 + k) % n]
            if self._ready(e):
                return e
        return None

    # ---- credential slot switch, with in-flight drain --------------------- #
    def _cred_switch(self, email: str) -> bool:
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", MANAGER, "switch", email],
                capture_output=True, text=True, timeout=60,
                env={**os.environ, "AGY_ACCOUNTS_DIR": ACCOUNTS_DIR})
            if r.returncode != 0:
                _log(f"switch {email} FAILED: {(r.stderr or r.stdout)[:160]}")
                return False
            return True
        except Exception as exc:                              # noqa: BLE001
            _log(f"switch {email} error: {exc}")
            return False

    def enter(self):
        with self.cond:
            while self.switching:
                self.cond.wait()
            self.inflight += 1
            return self.active

    def leave(self):
        with self.cond:
            self.inflight -= 1
            self.cond.notify_all()

    def rotate(self, failed_email: str) -> str | None:
        """A call on `failed_email` hit a quota wall. Cool it, switch the slot to
        the next healthy account, return it (or None if every account is
        cooling). If another thread already rotated away from failed_email, just
        return the current active — only the first failer actually switches."""
        with self.lock:
            if self.active != failed_email:
                return self.active                 # someone already rotated
            self.cooldown[failed_email] = time.time() + COOLDOWN_S
            nxt = self._next_ready(failed_email)
            self._save_state()
            if nxt is None:
                _log(f"ALL accounts cooling; {failed_email} exhausted")
                return None
            _log(f"rotate: {failed_email} -> {nxt} "
                 f"({sum(1 for e in self.emails if self._ready(e))}/{len(self.emails)} ready)")
        # drain in-flight, then flip the credential slot (outside self.lock)
        with self.cond:
            self.switching = True
            while self.inflight > 0:
                self.cond.wait()
            ok = self._cred_switch(nxt)
            if ok:
                with self.lock:
                    self.active = nxt
                    self._save_state()
            self.switching = False
            self.cond.notify_all()
        return nxt if ok else None

    def rotate_soft(self, failed_email: str) -> str | None:
        """Move OFF failed_email WITHOUT cooling it — a network/transient failure
        is not the account's fault. Switch to the next ready account so a global
        outage can be detected by cycling; returns the new active (or the same if
        it's the only ready one). Never sets a cooldown."""
        with self.lock:
            if self.active != failed_email:
                return self.active
            nxt = self._next_ready(failed_email)
            if nxt is None or nxt == failed_email:
                return failed_email
        with self.cond:
            self.switching = True
            while self.inflight > 0:
                self.cond.wait()
            ok = self._cred_switch(nxt)
            if ok:
                with self.lock:
                    self.active = nxt
                    self._save_state()
            self.switching = False
            self.cond.notify_all()
        return nxt if ok else failed_email

    def status(self) -> dict:
        now = time.time()
        return {"active": self.active, "model": MODEL,
                "accounts": [{"email": e, "ready": self._ready(e),
                              "cooldown_left_s": max(0, int(self.cooldown.get(e, 0) - now))}
                             for e in self.emails]}


POOL = Pool()


# --------------------------------------------------------------------------- #
# request -> agy translation (images to files, flatten prompt)
# --------------------------------------------------------------------------- #
def _ext_for(data: bytes) -> str:
    if data[:8].startswith(b"\x89PNG"):
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def _decode_data_uri(uri: str) -> bytes | None:
    m = re.match(r"data:[^;,]*;base64,(.*)$", uri, re.DOTALL)
    if not m:
        return None
    try:
        return base64.b64decode(m.group(1))
    except Exception:
        return None


def _flatten(messages: list, tmp: str) -> str:
    saved = 0
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, str):
            if content.strip():
                parts.append(("[SYSTEM]\n" if role == "system" else "") + content)
            continue
        buf: list[str] = []
        for p in content:
            t = p.get("type")
            if t == "text":
                buf.append(p.get("text", ""))
            elif t == "image_url":
                data = _decode_data_uri((p.get("image_url") or {}).get("url", ""))
                if data is None:
                    buf.append("[image: <unreadable>]")
                    continue
                saved += 1
                fn = f"img{saved:03d}{_ext_for(data)}"
                with open(os.path.join(tmp, fn), "wb") as f:
                    f.write(data)
                buf.append(f"[IMAGE FILE: {fn} — open and view this image]")
            elif t == "video_path":
                # A local video file to analyse (used by the video-pass
                # cataloguer). Copy it into agy's workspace and reference it.
                src = p.get("path") or ""
                if src and os.path.isfile(src):
                    saved += 1
                    fn = f"clip{saved:03d}" + os.path.splitext(src)[1]
                    try:
                        shutil.copy2(src, os.path.join(tmp, fn))
                        buf.append(f"[VIDEO FILE: {fn} — watch this video]")
                    except OSError:
                        buf.append("[video: <copy failed>]")
                else:
                    buf.append("[video: <missing>]")
        if buf:
            parts.append(("[SYSTEM]\n" if role == "system" else "") + "\n".join(buf))
    header = (
        "You are answering a machine request. IMAGE and/or VIDEO files are in "
        "your current working directory, referenced below as '[IMAGE FILE: ...]' "
        "or '[VIDEO FILE: ...]' at the point they belong. You MUST open and "
        "visually inspect / watch every one of them, in order — never guess. When "
        "a block says 'Reference — <name>:' the image after it is a reference "
        "photo of that person. After viewing all media, obey the request and "
        "reply with ONLY the exact strict JSON asked for — no prose, no code "
        "fences.\n\n" + "=" * 60 + "\n")
    return header + "\n\n".join(parts)


def _has_expected(obj) -> bool:
    """A parsed answer is 'real' if it's an object with an expected key, or a
    non-empty list of such objects (the video-pass returns a JSON ARRAY of shots
    — frame/verify passes return a single object). Both are valid answers."""
    if isinstance(obj, dict):
        return bool(EXPECTED_KEYS & set(obj.keys()))
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        return bool(EXPECTED_KEYS & set(obj[0].keys()))
    return False


def _extract_json(resp: str):
    """Parse the outermost JSON value in `resp` — object OR array — decided by
    whichever bracket opens first (so a `{...}` with an inner `[tags]` isn't
    mistaken for an array, and a `[{...}]` shot list parses whole)."""
    s = resp.strip()
    if s.startswith("```"):
        s = s.strip("`")
        s = s[s.find("\n") + 1:] if "\n" in s else s
    oi, ai = s.find("{"), s.find("[")
    if oi < 0 and ai < 0:
        return None
    if ai < 0 or (oi >= 0 and oi < ai):
        a, b = oi, s.rfind("}")
    else:
        a, b = ai, s.rfind("]")
    if a < 0 or b <= a:
        return None
    try:
        return json.loads(s[a:b + 1])
    except ValueError:
        return None


def _classify(env: dict, out: str) -> str:
    """'ok' | 'quota' | 'transient' | 'error' from an agy result envelope."""
    resp = env.get("response") or ""
    # a real answer: response holds our JSON (object or shot-array) with an
    # expected key (status ERROR but answer present is still good — agy
    # over-stamps ERROR).
    if _has_expected(_extract_json(resp)):
        return "ok"
    blob = (resp + " " + out).lower()
    if any(s in blob for s in QUOTA_SIGNS):
        return "quota"
    if any(s in blob for s in TRANSIENT_SIGNS):
        return "transient"
    return "error"


def _run_agy(prompt: str, tmp: str) -> tuple[str | None, str, str]:
    """(response_or_None, detail, kind)."""
    cmd = [AGY, "--add-dir", tmp, "--model", MODEL,
           "--dangerously-skip-permissions", "--output-format", "json",
           "--print-timeout", f"{max(60, TIMEOUT_S - 30)}s"]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=TIMEOUT_S, cwd=tmp)
    except subprocess.TimeoutExpired:
        return None, "agy timeout", "transient"
    except Exception as exc:                                  # noqa: BLE001
        return None, f"agy spawn failed: {exc}", "error"
    out = proc.stdout or ""
    a, b = out.find("{"), out.rfind("}")
    if a < 0 or b <= a:
        return None, f"agy no-json: {(proc.stderr or out)[:160]}", "transient"
    try:
        env = json.loads(out[a:b + 1])
    except ValueError:
        return None, f"agy bad-json: {out[a:b+1][:160]}", "transient"
    kind = _classify(env, out)
    tok = (env.get("usage") or {}).get("total_tokens")
    if kind == "ok":
        return env.get("response", ""), f"tok={tok}", "ok"
    return None, f"status={env.get('status')} {str(env.get('response'))[:120]}", kind


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        p = self.path.rstrip("/")
        if p.endswith("/models"):
            self._send(200, {"data": [{"id": MODEL, "object": "model"}]})
        elif p.endswith("/status"):
            self._send(200, POOL.status())
        else:
            self._send(200, {"ok": True, "model": MODEL, "active": POOL.active})

    def _maybe_force_quota(self) -> bool:
        global _ok_count
        if not FORCE_QUOTA_AFTER:
            return False
        with _ok_lock:
            _ok_count += 1
            return _ok_count % (FORCE_QUOTA_AFTER + 1) == 0

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send(404, {"error": {"message": "not found"}})
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._send(400, {"error": {"message": "bad json"}})
            return
        messages = req.get("messages") or []
        tmp = tempfile.mkdtemp(prefix="rot_")
        t0 = time.time()
        try:
            prompt = _flatten(messages, tmp)
            text = self._serve(prompt, tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if text is None:
            self._send(503, {"error": {"message": "all accounts cooling or "
                                       "unrecoverable; try later"}})
            return
        self._send(200, {
            "id": "rot", "object": "chat.completion",
            "model": req.get("model", MODEL),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"total_tokens": 0},
        })
        _log(f"ok {time.time()-t0:.0f}s acct={POOL.active}")

    def _serve(self, prompt: str, tmp: str) -> str | None:
        """Run on the active account. QUOTA -> cool + rotate. TRANSIENT (network
        blip, timeout) -> patient retry on the SAME account, then hop to another
        WITHOUT cooling anyone; if several accounts all transient-fail it's a
        global outage, so give up this one request (the caller retries) while
        leaving EVERY account ready — so when the internet returns it resumes
        instantly, never stuck behind a wrongly-applied 5-hour cooldown."""
        n_accounts = max(1, len(POOL.emails))
        soft_hops = 0
        while True:
            transient = 0
            while transient < 3:
                email = POOL.enter()
                if email is None:
                    POOL.leave()
                    return None
                try:
                    if self._maybe_force_quota():
                        text, detail, kind = None, "FORCED test quota", "quota"
                    else:
                        text, detail, kind = _run_agy(prompt, tmp)
                finally:
                    POOL.leave()
                if kind == "ok":
                    return text
                if kind == "quota":
                    if POOL.rotate(email) is None:
                        return None
                    soft_hops = 0            # a real rotation clears outage count
                    break                    # retry on the fresh account
                if kind == "transient":
                    transient += 1
                    time.sleep(min(20, 5 * transient))   # 5,10,15s: absorb blips
                    continue                 # retry the SAME account
                _log(f"error acct={email}: {detail[:120]}")
                return None                  # genuine hard error
            else:
                # 3 transients, no quota -> likely the network. Move on WITHOUT
                # cooling this account. After hopping a few accounts all failing,
                # call it a global outage and bail (caller retries; none cooled).
                soft_hops += 1
                if soft_hops >= min(3, n_accounts):
                    _log("network outage suspected — no account cooled; caller retries")
                    return None
                POOL.rotate_soft(email)


def main():
    if not os.path.isfile(AGY):
        _log("agy not found:", AGY); sys.exit(2)
    if not POOL.emails:
        _log(f"no accounts in {ACCOUNTS_DIR} — add with agy-accounts.ps1 snapshot");
    threading.Thread(target=POOL._auto_reload, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    _log(f"listening http://127.0.0.1:{PORT}/v1  model={MODEL}  "
         f"accounts={len(POOL.emails)} active={POOL.active} cooldown={COOLDOWN_S}s")
    srv.serve_forever()


if __name__ == "__main__":
    main()
