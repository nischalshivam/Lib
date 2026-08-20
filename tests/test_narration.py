"""Tests for finding out when each beat is actually spoken.

The timeline used to assume an even read at 150 words a minute. Real reads
are not even — a narrator pauses on the turn, races the list, holds the last
line — and the drift is not spread out, it is concentrated wherever the
pauses are. Every visual after a long pause sits under the wrong sentence.

faster-whisper is not needed here. What is tested is the matching: given the
words that were heard and when, do the beat boundaries land where the words
actually changed?
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from media_index import narration, timeline                    # noqa: E402


def spoken(pairs) -> list:
    """[(word, start)] -> Word objects, each a quarter second long."""
    return [narration.Word(text=w, start=t, end=t + 0.25) for w, t in pairs]


def read_aloud(beats, wpm=150.0, start=0.0, pauses=None) -> list:
    """Pretend to record the script, optionally pausing between beats."""
    pauses = pauses or {}
    out, t = [], start
    step = 60.0 / wpm
    for i, beat in enumerate(beats, 1):
        t += pauses.get(i, 0.0)
        for word in narration.normalise(beat.get("narration") or ""):
            out.append(narration.Word(text=word, start=round(t, 3),
                                      end=round(t + step * 0.8, 3)))
            t += step
    return out


BEATS = [
    {"beat": 1, "narration": "A quiet chemist walks into the superlab "
                             "carrying nothing at all"},
    {"beat": 2, "narration": "He removes his jacket and folds it across "
                             "a metal rail deliberately"},
    {"beat": 3, "narration": "Nobody speaks while the apron is tied"},
    {"beat": 4, "narration": "Then he reaches down and lifts a green "
                             "boxcutter from the bench"},
]


class TestReadingTheScript(unittest.TestCase):
    def test_words_are_lowercased_and_stripped(self):
        self.assertEqual(narration.normalise("Well? Get BACK to work!"),
                         ["well", "get", "back", "to", "work"])

    def test_apostrophes_survive_because_the_transcript_keeps_them(self):
        self.assertEqual(narration.normalise("don't"), ["don't"])

    def test_every_beat_ends_at_a_word_count(self):
        words, ends = narration.script_words(BEATS)
        self.assertEqual(ends[-1], len(words))
        self.assertEqual(ends, sorted(ends))

    def test_a_beat_with_no_narration_does_not_move_the_count(self):
        words, ends = narration.script_words([{"narration": "one two"},
                                              {"narration": ""},
                                              {"narration": "three"}])
        self.assertEqual(ends, [2, 2, 3])


class TestFindingTheUnmistakableWords(unittest.TestCase):
    def test_a_word_used_once_on_each_side_is_an_anchor(self):
        got = narration.unique_anchors(["the", "green", "the"],
                                       ["the", "green", "the"])
        self.assertEqual(got, [(1, 1)])

    def test_a_common_word_is_skipped_rather_than_guessed_at(self):
        # "the" appears twice; there is no way to know which is which, so it
        # contributes nothing. That is why this needs no threshold.
        got = narration.unique_anchors(["the", "a", "the"], ["the", "a", "the"])
        self.assertEqual([s for s, _ in got], [1])

    def test_a_word_only_one_side_has_is_not_an_anchor(self):
        self.assertEqual(narration.unique_anchors(["alpha"], ["beta"]), [])

    def test_a_real_script_yields_plenty(self):
        words, _ = narration.script_words(BEATS)
        heard = [w.text for w in read_aloud(BEATS)]
        got = narration.unique_anchors(words, heard)
        self.assertGreater(len(got), 15)


class TestKeepingOnlyWhatAgreesOnOrder(unittest.TestCase):
    def test_a_pair_out_of_sequence_is_dropped(self):
        # A transcriber mishearing one word as another that appears elsewhere
        # produces exactly this, and one such pair would drag every boundary
        # near it.
        got = narration.increasing([(0, 0), (1, 90), (2, 2), (3, 3), (4, 4)])
        self.assertEqual(got, [(0, 0), (2, 2), (3, 3), (4, 4)])

    def test_an_already_ordered_run_is_untouched(self):
        pairs = [(0, 1), (2, 5), (7, 9)]
        self.assertEqual(narration.increasing(pairs), pairs)

    def test_nothing_in_nothing_out(self):
        self.assertEqual(narration.increasing([]), [])

    def test_it_copes_with_a_long_script(self):
        pairs = [(i, i) for i in range(4000)]
        self.assertEqual(len(narration.increasing(pairs)), 4000)


class TestPlacingTheBoundaries(unittest.TestCase):
    def test_an_even_read_lands_where_the_words_change(self):
        heard = read_aloud(BEATS)
        got = narration.align(BEATS, heard)
        self.assertTrue(got.ok, got.reason)
        words, ends = narration.script_words(BEATS)
        for i, end_word in enumerate(ends[:-1]):
            self.assertAlmostEqual(got.spans[i][1], heard[end_word].start,
                                   delta=0.5)

    def test_a_pause_moves_only_what_comes_after_it(self):
        # The whole point. A ten-second silence before beat 3 must leave
        # beat 1 exactly where it was and push everything after by ten.
        even = narration.align(BEATS, read_aloud(BEATS))
        paused = narration.align(BEATS, read_aloud(BEATS, pauses={3: 10.0}))
        self.assertAlmostEqual(even.spans[0][1], paused.spans[0][1], delta=0.5)
        self.assertAlmostEqual(paused.spans[2][1] - even.spans[2][1], 10.0,
                               delta=1.0)

    def test_the_picture_holds_through_a_silence_rather_than_cutting_away(self):
        # A beat ends when the NEXT one starts speaking, not when this one
        # stops. Ten seconds of silence between them belongs to the picture
        # that is already up — ending beat 2 on its last word would leave ten
        # seconds of the video with nothing on screen.
        even = narration.align(BEATS, read_aloud(BEATS))
        paused = narration.align(BEATS, read_aloud(BEATS, pauses={3: 10.0}))
        self.assertAlmostEqual(paused.spans[1][1] - even.spans[1][1], 10.0,
                               delta=1.0)
        self.assertAlmostEqual(paused.spans[1][1], paused.spans[2][0],
                               places=6)

    def test_an_estimate_would_have_got_that_wrong(self):
        # Proof the work is worth doing: the same recording, estimated from
        # word counts, puts beat 2 many seconds from where it really ends.
        heard = read_aloud(BEATS, pauses={3: 10.0})
        total = heard[-1].end
        guessed = timeline.boundaries(BEATS, total_seconds=total)
        listened = narration.align(BEATS, heard, total_seconds=total)
        drift = abs(guessed[1][1] - listened.spans[1][1])
        self.assertGreater(drift, 2.0,
                           "the estimate happened to be right; pick a harder case")

    def test_beats_still_run_back_to_back(self):
        got = narration.align(BEATS, read_aloud(BEATS, pauses={2: 3.0}))
        for (_s0, e0), (s1, _e1) in zip(got.spans, got.spans[1:]):
            self.assertAlmostEqual(e0, s1, places=6)

    def test_the_last_beat_runs_to_the_end_of_the_recording(self):
        # The closing line is where a narrator slows right down, and cutting
        # the picture before the voice stops is the most visible mistake
        # there is.
        heard = read_aloud(BEATS)
        got = narration.align(BEATS, heard, total_seconds=90.0)
        self.assertAlmostEqual(got.spans[-1][1], 90.0, places=2)

    def test_a_slow_reader_is_followed_not_corrected(self):
        heard = read_aloud(BEATS, wpm=95.0)
        got = narration.align(BEATS, heard)
        _words, ends = narration.script_words(BEATS)
        self.assertAlmostEqual(got.spans[-1][0], heard[ends[-2]].start,
                               delta=1.0)

    def test_a_boundary_far_from_any_anchor_is_named(self):
        # Two long stretches of nothing but common, repeated words: no word
        # in them can be matched unambiguously, so the boundary between them
        # is interpolated across eighty words. It is still placed — but the
        # editor is told which ones were placed on a guess.
        filler = " ".join(["the", "a", "of", "and"] * 20)
        beats = [{"narration": filler}, {"narration": filler},
                 {"narration": "boxcutter apron superlab chemist rail"}]
        got = narration.align(beats, read_aloud(beats))
        self.assertTrue(got.ok, got.reason)
        self.assertIn(1, got.weak)


class TestWhenItCannotWork(unittest.TestCase):
    def test_the_wrong_recording_is_refused_rather_than_forced(self):
        heard = spoken([("completely", 0.0), ("different", 1.0),
                        ("audio", 2.0), ("entirely", 3.0)])
        got = narration.align(BEATS, heard)
        self.assertFalse(got.ok)
        self.assertIn("right audio", got.reason)

    def test_silence_is_reported(self):
        got = narration.align(BEATS, [])
        self.assertFalse(got.ok)
        self.assertIn("nothing was heard", got.reason)

    def test_a_script_with_no_narration_is_reported(self):
        got = narration.align([{"beat": 1}], read_aloud(BEATS))
        self.assertFalse(got.ok)
        self.assertIn("no narration text", got.reason)

    def test_a_failure_reads_as_one_in_the_summary(self):
        self.assertIn("not aligned", narration.align(BEATS, []).summary())

    def test_a_missing_recording_is_a_reason_not_a_crash(self):
        got = narration.align_audio(BEATS, "/no/such/file.mp3")
        self.assertFalse(got.ok)
        self.assertTrue(got.reason)


class TestFeedingItToTheTimeline(unittest.TestCase):
    def _manifest(self, n):
        return {"video": "v", "scenes": [
            {"scene": i, "assets": [
                {"file": f"clip_{i}.mp4", "kind": "video"},
                {"file": f"img_{i}.jpg", "kind": "image"}]}
            for i in range(1, n + 1)]}

    def test_measured_boundaries_are_used_over_the_estimate(self):
        heard = read_aloud(BEATS, pauses={3: 10.0})
        got = narration.align(BEATS, heard, total_seconds=heard[-1].end)
        tl = timeline.plan(BEATS, self._manifest(4), spans=got.spans,
                           total_seconds=heard[-1].end)
        for scene, (start, end) in zip(tl.scenes, got.spans):
            self.assertAlmostEqual(scene.start, start, places=2)
            self.assertAlmostEqual(scene.end, end, places=2)

    def test_a_mismatched_span_count_falls_back_rather_than_misaligning(self):
        # Spans for three beats against a four-beat script would silently put
        # every picture one beat out. The estimate is wrong; that is worse.
        tl = timeline.plan(BEATS, self._manifest(4), total_seconds=60.0,
                           spans=[(0, 1), (1, 2), (2, 3)])
        self.assertEqual(len(tl.scenes), 4)
        self.assertAlmostEqual(tl.scenes[-1].end, 60.0, places=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
