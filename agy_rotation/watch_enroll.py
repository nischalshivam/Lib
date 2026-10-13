"""Auto-enroll: watch the agy login slot and save every NEW Google account
automatically. You just sign accounts into `agy` one by one (/logout, sign in
the next one); this notices each new login and saves it to the rotation pool —
no commands, no Enter, no typing emails.

Reads the Windows Credential Manager slot `gemini:antigravity` directly (the one
place agy keeps its single logged-in account) and writes the raw blob as
acct-NN.cred — exactly what agy-accounts.ps1 does, just automatically.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import os
import re
import time

TARGET = "gemini:antigravity"
STORE = os.environ.get("AGY_ACCOUNTS_DIR") or os.path.join(
    os.path.expanduser("~"), ".model-router", "agy-accounts")
os.makedirs(STORE, exist_ok=True)

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)


class CREDENTIAL(ctypes.Structure):
    _fields_ = [("Flags", w.DWORD), ("Type", w.DWORD), ("TargetName", w.LPWSTR),
                ("Comment", w.LPWSTR), ("LastWritten", w.FILETIME),
                ("CredentialBlobSize", w.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
                ("Persist", w.DWORD), ("AttributeCount", w.DWORD),
                ("Attributes", ctypes.c_void_p), ("TargetAlias", w.LPWSTR),
                ("UserName", w.LPWSTR)]


_CredRead = advapi32.CredReadW
_CredRead.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD,
                      ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
_CredRead.restype = w.BOOL
_CredFree = advapi32.CredFree
_CredFree.argtypes = [ctypes.c_void_p]


def read_login() -> bytes | None:
    """The blob of the currently logged-in agy account, or None if none."""
    ptr = ctypes.POINTER(CREDENTIAL)()
    if not _CredRead(TARGET, 1, 0, ctypes.byref(ptr)):
        return None
    try:
        c = ptr.contents
        return ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize)
    finally:
        _CredFree(ptr)


def saved_blobs() -> dict[str, bytes]:
    out = {}
    for f in os.listdir(STORE):
        if f.endswith(".cred"):
            try:
                out[f[:-5]] = open(os.path.join(STORE, f), "rb").read()
            except OSError:
                pass
    return out


def email_for_current() -> str | None:
    """The Google email agy is currently logged in as, read from agy's own CLI
    log (the credential blob itself is encrypted). Lets new accounts be saved
    under their real email instead of acct-NN."""
    import glob
    logdir = os.path.join(os.path.expanduser("~"), ".gemini",
                          "antigravity-cli", "log")
    try:
        logs = sorted(glob.glob(os.path.join(logdir, "*.log")),
                      key=os.path.getmtime, reverse=True)
    except OSError:
        return None
    for lg in logs[:3]:
        try:
            txt = open(lg, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        m = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", txt)
        if m:
            return m[-1].lower()
    return None


def next_label(existing: dict) -> str:
    email = email_for_current()
    if email and (email + "") not in existing:
        return email
    nums = [int(m.group(1)) for name in existing
            if (m := re.match(r"acct-(\d+)$", name))]
    return f"acct-{(max(nums) + 1) if nums else 1:02d}"


def main():
    print("=" * 60)
    print("  AUTO ACCOUNT SAVER — just log accounts into agy")
    print("=" * 60)
    print("Keep this window open. In your agy window:")
    print("   type  /logout , then sign in the next Google account.")
    print("Each new sign-in is saved here automatically. Repeat for all")
    print("your accounts. Close this window when you're done.\n")

    known = saved_blobs()
    print(f"[start] {len(known)} account(s) already saved: "
          f"{', '.join(sorted(known)) or '(none)'}\n")
    if read_login() is None:
        print("[waiting] agy isn't logged in yet — sign in an account in agy...")

    # A real login settles and STAYS put; a switch/transient blob changes again
    # within a second or two. So only save a new blob after it has been stable
    # for DEBOUNCE_S — that filters out mid-switch flickers and never saves a
    # credential that isn't a genuine, finished sign-in.
    DEBOUNCE_S = 8
    pending = None
    pending_since = 0.0
    while True:
        try:
            blob = read_login()
            if blob:
                known = saved_blobs()
                if any(blob == b for b in known.values()):
                    pending = None                    # already saved — ignore
                elif blob != pending:
                    pending = blob                    # new candidate: start timer
                    pending_since = time.time()
                elif time.time() - pending_since >= DEBOUNCE_S:
                    email = email_for_current()
                    if email and email in known:
                        print(f"  [already have] {email} — /logout and sign in a "
                              f"NEW account.")
                        pending = None
                        time.sleep(6)          # don't re-alert on the same token
                        continue
                    label = next_label(known)
                    with open(os.path.join(STORE, f"{label}.cred"), "wb") as f:
                        f.write(blob)
                    print(f"  ✓ SAVED {label}   (pool now has {len(known)+1} accounts)")
                    pending = None
            time.sleep(2)
        except KeyboardInterrupt:
            break
        except Exception as exc:                              # noqa: BLE001
            print(f"[skip] {type(exc).__name__}: {exc}")
            time.sleep(2)

    print("\nDone. Start indexing anytime with start.bat")


if __name__ == "__main__":
    main()
