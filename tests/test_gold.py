"""The benchmark that makes every other number honest.

These tests are about arithmetic and format, because that is all the module
is: it must turn a manifest into a sheet a person can fill, read that sheet
back through the mangling of a text editor, and count the verdicts the one
correct way — a declined scene (`none`) never held against precision, a
`wrong` never hidden inside a healthy total.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from media_index import gold                               # noqa: E402


MANIFEST = {
    "scenes": [
        {"scene": 1, "narration": "Hank is the loud one.",
         "assets": [{"kind": "video", "placed_by": "anchor", "tier": "A",
                     "source": "S01E01.mp4", "source_start": 700.0}]},
        {"scene": 2, "narration": "Victor starts cooking.",
         "assets": [{"kind": "image", "placed_by": "interpolated", "tier": "B",
                     "source": "S04E01.mp4", "source_start": 1980.0}]},
        {"scene": 3, "narration": "A silent moment.",
         "assets": [{"kind": "image", "placed_by": "needs_visual", "tier": "C",
                     "source": "S04E01.mp4", "source_start": None}]},
    ]
}


class TestBuildingTheSheet(unittest.TestCase):

    def test_one_row_per_scene_with_a_stable_id(self):
        rows = gold.rows_from_manifest(MANIFEST)
        self.assertEqual([r.request_id for r in rows],
                         ["beat_001", "beat_002", "beat_003"])

    def test_the_row_summarises_what_the_tool_placed(self):
        rows = gold.rows_from_manifest(MANIFEST)
        self.assertIn("S01E01.mp4", rows[0].placed)
        self.assertIn("11:40", rows[0].placed)          # 700s
        self.assertEqual(rows[0].method, "anchor")

    def test_a_card_scene_is_shown_as_needs_visual(self):
        rows = gold.rows_from_manifest(MANIFEST)
        self.assertIn("NEEDS VISUAL", rows[2].placed)
        self.assertEqual(rows[2].method, "needs_visual")

    def test_the_sheet_round_trips_through_csv(self):
        rows = gold.rows_from_manifest(MANIFEST)
        text = gold.write_template(rows)
        back = gold.read_labels(text)
        self.assertEqual([r.request_id for r in back],
                         [r.request_id for r in rows])
        self.assertEqual(back[0].method, "anchor")


class TestReadingVerdicts(unittest.TestCase):

    def test_forgiving_of_how_a_person_types(self):
        self.assertEqual(gold.normalise("Exact"), "exact")
        self.assertEqual(gold.normalise(" OK "), "ok")
        self.assertEqual(gold.normalise("bad"), "wrong")
        self.assertEqual(gold.normalise("card"), "none")
        self.assertEqual(gold.normalise("???"), "")

    def test_an_empty_verdict_is_simply_unlabelled(self):
        self.assertEqual(gold.normalise(""), "")


class TestTheOnlyNumbersThatMatter(unittest.TestCase):

    def _score(self, verdicts):
        rows = gold.rows_from_manifest(MANIFEST)
        # extend with more rows if the test wants them
        while len(rows) < len(verdicts):
            n = len(rows) + 1
            rows.append(gold.Row(request_id=gold.request_id(n), scene=n,
                                 narration="x", placed="y",
                                 method="interpolated", tier="B"))
        for r, v in zip(rows, verdicts):
            r.verdict = gold.normalise(v)
        return gold.score(rows)

    def test_precision_is_over_what_was_placed_not_over_everything(self):
        # exact, wrong, none  ->  placed=2, usable=1  -> 50%
        s = self._score(["exact", "wrong", "none"])
        self.assertEqual(s.placed, 2)
        self.assertAlmostEqual(s.precision, 0.5)
        self.assertAlmostEqual(s.exact_precision, 0.5)

    def test_a_declined_scene_never_counts_against_precision(self):
        # exact, none, none  ->  placed=1, usable=1 -> 100% precision
        s = self._score(["exact", "none", "none"])
        self.assertAlmostEqual(s.precision, 1.0)
        self.assertAlmostEqual(s.coverage, 1 / 3)       # only 1 of 3 filled

    def test_ok_counts_as_usable_wrong_does_not(self):
        s = self._score(["exact", "ok", "wrong"])
        self.assertAlmostEqual(s.precision, 2 / 3)
        self.assertAlmostEqual(s.exact_precision, 1 / 3)

    def test_unlabelled_rows_are_ignored_not_counted_wrong(self):
        s = self._score(["exact", "", ""])
        self.assertEqual(s.labelled, 1)
        self.assertEqual(s.placed, 1)

    def test_a_wrong_tier_b_cannot_hide_in_the_total(self):
        # two interpolated, one right one wrong: the method breakdown shows it
        s = self._score(["exact", "wrong"])   # scene1 anchor, scene2 interp
        anchor = s.by_method.get("anchor")
        interp = s.by_method.get("interpolated")
        self.assertEqual(anchor, (1, 1))       # anchor: 1/1 usable
        self.assertEqual(interp, (0, 1))       # interpolated: 0/1 usable

    def test_an_empty_sheet_says_so_rather_than_dividing_by_zero(self):
        s = self._score(["", "", ""])
        self.assertEqual(s.precision, 0.0)
        self.assertIn("koi verdict nahi", s.summary())


if __name__ == "__main__":
    unittest.main()
