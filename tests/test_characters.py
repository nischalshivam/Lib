"""The script tells the tool whose photos it will need, before any build."""
import unittest

from media_index import characters


def _beat(narration="", shots=()):
    return {"beat": 1, "narration": narration, "shots": list(shots)}


class TestCharactersFromScript(unittest.TestCase):

    def test_named_fields_are_ranked_most_central_first(self):
        beats = [
            _beat("Arthur watches Murray on the television.", [
                {"characters": ["Arthur"], "visual": "Arthur alone"},
                {"characters": ["Arthur", "Murray"], "visual": "Arthur, Murray"},
                {"characters": ["Murray"], "visual": "Murray on stage"},
            ]),
            _beat("Arthur again.", [{"characters": ["Arthur"]}]),
        ]
        out = characters.needed(beats)
        names = [c["name"] for c in out["main"]]
        self.assertEqual(names[0], "Arthur")
        self.assertIn("Murray", names)
        self.assertEqual(out["photos_each"], characters.PHOTOS_WANTED)

    def test_a_stringified_list_field_is_understood(self):
        """Visual scripts have carried this as "['Arthur', 'Murray']"."""
        beats = [_beat("", [{"characters": "['Arthur', 'Murray']"}])]
        names = [c["name"] for c in characters.needed(beats)["main"]]
        self.assertIn("Arthur", names)
        self.assertIn("Murray", names)

    def test_crowd_nouns_are_never_offered_as_people(self):
        beats = [_beat("", [
            {"characters": ["Arthur", "the kids", "a crowd", "people"]},
        ])]
        names = [c["name"] for c in characters.needed(beats)["main"]]
        self.assertEqual(names, ["Arthur"])

    def test_a_name_only_in_the_caption_is_still_found(self):
        """A shot that lists nobody but whose caption names them counts."""
        beats = [
            _beat("", [{"characters": ["Gus"]}]),
            _beat("", [{"visual": "Gus stands over Victor in the lab"}]),
        ]
        # Victor is not in any characters field, so he is not minted from
        # free text; only names already known get counted in prose.
        names = [c["name"] for c in characters.needed(beats)["main"]]
        self.assertIn("Gus", names)

    def test_a_bit_part_is_dropped_below_the_minor_threshold(self):
        shots = [{"characters": ["Arthur"]} for _ in range(30)]
        shots.append({"characters": ["Randall"]})    # one glance, once
        beats = [_beat("Arthur " * 30, shots)]
        names = [c["name"] for c in characters.needed(beats)["main"]]
        self.assertIn("Arthur", names)
        self.assertNotIn("Randall", names)

    def test_no_characters_anywhere_is_an_empty_answer_not_a_crash(self):
        out = characters.needed([_beat("A quiet landscape.", [{"visual": "hills"}])])
        self.assertEqual(out["main"], [])
        self.assertEqual(out["count"], 0)


if __name__ == "__main__":
    unittest.main()
