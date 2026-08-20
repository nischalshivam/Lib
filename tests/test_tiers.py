"""What a placement is worth, and what each mode is allowed to do with it.

Every version of this tool until now had one word for "we put something
there": *placed*. A shot found by a quoted line and a shot picked by a
golden-ratio counter were both `placed`, both went into the video, and both
looked identical until somebody watched it. That single word is why a week
went into "why are the clips random".
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from media_index import placeholder, tiers                  # noqa: E402


class TestWhatAPlacementIsWorth(unittest.TestCase):

    def test_a_quoted_line_and_a_typed_time_are_the_only_tier_a(self):
        self.assertEqual(tiers.tier_of("anchor"), "A")
        self.assertEqual(tiers.tier_of("stated"), "A")
        self.assertEqual(tiers.tier_of("chosen"), "A")

    def test_a_guess_between_two_placed_shots_is_tier_b(self):
        self.assertEqual(tiers.tier_of("interpolated"), "B")
        self.assertEqual(tiers.tier_of("picture"), "B")
        self.assertEqual(tiers.tier_of("verified"), "B")

    def test_everything_that_exists_to_cover_a_hole_is_tier_c(self):
        """`paced` and `filler` exist to fill gaps. They can never be the
        thing that proves there is no gap."""
        self.assertEqual(tiers.tier_of("filler"), "C")
        self.assertEqual(tiers.tier_of("paced"), "C")
        self.assertEqual(tiers.tier_of("none"), "C")

    def test_a_stated_range_lifts_a_guess_to_b_and_never_to_a(self):
        """A typed range says which four minutes, not which second."""
        self.assertEqual(tiers.tier_of("filler", stated=True), "B")
        self.assertEqual(tiers.tier_of("paced", stated=True), "B")
        self.assertEqual(tiers.tier_of("interpolated", stated=True), "B")

    def test_an_unknown_method_is_never_trusted(self):
        self.assertEqual(tiers.tier_of("some_new_idea"), "C")
        self.assertEqual(tiers.tier_of(""), "C")
        self.assertEqual(tiers.tier_of(None), "C")


class TestWhatEachModeShows(unittest.TestCase):

    def test_strict_shows_only_what_is_proven(self):
        self.assertTrue(tiers.places(tiers.STRICT, "A"))
        self.assertFalse(tiers.places(tiers.STRICT, "B"))
        self.assertFalse(tiers.places(tiers.STRICT, "C"))

    def test_balanced_shows_good_guesses_too(self):
        self.assertTrue(tiers.places(tiers.BALANCED, "A"))
        self.assertTrue(tiers.places(tiers.BALANCED, "B"))
        self.assertFalse(tiers.places(tiers.BALANCED, "C"))

    def test_draft_shows_everything(self):
        for tier in ("A", "B", "C"):
            self.assertTrue(tiers.places(tiers.DRAFT, tier))

    def test_no_filler_can_ever_reach_strict(self):
        """The invariant the whole design rests on. If this ever passes for
        filler, Strict mode is a lie."""
        for method in ("filler", "paced", "none"):
            for stated in (False, True):
                tier = tiers.tier_of(method, stated=stated)
                self.assertFalse(tiers.places(tiers.STRICT, tier),
                                 msg=f"{method} stated={stated} -> {tier}")

    def test_an_unknown_mode_falls_back_to_balanced(self):
        self.assertEqual(tiers.normalise("STRICT"), tiers.STRICT)
        self.assertEqual(tiers.normalise("nonsense"), tiers.BALANCED)
        self.assertEqual(tiers.normalise(None), tiers.BALANCED)


class TestTheCard(unittest.TestCase):

    def test_it_says_what_was_wanted_and_why_it_is_missing(self):
        lines = placeholder.lines_for({
            "scene": 23, "seconds": 4.2,
            "narration": "Hank picks up the book.",
            "episode": "S05E08", "why": "koi quoted line nahi mili",
            "must_show": ["Hank"], "options": 3})
        text = "\n".join(lines)
        self.assertIn("NEEDS VISUAL", text)
        self.assertIn("S05E08", text)
        self.assertIn("Hank", text)
        self.assertIn("4.2 sec", text)
        self.assertIn("koi quoted line nahi mili", text)

    def test_a_bare_request_still_makes_a_readable_card(self):
        text = "\n".join(placeholder.lines_for({}))
        self.assertIn("NEEDS VISUAL", text)

    def test_long_narration_is_wrapped_not_dumped(self):
        long = " ".join(["word"] * 200)
        lines = placeholder.lines_for({"narration": long})
        for line in "\n".join(lines).split("\n"):
            self.assertLessEqual(len(line), 60)

    @unittest.skipUnless(placeholder.font_file(), "no font on this machine")
    def test_a_card_is_actually_written(self):
        import tempfile

        out = os.path.join(tempfile.mkdtemp(prefix="card_"), "c.png")
        got = placeholder.card(out, {"scene": 1, "seconds": 3.0,
                                     "narration": "A line."})
        self.assertTrue(got, "no card was drawn")
        self.assertGreater(os.path.getsize(got), 1000)


if __name__ == "__main__":
    unittest.main()
