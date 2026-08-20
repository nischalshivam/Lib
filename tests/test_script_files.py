"""Reading the file a chat model actually returns.

Every shape in here came off a real script the tool refused to open. The
error a person saw was:

    Extra data: line 2104 column 1 (char 80848)

That character was the opening brace of the summary block the prompt itself
asks for. The file was right; the reader was wrong.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from media_index import jobs, narration                    # noqa: E402


BEATS = [{"beat": 1, "narration": "A man walks into a room.",
          "shots": [{"kind": "clip", "source": "Breaking Bad",
                     "season_episode": "S04E01", "visual": "a lab",
                     "duration_target_sec": 4}]}]
SUMMARY = {"summary": {"beats": 1, "shots_total": 1, "runs_total": 1,
                       "runs_with_a_scene_range": 1}}


class TestAScriptWithMoreThanOneDocument(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="script_")

    def _write(self, text):
        path = os.path.join(self.tmp, "script.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_the_plain_array_still_reads(self):
        path = self._write(json.dumps(BEATS))
        self.assertEqual(len(jobs.read_beats(path)), 1)

    def test_the_array_followed_by_the_summary_block(self):
        path = self._write(json.dumps(BEATS) + "\n\n\n" + json.dumps(SUMMARY))
        self.assertEqual(len(jobs.read_beats(path)), 1)
        got, note = jobs.script_extras(path)
        self.assertEqual(got["runs_total"], 1)
        self.assertEqual(note, "")

    def test_and_the_plain_english_note_after_that(self):
        """The prompt invites one. On the real script it named the four
        ranges that were guesses — the single most useful thing in the
        file for somebody about to start a forty-minute build."""
        path = self._write(json.dumps(BEATS) + "\n" + json.dumps(SUMMARY)
                           + "\n\nNote: the S04E08 range is a guess and "
                             "should be verified in a player.")
        self.assertEqual(len(jobs.read_beats(path)), 1)
        _summary, note = jobs.script_extras(path)
        self.assertIn("S04E08", note)

    def test_a_note_with_no_summary_block_at_all(self):
        path = self._write(json.dumps(BEATS) + "\n\nThat is everything.")
        self.assertEqual(len(jobs.read_beats(path)), 1)
        summary, note = jobs.script_extras(path)
        self.assertEqual(summary, {})
        self.assertIn("everything", note)

    def test_the_summary_written_first_still_finds_the_beats(self):
        path = self._write(json.dumps(SUMMARY) + "\n" + json.dumps(BEATS))
        self.assertEqual(len(jobs.read_beats(path)), 1)

    def test_a_genuinely_broken_file_still_fails_and_says_where(self):
        """Tolerating a trailing note must not tolerate a broken script.
        A file whose FIRST value will not parse is a broken file."""
        path = self._write('[{"beat": 1, "shots": [}]')
        with self.assertRaises(json.JSONDecodeError):
            jobs.read_beats(path)

    def test_typographic_quotes_are_still_repaired(self):
        path = self._write(json.dumps(BEATS).replace('"visual"', '“visual”')
                           + "\n" + json.dumps(SUMMARY))
        self.assertEqual(len(jobs.read_beats(path)), 1)

    def test_curly_delimiters_with_straight_inner_quotes_are_repaired(self):
        """The Joker-script case: every JSON delimiter is a curly quote, but
        a phrase quoted inside the narration keeps straight quotes. Escaping
        the inner quotes before straightening the delimiters is what lets it
        open at all."""
        text = ('[\n{\n“beat”: 1,\n'
                '“narration”: “She said "good" and smiled”,\n'
                '“shots”: []\n}\n]')
        path = self._write(text)
        beats = jobs.read_beats(path)
        self.assertEqual(len(beats), 1)
        self.assertIn('"good"', beats[0]["narration"])

    def test_a_valid_file_is_never_touched_by_the_inner_quote_repair(self):
        """A normal script with straight delimiters and escaped inner quotes
        parses on the first, untouched try — the bolder repair never runs and
        cannot corrupt it."""
        beats = [{"beat": 1, "narration": 'He said "no" clearly.', "shots": []}]
        path = self._write(json.dumps(beats))
        got = jobs.read_beats(path)
        self.assertEqual(got[0]["narration"], 'He said "no" clearly.')

    def test_a_script_that_will_not_open_reports_nothing_rather_than_raising(self):
        summary, note = jobs.script_extras(os.path.join(self.tmp, "nope.txt"))
        self.assertEqual((summary, note), ({}, ""))


class TestTimingAgainstTheNarrationScript(unittest.TestCase):
    """The beat text is a copy of the narration, and copies drift. Given the
    script that was read aloud, the beats are located inside THAT."""

    CLEAN = ("A man walks into a room where two people think they are about "
             "to die. He does not look at them. "
             "He takes off his jacket and rolls up his sleeves. "
             "And he does all of it in silence, not one word.")

    def _beats(self, *texts):
        return [{"beat": i, "narration": t, "shots": []}
                for i, t in enumerate(texts, 1)]

    def test_each_beat_is_found_in_the_narration_script(self):
        beats = self._beats(
            "A man walks into a room where two people think they are about to die.",
            "He does not look at them.",
            "He takes off his jacket and rolls up his sleeves.")
        ends, drift = narration.beats_in_clean(beats,
                                               narration.normalise(self.CLEAN))
        self.assertEqual(drift, 0)
        self.assertEqual(ends, sorted(ends))
        self.assertEqual(ends[-1], 31)   # ...and rolls up his sleeves

    def test_a_beat_the_model_reworded_is_found_by_its_opening_words(self):
        beats = self._beats(
            "A man walks into a room where two people think they are about to die.",
            "He takes off his jacket and rolls up his sleeves slowly, with care.")
        ends, _drift = narration.beats_in_clean(beats,
                                                narration.normalise(self.CLEAN))
        self.assertIsNotNone(ends)
        self.assertEqual(ends, sorted(ends))

    def test_a_narration_script_for_a_different_video_is_refused(self):
        """It would sail through every other check and quietly retime the
        whole build."""
        beats = self._beats("Winter is coming to the north.",
                            "The lord commander said nothing.",
                            "Snow fell on the wall for three days.",
                            "Nobody came back through the gate.")
        ends, drift = narration.beats_in_clean(beats,
                                               narration.normalise(self.CLEAN))
        self.assertIsNone(ends)
        self.assertGreater(drift, 0)

    def test_align_falls_back_to_the_beats_when_it_does_not_match(self):
        beats = self._beats("Winter is coming.", "Nobody came back.",
                            "The gate stayed shut.", "Snow fell for days.")
        spoken = [narration.Word(text=w, start=i * 0.5, end=i * 0.5 + 0.4)
                  for i, w in enumerate(narration.normalise(
                      "winter is coming nobody came back the gate stayed shut "
                      "snow fell for days"))]
        got = narration.align(beats, spoken, total_seconds=8.0,
                              clean=self.CLEAN)
        self.assertFalse(got.used_clean)
        self.assertTrue(got.ok)

    def test_no_narration_script_leaves_everything_exactly_as_it_was(self):
        beats = self._beats("Winter is coming.", "Nobody came back.")
        spoken = [narration.Word(text=w, start=i * 0.5, end=i * 0.5 + 0.4)
                  for i, w in enumerate(narration.normalise(
                      "winter is coming nobody came back"))]
        with_none = narration.align(beats, spoken, total_seconds=4.0)
        with_empty = narration.align(beats, spoken, total_seconds=4.0, clean="")
        self.assertEqual(with_none.spans, with_empty.spans)
        self.assertFalse(with_none.used_clean)

    def test_a_missing_narration_file_is_simply_no_narration_script(self):
        self.assertEqual(narration.read_clean("/nowhere/at/all.txt"), "")
        self.assertEqual(narration.read_clean(""), "")


if __name__ == "__main__":
    unittest.main()
