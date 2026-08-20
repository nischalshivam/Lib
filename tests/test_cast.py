"""Who is in the frame, from a handful of photographs.

The complaint being answered, in the words it arrived in: a sentence about
Walter White and Gus, with Walter's wife and son on the screen. No
description can catch that — the kitchen looks like the kitchen either way.
"""
import os
import shutil
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from media_index import cast, embed, visual                 # noqa: E402


class TestReadingACastFolder(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cast_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _person(self, name, files):
        here = os.path.join(self.tmp, name)
        os.makedirs(here, exist_ok=True)
        for f in files:
            open(os.path.join(here, f), "wb").close()

    def test_a_folder_per_character_is_counted_without_any_model(self):
        self._person("Gus", ["1.jpg", "2.png", "notes.txt"])
        self._person("Walter", ["a.jpeg"])
        got = cast.look(self.tmp)
        self.assertEqual(got, [{"name": "Gus", "images": 2},
                               {"name": "Walter", "images": 1}])

    def test_a_folder_that_is_not_there_says_so_rather_than_crashing(self):
        with self.assertRaises(cast.CastError):
            cast.look(os.path.join(self.tmp, "nope"))

    def test_a_folder_of_loose_images_is_refused_with_the_shape_wanted(self):
        """Someone will drop ten stills straight into `cast\\`. Telling them
        the structure in the first second beats a forty-minute build that
        quietly did nothing."""
        open(os.path.join(self.tmp, "gus.jpg"), "wb").close()
        with self.assertRaises(cast.CastError) as caught:
            cast.look(self.tmp)
        self.assertIn("Gus", str(caught.exception))


class TestWhoAShotNames(unittest.TestCase):

    def setUp(self):
        self.people = {
            "gus": cast.Person(name="Gus"),
            "walter": cast.Person(name="Walter"),
            "walt jr": cast.Person(name="Walt Jr"),
        }

    def test_the_characters_field_is_read_first(self):
        got = cast.named_in({"characters": ["Gus Fring", "Walter White"]},
                            self.people)
        self.assertEqual([p.name for p in got], ["Gus", "Walter"])

    def test_a_comma_separated_string_works_too(self):
        got = cast.named_in({"characters": "Gus, Walter"}, self.people)
        self.assertEqual(len(got), 2)

    def test_a_script_written_before_this_existed_still_benefits(self):
        """A caption saying "Gus stands over Victor" names him as clearly as
        a list would, and every script already written says it that way."""
        got = cast.named_in({"visual": "Gus stands over the body, calm"},
                            self.people)
        self.assertEqual([p.name for p in got], ["Gus"])

    def test_a_shot_naming_nobody_known_names_nobody(self):
        self.assertEqual(cast.named_in({"visual": "an empty desert road"},
                                       self.people), [])

    def test_no_cast_folder_means_no_opinion_at_all(self):
        self.assertEqual(cast.named_in({"characters": ["Gus"]}, {}), [])


class TestWhatPresenceDoes(unittest.TestCase):
    """The reference vectors are compared to frames already on disk, in the
    same space, so this needs no model and no images — only vectors."""

    def setUp(self):
        self.backend = embed.Deterministic(dim=48)
        words = ["kitchen", "desert", "car", "lab", "diner", "office"]
        captions = [f"a {words[i % len(words)]}, frame {i}" for i in range(60)]
        # Ten consecutive frames really are of this person.
        captions[20:30] = ["a calm man in glasses and a yellow shirt"] * 10
        times = np.arange(0, 60, 1.0, dtype=np.float32)
        vecs = self.backend.encode_texts(captions).astype(np.float32)
        self.index = visual.VisualIndex(path="ep.mkv", times=times, vecs=vecs,
                                        model=self.backend.name)
        self.gus = cast.Person(
            name="Gus",
            vec=self.backend.encode_texts(
                ["a calm man in glasses and a yellow shirt"])[0])

    def test_the_frames_a_person_is_in_stand_out_from_the_rest(self):
        lifts = cast.presence(self.index, self.gus)
        self.assertGreater(float(lifts[20:30].mean()),
                           float(lifts[:20].mean()) + 1.0)

    def test_the_bonus_is_bounded_so_it_can_never_overrule_a_quoted_line(self):
        bonus = cast.frames_with(self.index, [self.gus])
        self.assertGreaterEqual(float(bonus.min()), 0.0)
        self.assertLessEqual(float(bonus.max()), 1.0)
        self.assertGreater(float(bonus[20:30].max()), 0.0)

    def test_a_person_with_no_reference_images_changes_nothing(self):
        empty = cast.Person(name="Nobody")
        self.assertFalse(np.any(cast.presence(self.index, empty)))
        self.assertFalse(np.any(cast.frames_with(self.index, [empty])))

    def test_naming_three_people_takes_the_best_of_them_not_all_three(self):
        """A script naming "Gus, Walter, Jesse" for a wide shot is naming who
        is in the SCENE. Demanding all three would rule out every close-up
        in it."""
        nobody = cast.Person(
            name="Nobody",
            vec=self.backend.encode_texts(["a snow covered mountain"])[0])
        both = cast.frames_with(self.index, [self.gus, nobody])
        alone = cast.frames_with(self.index, [self.gus])
        self.assertTrue(np.all(both >= alone - 1e-6))
        self.assertGreater(float(both[20:30].max()), 0.0)

    def test_an_empty_list_is_no_opinion_rather_than_a_penalty(self):
        self.assertFalse(np.any(cast.frames_with(self.index, [])))


class TestTheBonusInsideTheSearch(unittest.TestCase):

    def setUp(self):
        self.backend = embed.Deterministic(dim=48)
        captions = [f"a room number {i}" for i in range(60)]
        captions[10] = "a red doorway at night"
        captions[40] = "a red doorway at night"
        times = np.arange(0, 60, 1.0, dtype=np.float32)
        vecs = self.backend.encode_texts(captions).astype(np.float32)
        self.index = visual.VisualIndex(path="ep.mkv", times=times, vecs=vecs,
                                        model=self.backend.name)
        self.vec = self.backend.encode_texts(["a red doorway at night"])[0]

    def test_without_a_bonus_the_search_is_exactly_what_it_was(self):
        got = visual.best_in(self.index, self.vec)
        self.assertIn(got.time, (10.0, 40.0))

    def test_a_bonus_breaks_the_tie_towards_the_frame_somebody_is_in(self):
        bonus = np.zeros(len(self.index), dtype=np.float32)
        bonus[40] = 0.5
        got = visual.best_in(self.index, self.vec, bonus=bonus)
        self.assertEqual(got.time, 40.0)

    def test_a_bonus_cannot_conjure_a_match_out_of_nothing(self):
        """It is a nudge, not a veto. A frame nothing matched must still fail
        the episode's own noise floor, or the one label this tool promises
        means nothing."""
        bonus = np.zeros(len(self.index), dtype=np.float32)
        bonus[3] = cast.CAST_WEIGHT
        got = visual.best_in(self.index,
                             self.backend.encode_texts(["a lighthouse"])[0],
                             bonus=bonus)
        self.assertLess(got.lift, visual.LIFT_OK + cast.CAST_WEIGHT + 0.01)

    def test_lifts_of_agrees_with_lift_of_frame_by_frame(self):
        sims = self.index.similarities(self.vec)
        every = visual.lifts_of(sims)
        for i in (0, 10, 25, 40, 59):
            self.assertAlmostEqual(float(every[i]),
                                   visual.lift_of(sims, float(sims[i])),
                                   places=4)


if __name__ == "__main__":
    unittest.main()
