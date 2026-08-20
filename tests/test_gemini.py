"""The vision verifier's offline half: config, prompt, parse — no network.

The one thing local retrieval cannot do is look at a frame, so the model
call itself cannot be unit-tested here and is deliberately isolated. What
CAN be tested is everything around it: that a secret only ever comes from
settings or environment and never from code, that the prompt numbers frames
so the answer maps back to a real timestamp, and that a mangled or abstaining
verdict is read as "no opinion" rather than moving a shot on nonsense.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from media_index import gemini                             # noqa: E402


def frames(*times):
    return [gemini.Frame(at_s=t, jpeg=b"\xff\xd8jpeg") for t in times]


class TestConfigNeverComesFromCode(unittest.TestCase):

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("GEMINI_API_KEY", "GEMINI_BASE_URL", "GEMINI_MODEL")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_environment_supplies_the_key(self):
        os.environ["GEMINI_API_KEY"] = "sk-test"
        os.environ["GEMINI_BASE_URL"] = "https://x/v1"
        cfg = gemini.config()
        self.assertEqual(cfg.key, "sk-test")
        self.assertTrue(cfg.ok)
        self.assertEqual(cfg.endpoint, "https://x/v1/chat/completions")

    def test_settings_file_beats_a_stale_environment_key(self):
        """The whole bug: a stale short-lived env token must not override a
        good key the user put in settings.txt. The file wins."""
        os.environ["GEMINI_API_KEY"] = "AQ.stale-google-token"
        orig = gemini._from_settings
        gemini._from_settings = lambda: {"gemini_key": "sk-from-file",
                                         "gemini_base": "https://f/v1"}
        try:
            cfg = gemini.config()
            self.assertEqual(cfg.key, "sk-from-file")
            self.assertEqual(gemini.key_source(), "settings.txt")
        finally:
            gemini._from_settings = orig

    def test_environment_is_the_fallback_when_the_file_is_silent(self):
        os.environ["GEMINI_API_KEY"] = "sk-env-fallback"
        orig = gemini._from_settings
        gemini._from_settings = lambda: {}
        try:
            self.assertEqual(gemini.config().key, "sk-env-fallback")
            self.assertEqual(gemini.key_source(), "environment")
        finally:
            gemini._from_settings = orig

    def test_no_key_is_not_ok_and_says_why(self):
        ok, why = gemini.available()
        self.assertFalse(ok)
        self.assertIn("gemini_key", why)

    def test_the_default_model_is_flash(self):
        self.assertEqual(gemini.Config(key="k", base="b").model,
                         gemini.DEFAULT_MODEL)

    def test_no_key_literal_is_committed_in_the_source(self):
        """A pasted key must never end up in the repository."""
        with open(gemini.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("sk-", src)


class TestTheQuestion(unittest.TestCase):

    def test_frames_are_numbered_so_the_answer_maps_back(self):
        msgs = gemini.build_messages("a bell is struck", ["Hector"],
                                     frames(10.0, 20.0, 30.0))
        user = msgs[1]["content"]
        texts = [c["text"] for c in user if c["type"] == "text"]
        self.assertTrue(any("Frame 1:" in t for t in texts))
        self.assertTrue(any("Frame 3:" in t for t in texts))
        self.assertTrue(any("Hector" in t for t in texts))
        images = [c for c in user if c["type"] == "image_url"]
        self.assertEqual(len(images), 3)
        self.assertTrue(images[0]["image_url"]["url"].startswith(
            "data:image/jpeg;base64,"))

    def test_the_system_rule_allows_an_abstention(self):
        msgs = gemini.build_messages("x", [], frames(1.0))
        self.assertIn("-1", msgs[0]["content"])


class TestTheAnswer(unittest.TestCase):

    def test_a_clean_verdict_maps_to_the_frames_timestamp(self):
        fr = frames(10.0, 20.0, 30.0)
        ch = gemini.parse_verdict(
            '{"frame": 2, "confidence": 0.9, "reason": "bell visible"}', fr)
        self.assertTrue(ch.chose)
        self.assertEqual(ch.at_s, 20.0)
        self.assertEqual(ch.index, 1)

    def test_a_fenced_verdict_is_still_read(self):
        fr = frames(5.0, 6.0)
        ch = gemini.parse_verdict(
            'Here you go:\n```json\n{"frame": 1, "confidence": 0.8}\n```', fr)
        self.assertTrue(ch.chose)
        self.assertEqual(ch.at_s, 5.0)

    def test_frame_minus_one_is_an_honest_abstention(self):
        ch = gemini.parse_verdict('{"frame": -1, "confidence": 0.0}',
                                  frames(1.0, 2.0))
        self.assertFalse(ch.chose)
        self.assertEqual(ch.index, -1)

    def test_low_confidence_does_not_move_a_shot(self):
        ch = gemini.parse_verdict('{"frame": 1, "confidence": 0.3}',
                                  frames(1.0, 2.0))
        self.assertFalse(ch.chose)

    def test_a_frame_number_out_of_range_is_refused(self):
        ch = gemini.parse_verdict('{"frame": 9, "confidence": 0.9}',
                                  frames(1.0, 2.0))
        self.assertFalse(ch.chose)
        self.assertEqual(ch.index, -1)

    def test_garbage_is_no_opinion_not_a_crash(self):
        for bad in ("", "not json", "{", '{"frame":', "null"):
            ch = gemini.parse_verdict(bad, frames(1.0))
            self.assertFalse(ch.chose)

    def test_verify_without_config_returns_no_choice(self):
        saved = {k: os.environ.pop(k, None) for k in
                 ("GEMINI_API_KEY", "GEMINI_BASE_URL")}
        try:
            ch = gemini.verify("x", frames(1.0, 2.0),
                               cfg=gemini.Config())    # empty config
            self.assertFalse(ch.chose)
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


class TestErrorsAreSurfacedNotSwallowed(unittest.TestCase):
    """The bug that left a user stuck with a working key: a failure that said
    only "koi jawab nahi aaya" and hid the actual HTTP status."""

    def setUp(self):
        import urllib.request
        self._real = urllib.request.urlopen
        self.cfg = gemini.Config(key="k", base="https://x/v1")

    def tearDown(self):
        import urllib.request
        urllib.request.urlopen = self._real

    def _fake_urlopen(self, payload=None, code=200, http_error=None):
        import io
        import urllib.error
        import urllib.request

        def fake(req, timeout=None):
            if http_error is not None:
                raise urllib.error.HTTPError(
                    "u", http_error, "bad", {}, io.BytesIO(b'{"error":"nope"}'))

            class R:
                def __enter__(self_): return self_
                def __exit__(self_, *a): return False
                def read(self_): return payload.encode("utf-8")
                def getcode(self_): return code
            return R()
        urllib.request.urlopen = fake

    def test_a_clean_answer_comes_through(self):
        self._fake_urlopen(
            '{"choices":[{"message":{"content":"OK"}}]}')
        text, detail = gemini.call(self.cfg, [])
        self.assertEqual(text, "OK")

    def test_an_http_error_names_the_status(self):
        self._fake_urlopen(http_error=401)
        text, detail = gemini.call(self.cfg, [])
        self.assertIsNone(text)
        self.assertIn("401", detail)

    def test_an_error_object_in_a_200_is_still_an_error(self):
        self._fake_urlopen('{"error":{"message":"quota over"}}')
        text, detail = gemini.call(self.cfg, [])
        self.assertIsNone(text)
        self.assertIn("quota over", detail)

    def test_html_instead_of_json_is_reported(self):
        self._fake_urlopen("<html>docs</html>")
        text, detail = gemini.call(self.cfg, [])
        self.assertIsNone(text)
        self.assertIn("JSON nahi", detail)

    def test_ping_reports_the_answer_on_success(self):
        self._fake_urlopen('{"choices":[{"message":{"content":"OK"}}]}')
        ok, detail = gemini.ping(self.cfg)
        self.assertTrue(ok)
        self.assertEqual(detail, "OK")


if __name__ == "__main__":
    unittest.main()
