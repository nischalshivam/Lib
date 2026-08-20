"""Console symbols that survive a Windows command prompt.

cmd.exe still runs on a legacy code page by default, where printing "✅" is not
a cosmetic problem — it raises UnicodeEncodeError and kills the command. The
first thing a new user sees must not be a traceback, so every symbol is
declared here once and falls back to ASCII when the console cannot encode it.
"""
from __future__ import annotations

import sys

# name -> (preferred, ascii fallback)
_SYMBOLS = {
    "ok": ("✅", "[OK]"),
    "warn": ("⚠️ ", "[!] "),
    "fail": ("❌", "[X]"),
    "skip": ("⏭ ", "[-] "),
    "blank": ("◻️ ", "[ ] "),
    "pending": ("…", "..."),
    "yes": ("✓", "y"),
    "maybe": ("~", "~"),
    "no": ("?", "?"),
    "arrow": ("→", "->"),
    "dot": ("·", "."),
    "dash": ("—", "-"),
}


def _console_ok() -> bool:
    """Can this console actually encode the preferred symbols?"""
    enc = getattr(sys.stdout, "encoding", None) or ""
    if not enc:
        return False
    try:
        "".join(v for v, _ in _SYMBOLS.values()).encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def enable_utf8() -> None:
    """Ask the stream for UTF-8 first; fall back to replacing bad characters.

    Called once at CLI start. On a modern Windows terminal this succeeds and
    the pretty symbols are used; on an old code page it at least guarantees
    that nothing raises.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass
    _refresh()


_UNICODE = True


def _refresh() -> None:
    global _UNICODE
    _UNICODE = _console_ok()


def sym(name: str) -> str:
    pref, plain = _SYMBOLS.get(name, ("", ""))
    return pref if _UNICODE else plain


def icons(*names) -> dict:
    """{name: symbol} — handy for building the small status maps."""
    return {n: sym(n) for n in names}


_refresh()
