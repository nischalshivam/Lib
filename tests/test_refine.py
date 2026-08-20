"""Which shots the verifier is allowed to move, and what happens when it does.

The model call and the frame grab are injected as fakes here, so these tests
are entirely about judgement: only interpolated shots inside a wide window
are eligible; an anchor or a stated time is never touched; a confident
verdict moves a shot and marks it `vlm` (Tier B); an abstention leaves it
exactly where interpolation put it; and nothing in here can raise a build to
its knees.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from media_index import align, gemini, refine, tiers      # noqa: E402


def placement(beat, shot, method="interpolated", start=1000_000):
    p = align.Placement(beat=beat, shot=shot, path="ep.mkv")
    p.method = method
    p.start_ms = start
    p.end_ms = start + 4000
    return p


def beats_with(n_shots, visual="a bell is struck", people=None):
    shots = [{"source": "Breaking Bad", "season_episode": "S04E13",
              "visual": visual, "characters": people or []}
             for _ in range(n_shots)]
    return [{"beat": 1, "shots": shots}]


class TestWhoIsEligible(unittest.TestCase):

    def test_every_guessed_method_is_offered(self):
        """interpolated, paced and homeless are all guesses worth a look —
        the Hank build had 40 wrong shots and only the interpolated ones
        (20) were being offered."""
        ps = [placement(1, 1, "interpolated"),
              placement(1, 2, "paced"),
              placement(1, 3, "none"),
              placement(1, 4, "anchor"),
              placement(1, 5, "picture")]
        windows = {(1, i): (0.0, 300.0) for i in range(1, 6)}
        got = refine.eligible(ps, windows)
        self.assertEqual(sorted(p.shot for p, _ in got), [1, 2, 3])

    def test_a_tight_window_is_left_alone(self):
        ps = [placement(1, 1, "interpolated")]
        windows = {(1, 1): (100.0, 115.0)}   # 15s — a guess lands close
        self.assertEqual(refine.eligible(ps, windows), [])

    def test_a_shot_with_no_window_is_skipped(self):
        ps = [placement(1, 1, "interpolated")]
        self.assertEqual(refine.eligible(ps, {}), [])

    def test_a_homeless_shot_with_no_source_file_is_skipped(self):
        p = placement(1, 1, "none")
        p.path = ""
        self.assertEqual(refine.eligible([p], {(1, 1): (0.0, 300.0)}), [])

    def test_an_anchor_and_a_stated_time_are_never_touched(self):
        ps = [placement(1, 1, "anchor"), placement(1, 2, "stated")]
        windows = {(1, 1): (0.0, 600.0), (1, 2): (0.0, 600.0)}
        self.assertEqual(refine.eligible(ps, windows), [])


class TestWhereToSample(unittest.TestCase):

    def test_frames_span_the_window_and_are_not_too_dense(self):
        ts = refine.candidate_times((0.0, 300.0))
        self.assertEqual(ts[0], 0.0)
        self.assertEqual(ts[-1], 300.0)
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        self.assertTrue(all(g >= refine.MIN_FRAME_GAP_S for g in gaps))

    def test_a_short_window_yields_few_frames_not_a_crowd(self):
        ts = refine.candidate_times((0.0, 6.0))
        self.assertLessEqual(len(ts), 4)
        self.assertGreaterEqual(len(ts), 2)


class TestApplyingAVerdict(unittest.TestCase):

    def test_a_confident_choice_moves_the_shot_and_marks_it_vlm(self):
        p = placement(1, 1, start=1000_000)
        moved = refine.apply_choice(
            p, gemini.Choice(index=0, at_s=2262.0, confidence=0.9,
                             reason="bell"))
        self.assertTrue(moved)
        self.assertEqual(p.method, "vlm")
        self.assertEqual(p.start_ms, 2262_000)
        self.assertEqual(p.end_ms, 2262_000 + 4000)   # length preserved
        self.assertEqual(tiers.tier_of(p.method), "B")

    def test_an_abstention_changes_nothing(self):
        p = placement(1, 1, start=1000_000)
        moved = refine.apply_choice(p, gemini.Choice(index=-1))
        self.assertFalse(moved)
        self.assertEqual(p.method, "interpolated")
        self.assertEqual(p.start_ms, 1000_000)

    def test_a_homeless_shot_gets_a_real_length_when_rescued(self):
        p = placement(1, 1, "none", start=0)
        p.end_ms = 0                          # no length yet
        moved = refine.apply_choice(
            p, gemini.Choice(index=0, at_s=610.0, confidence=0.9, reason="x"),
            want_s=5.0)
        self.assertTrue(moved)
        self.assertEqual(p.method, "vlm")
        self.assertEqual(p.start_ms, 610_000)
        self.assertEqual(p.end_ms, 615_000)   # 5s, not an instant


class TestTheWholeStepWithAFakeModel(unittest.TestCase):

    def _grab(self, path, at):
        return b"\xff\xd8jpeg"          # a non-empty "frame" at any time

    def test_it_moves_a_silent_shot_onto_the_chosen_frame(self):
        beats = beats_with(1)
        ps = [placement(1, 1, "interpolated")]
        windows = {(1, 1): (2000.0, 2300.0)}
        seen = {}

        def ask(intent, frames, people):
            seen["intent"] = intent
            seen["people"] = people
            return gemini.Choice(index=0, at_s=frames[3].at_s,
                                 confidence=0.9, reason="bell visible")

        out = refine.refine_runs(beats, ps, windows, grab=self._grab, ask=ask)
        self.assertEqual(out.moved, 1)
        self.assertEqual(ps[0].method, "vlm")
        self.assertIn("bell", seen["intent"])

    def test_the_intent_carries_the_characters_that_must_be_visible(self):
        beats = beats_with(1, people=["Hector", "Gus"])
        ps = [placement(1, 1, "interpolated")]
        windows = {(1, 1): (2000.0, 2300.0)}
        grabbed = {}

        def ask(intent, frames, people):
            grabbed["people"] = people
            return gemini.Choice(index=-1)

        refine.refine_runs(beats, ps, windows, grab=self._grab, ask=ask)
        self.assertEqual(grabbed["people"], ["Hector", "Gus"])

    def test_a_model_that_raises_never_breaks_the_build(self):
        beats = beats_with(1)
        ps = [placement(1, 1, "interpolated")]
        windows = {(1, 1): (2000.0, 2300.0)}

        def boom(intent, frames, people):
            raise RuntimeError("proxy 500")

        out = refine.refine_runs(beats, ps, windows, grab=self._grab, ask=boom)
        self.assertEqual(out.moved, 0)
        self.assertEqual(ps[0].method, "interpolated")   # untouched

    def test_no_eligible_shots_means_no_model_call(self):
        beats = beats_with(1)
        ps = [placement(1, 1, "anchor")]
        windows = {(1, 1): (2000.0, 2300.0)}
        called = {"n": 0}

        def ask(*a):
            called["n"] += 1
            return gemini.Choice()

        refine.refine_runs(beats, ps, windows, grab=self._grab, ask=ask)
        self.assertEqual(called["n"], 0)

    def test_frames_that_cannot_be_grabbed_skip_the_shot(self):
        beats = beats_with(1)
        ps = [placement(1, 1, "interpolated")]
        windows = {(1, 1): (2000.0, 2300.0)}

        out = refine.refine_runs(beats, ps, windows,
                                 grab=lambda p, at: b"",   # nothing extracts
                                 ask=lambda *a: gemini.Choice(index=0, at_s=1,
                                                              confidence=0.9))
        self.assertEqual(out.moved, 0)


if __name__ == "__main__":
    unittest.main()
