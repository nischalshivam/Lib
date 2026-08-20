"""The console symbols must never be the reason a command fails.

Windows cmd.exe still defaults to a legacy code page where printing "✅"
raises UnicodeEncodeError. A traceback as the very first thing a new user sees
is the worst possible outcome, so the symbols degrade to ASCII instead.
"""
from __future__ import annotations

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from media_index import term                                     # noqa: E402


class TestSymbols(unittest.TestCase):
    def tearDown(self):
        term._refresh()

    def _with_encoding(self, enc):
        """Pretend stdout is a console using `enc`."""
        real = sys.stdout
        sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding=enc,
                                      errors="strict")
        try:
            term._refresh()
            return {name: term.sym(name) for name in term._SYMBOLS}
        finally:
            sys.stdout = real

    def test_utf8_console_gets_the_real_symbols(self):
        got = self._with_encoding("utf-8")
        self.assertEqual(got["ok"], "✅")
        self.assertEqual(got["fail"], "❌")

    def test_legacy_codepage_falls_back_to_ascii(self):
        got = self._with_encoding("cp437")
        self.assertEqual(got["ok"], "[OK]")
        self.assertEqual(got["fail"], "[X]")
        self.assertEqual(got["arrow"], "->")

    def test_every_fallback_is_encodable_on_a_legacy_codepage(self):
        """The whole point: nothing in the ASCII set can raise."""
        for _, plain in term._SYMBOLS.values():
            plain.encode("cp437")       # would raise if it could not

    def test_every_symbol_has_both_forms(self):
        for name, (pref, plain) in term._SYMBOLS.items():
            self.assertTrue(pref, name)
            self.assertTrue(plain, name)

    def test_unknown_name_is_empty_not_an_error(self):
        self.assertEqual(term.sym("not_a_symbol"), "")

    def test_enable_utf8_survives_a_stream_that_refuses(self):
        real = sys.stdout

        class Stubborn(io.StringIO):
            encoding = "cp437"

            def reconfigure(self, **kw):
                raise OSError("not reconfigurable")

        sys.stdout = Stubborn()
        try:
            term.enable_utf8()          # must not raise
            self.assertEqual(term.sym("ok"), "[OK]")
        finally:
            sys.stdout = real


if __name__ == "__main__":
    unittest.main(verbosity=2)
