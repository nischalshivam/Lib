"""Stage 2: match a script's shot-requests to catalogued shots."""
import unittest

from media_index import catalog, plan


def _lib():
    return {
        "s1": catalog.Shot("s1", "J", "/j.mp4", 890.0, 895.0,
                           description="a man on a tv talk show set introduces a clip",
                           tags=["tv", "talk show", "stage"],
                           characters=["Murray"], quality="high",
                           dialogue="Check out this joker."),
        "s2": catalog.Shot("s2", "J", "/j.mp4", 60.0, 65.0,
                           description="a thin man in clown makeup spins a sign on a sidewalk",
                           tags=["clown", "sign", "sidewalk", "street"],
                           characters=["Arthur"], quality="high", dialogue=""),
        "s3": catalog.Shot("s3", "J", "/j.mp4", 120.0, 125.0,
                           description="an empty city street with garbage bags",
                           tags=["street", "garbage", "city"],
                           characters=[], quality="high", dialogue=""),
    }


class TestDialogueAnchor(unittest.TestCase):

    def test_a_quoted_line_anchors_to_the_shot_that_says_it(self):
        got = plan.dialogue_anchor(_lib(), "Check out this joker")
        self.assertEqual(got[0].id, "s1")

    def test_too_short_a_line_never_anchors(self):
        self.assertEqual(plan.dialogue_anchor(_lib(), "Stop"), [])


class TestMatch(unittest.TestCase):

    def test_a_line_wins_by_dialogue_even_if_the_visual_is_vague(self):
        req = plan.Request(visual="a person talks", characters=["Murray"],
                           dialogue="Check out this joker")
        m = plan.match(req, _lib())
        self.assertEqual(m.method, "dialogue")
        self.assertEqual(m.shot.id, "s1")

    def test_a_silent_request_falls_to_description_and_character(self):
        req = plan.Request(visual="clown spinning a sign on the sidewalk",
                           characters=["Arthur"])
        m = plan.match(req, _lib())
        self.assertEqual(m.method, "description")
        self.assertEqual(m.shot.id, "s2")

    def test_nothing_matching_is_an_honest_gap(self):
        req = plan.Request(visual="a spaceship over the ocean",
                           characters=["Zorg"])
        m = plan.match(req, _lib())
        self.assertEqual(m.method, "none")
        self.assertFalse(m.placed)


class TestScoping(unittest.TestCase):

    def _series(self):
        return {
            "e1_a": catalog.Shot("e1_a", "Breaking Bad S04E01", "/1.mp4",
                                 2450, 2455, description="Gus picks up a box cutter",
                                 characters=["Gus Fring"], quality="high"),
            "e1_b": catalog.Shot("e1_b", "Breaking Bad S04E01", "/1.mp4",
                                 300, 305, description="Gale talks in the lab early on",
                                 characters=["Gale Boetticher"], quality="high"),
            "e11": catalog.Shot("e11", "Breaking Bad S04E11", "/11.mp4",
                                340, 345, description="a box cutter on a table",
                                characters=[], quality="high"),
        }

    def test_scope_confines_to_one_episode(self):
        req = plan.Request(visual="box cutter")
        m = plan.match(req, self._series(), scope="S04E01")
        self.assertIn("S04E01", m.shot.source)

    def test_scene_range_confines_within_the_episode(self):
        # box-cutter scene is ~40 min in; the range must beat the early lab shot
        req = plan.Request(visual="box cutter", source="S04E01",
                           scene_range="40:00-45:00")
        m = plan.match(req, self._series())
        self.assertEqual(m.shot.id, "e1_a")

    def test_range_parsing(self):
        self.assertEqual(plan._range_seconds("40:00-45:00"), (2400.0, 2700.0))
        self.assertEqual(plan._range_seconds("02:00-05:00"), (120.0, 300.0))
        self.assertEqual(plan._range_seconds("nope"), ())


class TestPlanWholeScript(unittest.TestCase):

    def test_beats_become_requests_and_a_plan_with_stats(self):
        beats = [{"beat": 1, "shots": [
            {"visual": "clown spins a sign on the sidewalk",
             "characters": "['Arthur']"},
            {"visual": "a man on a talk show", "characters": ["Murray"],
             "exact_dialogue": "Check out this joker"},
            {"visual": "a spaceship", "characters": ["Zorg"]},
        ]}]
        pairs, stats = plan.plan(beats, _lib())
        self.assertEqual(stats.total, 3)
        self.assertEqual(stats.by_method.get("dialogue"), 1)
        self.assertEqual(stats.by_method.get("description"), 1)
        self.assertEqual(stats.by_method.get("none"), 1)
        self.assertEqual(stats.placed, 2)
        self.assertAlmostEqual(stats.coverage, 2 / 3)

    def test_scene_range_carries_across_a_run_but_resets_on_new_episode(self):
        beats = [
            {"beat": 1, "shots": [
                {"visual": "a", "season_episode": "S04E01",
                 "scene_range": "27:00-35:00"},
                {"visual": "b", "season_episode": "S04E01"},          # inherits
                {"visual": "c", "season_episode": "S04E01",
                 "scene_range": "00:00-03:30"}]},                     # new scene
            {"beat": 2, "shots": [
                {"visual": "d", "season_episode": "S03E13"}]},        # new episode
        ]
        reqs = plan.requests_from_beats(beats)
        self.assertEqual(reqs[0].scene_range, "27:00-35:00")
        self.assertEqual(reqs[1].scene_range, "27:00-35:00")          # carried
        self.assertEqual(reqs[2].scene_range, "00:00-03:30")          # replaced
        self.assertEqual(reqs[3].scene_range, "")                     # reset

    def test_a_stringified_character_field_is_parsed(self):
        reqs = plan.requests_from_beats(
            [{"beat": 1, "shots": [{"visual": "x", "characters": "['Arthur']"}]}])
        self.assertEqual(reqs[0].character, "Arthur")


if __name__ == "__main__":
    unittest.main()
