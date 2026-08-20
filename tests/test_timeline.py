"""Tests for how long each shot holds, and when.

Correct footage cut to a metronome still looks like a machine made it. Every
rule here exists to stop one of the two things a viewer notices first: the
same duration over and over, and four visuals crammed under one short line.
"""
from __future__ import annotations

import json
import os
import random
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from media_index import timeline                                # noqa: E402


def clip(name="clip_01.mp4", **kw):
    return {"file": name, "kind": "video", **kw}


def still(name="image_01_1.jpg", **kw):
    return {"file": name, "kind": "image", **kw}


class TestHowManyVisualsABeatCanHold(unittest.TestCase):
    """The count comes from the narration, never from the assets."""

    def test_a_ten_word_line_gets_one_visual(self):
        # About four seconds of speech. A clip and three stills under it is a
        # second each, which is what "footage shoved in to fill a hole" looks
        # like — because that is exactly what it is.
        self.assertEqual(timeline.segment_count(4.0, available=6), 1)

    def test_a_long_beat_gets_several(self):
        self.assertGreaterEqual(timeline.segment_count(24.0, available=8), 4)

    def test_never_more_visuals_than_there_are_assets(self):
        self.assertEqual(timeline.segment_count(60.0, available=3), 3)

    def test_nothing_ever_flashes_past(self):
        for budget in (3.0, 5.0, 9.0, 14.0, 30.0, 61.0):
            n = timeline.segment_count(budget, available=50)
            self.assertGreaterEqual(budget / n, timeline.MIN_ON_SCREEN_S,
                                    f"{n} visuals in {budget}s is too fast")

    def test_a_beat_with_no_time_gets_nothing(self):
        self.assertEqual(timeline.segment_count(0.0, available=5), 0)

    def test_a_beat_with_no_assets_gets_nothing(self):
        self.assertEqual(timeline.segment_count(20.0, available=0), 0)


class TestNothingIsAFixedLength(unittest.TestCase):
    """Forty clips of exactly 4.0 seconds is the signature of an automated
    edit — and it is what this tool produced, because `clip_seconds`
    defaulted to 4.0 and the cutter took `min(end, start + 4.0)`."""

    def _durations(self, kinds, budget, seed=1):
        rng = random.Random(seed)
        return timeline.vary(timeline.share_out(budget, kinds, rng), kinds, rng)

    def test_neighbours_are_never_the_same_length(self):
        kinds = ["video", "image"] * 6
        got = self._durations(kinds, 60.0)
        for a, b in zip(got, got[1:]):
            self.assertGreaterEqual(abs(a - b), timeline.SAME_LENGTH_S,
                                    f"{a} then {b} reads as one length")

    def test_a_whole_video_uses_many_different_lengths(self):
        lengths = set()
        for beat in range(30):
            kinds = ["video", "image", "video"]
            lengths.update(self._durations(kinds, 15.0, seed=beat))
        self.assertGreater(len(lengths), 20,
                           f"only {len(lengths)} distinct lengths in 90 shots")

    def test_the_same_seed_gives_the_same_timeline(self):
        # A review step is worthless if the thing reviewed changes when it is
        # rendered.
        kinds = ["video", "image", "video", "image"]
        self.assertEqual(self._durations(kinds, 22.0, seed=5),
                         self._durations(kinds, 22.0, seed=5))

    def test_different_beats_do_not_share_a_rhythm(self):
        kinds = ["video", "image", "video"]
        self.assertNotEqual(self._durations(kinds, 15.0, seed=1),
                            self._durations(kinds, 15.0, seed=2))


class TestTheTwoInstruments(unittest.TestCase):
    """A clip carries motion and tires past six seconds. A still holds."""

    def _durations(self, kinds, budget, seed=3):
        rng = random.Random(seed)
        return timeline.vary(timeline.share_out(budget, kinds, rng), kinds, rng)

    def test_no_clip_ever_runs_past_six_seconds(self):
        for seed in range(40):
            for budget in (8.0, 20.0, 45.0):
                kinds = ["video"] * 3
                for d in self._durations(kinds, budget, seed=seed):
                    self.assertLessEqual(d, timeline.MAX_CLIP_S + 0.01,
                                         f"{d}s clip")

    def test_a_still_may_hold_much_longer_than_a_clip(self):
        got = self._durations(["image"], 11.0)
        self.assertGreater(got[0], timeline.MAX_CLIP_S)
        self.assertLessEqual(got[0], timeline.MAX_STILL_S + 0.01)

    def test_a_still_never_outstays_twelve_seconds(self):
        for seed in range(30):
            for d in self._durations(["image"] * 2, 40.0, seed=seed):
                self.assertLessEqual(d, timeline.MAX_STILL_S + 0.01)

    def test_clip_overflow_is_given_to_the_stills(self):
        # Three visuals over 24 seconds is 8 each, and a clip cannot take 8.
        # If the overflow is dropped instead of moved the narration runs on
        # over a frozen frame.
        kinds = ["video", "image", "video"]
        got = self._durations(kinds, 24.0)
        self.assertAlmostEqual(sum(got), 24.0, delta=0.4)
        self.assertGreater(got[1], got[0])

    def test_a_beat_that_cannot_be_filled_says_so_instead_of_stretching(self):
        # Two clips, capped at six each, cannot cover forty seconds.
        scene = timeline.lay_out(1, "n", 0.0, 40.0,
                                 [clip("a.mp4"), clip("b.mp4")])
        self.assertGreater(scene.gap, 1.0)
        self.assertIn("no footage", scene.note)


class TestChoosingWhatGoesWhere(unittest.TestCase):
    def test_motion_and_stillness_alternate(self):
        picked = timeline.choose([clip("c1.mp4"), clip("c2.mp4"),
                                  still("s1.jpg"), still("s2.jpg")], 4)
        kinds = [p["kind"] for p in picked]
        for a, b in zip(kinds, kinds[1:]):
            self.assertNotEqual(a, b, f"{kinds} is a run of one kind")

    def test_a_beat_of_mostly_stills_does_not_stop_moving_after_one_clip(self):
        assets = [clip("c1.mp4")] + [still(f"s{i}.jpg") for i in range(5)]
        kinds = [p["kind"] for p in timeline.choose(assets, 4)]
        self.assertEqual(kinds[0], "image")

    def test_asking_for_more_than_exists_returns_what_exists(self):
        self.assertEqual(len(timeline.choose([clip(), still()], 9)), 2)

    def test_nothing_is_used_twice(self):
        assets = [clip("c1.mp4"), still("s1.jpg"), still("s2.jpg")]
        picked = timeline.choose(assets, 3)
        self.assertEqual(len({p["file"] for p in picked}), 3)


class TestWhenEachBeatHappens(unittest.TestCase):
    def test_beats_run_back_to_back_with_no_holes(self):
        beats = [{"narration_seconds": 8}, {"narration_seconds": 12},
                 {"narration_seconds": 5}]
        spans = timeline.boundaries(beats)
        self.assertEqual(spans[0][0], 0.0)
        for (_, end), (start, _) in zip(spans, spans[1:]):
            self.assertAlmostEqual(end, start, places=6)

    def test_the_plan_is_stretched_onto_the_real_voiceover(self):
        # The script estimates at 150 words a minute. A real read is rarely
        # that, and an eight-minute script over a nine-minute recording puts
        # every visual in the last third under the wrong sentence.
        beats = [{"narration_seconds": 10}, {"narration_seconds": 10}]
        spans = timeline.boundaries(beats, total_seconds=30.0)
        self.assertAlmostEqual(spans[-1][1], 30.0, places=3)
        self.assertAlmostEqual(spans[0][1], 15.0, places=3)

    def test_a_beat_with_no_stated_length_is_counted_by_words(self):
        beats = [{"narration": " ".join(["word"] * 150)}]
        self.assertAlmostEqual(timeline.boundaries(beats)[0][1], 60.0, delta=1)

    def test_a_beat_with_nothing_at_all_still_gets_time(self):
        self.assertGreater(timeline.boundaries([{}])[0][1], 0.0)


class TestLayingOutOneBeat(unittest.TestCase):
    def test_visuals_run_back_to_back_from_the_beat_start(self):
        scene = timeline.lay_out(3, "n", 10.0, 30.0,
                                 [clip("c1.mp4"), still("s1.jpg"),
                                  clip("c2.mp4"), still("s2.jpg")])
        self.assertEqual(scene.items[0].start, 10.0)
        for a, b in zip(scene.items, scene.items[1:]):
            self.assertAlmostEqual(a.end, b.start, places=2)

    def test_the_beat_is_covered(self):
        scene = timeline.lay_out(1, "n", 0.0, 20.0,
                                 [clip("c1.mp4"), still("s1.jpg"),
                                  clip("c2.mp4"), still("s2.jpg")])
        self.assertAlmostEqual(scene.covered, 20.0, delta=0.4)
        self.assertEqual(scene.note, "")

    def test_where_each_asset_came_from_travels_with_it(self):
        scene = timeline.lay_out(1, "n", 0.0, 10.0, [
            clip("c1.mp4", source="Breaking Bad S04E01.mp4",
                 source_start=2013.4, placed_by="verified")])
        self.assertEqual(scene.items[0].source_start, 2013.4)
        self.assertEqual(scene.items[0].placed_by, "verified")

    def test_a_beat_with_no_assets_is_reported_not_dropped(self):
        scene = timeline.lay_out(7, "n", 0.0, 12.0, [])
        self.assertEqual(scene.items, [])
        self.assertIn("nothing to show", scene.note)


class TestAWholeVideo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tl_")
        self.beats = [{"beat": i, "narration_seconds": s,
                       "narration": f"beat {i}"}
                      for i, s in enumerate([9, 18, 4, 25, 12], 1)]
        # Six assets a beat. Four was not enough and the failure was real
        # rather than a fixture mistake: a 44-second beat with two clips and
        # two stills tops out at 36 seconds, because a clip may not exceed
        # six. The tool reported the shortfall instead of stretching a shot,
        # which is the right answer — a script whose long beats carry three
        # shots cannot fill them, and that is a fact about the script.
        self.manifest = {"video": "Gus", "scenes": [
            {"scene": i, "narration": f"beat {i}", "source": "BB S04E01.mp4",
             "confidence": "high", "assets": [
                 clip(f"clip_{i:02d}.mp4", source_start=100.0 * i,
                      placed_by="verified"),
                 still(f"image_{i:02d}_1.jpg", source_start=100.0 * i + 2),
                 still(f"image_{i:02d}_2.jpg", source_start=100.0 * i + 4),
                 clip(f"clip_{i:02d}b.mp4", source_start=100.0 * i + 6),
                 still(f"image_{i:02d}_3.jpg", source_start=100.0 * i + 8),
                 clip(f"clip_{i:02d}c.mp4", source_start=100.0 * i + 10)]}
            for i in range(1, 6)]}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_timeline_covers_the_voiceover_end_to_end(self):
        tl = timeline.plan(self.beats, self.manifest, total_seconds=120.0)
        self.assertAlmostEqual(tl.scenes[-1].end, 120.0, places=2)
        self.assertEqual(tl.uncovered(), [])

    def test_the_short_beat_gets_one_visual_and_the_long_one_gets_more(self):
        tl = timeline.plan(self.beats, self.manifest)
        short = next(s for s in tl.scenes if s.index == 3)     # 4 seconds
        long = next(s for s in tl.scenes if s.index == 4)      # 25 seconds
        self.assertEqual(len(short.items), 1)
        self.assertGreater(len(long.items), len(short.items))

    def test_stills_take_a_little_more_of_the_screen_than_clips(self):
        tl = timeline.plan(self.beats, self.manifest, total_seconds=120.0)
        self.assertGreater(tl.still_share, 0.45)
        self.assertLess(tl.still_share, 0.75)

    def test_the_cutting_rate_lands_where_a_real_essay_sits(self):
        # Competitor videos measured 4 to 63 cuts a minute, averaging 31.5.
        tl = timeline.plan(self.beats, self.manifest, total_seconds=120.0)
        self.assertGreater(tl.cuts_per_minute, 4)
        self.assertLess(tl.cuts_per_minute, 63)

    def test_a_scene_missing_from_the_manifest_is_a_gap_not_a_crash(self):
        manifest = {"video": "Gus", "scenes": self.manifest["scenes"][:2]}
        tl = timeline.plan(self.beats, manifest, total_seconds=100.0)
        self.assertEqual(len(tl.scenes), 5)
        self.assertTrue(tl.uncovered())

    def test_it_writes_and_reads_back_as_json(self):
        tl = timeline.plan(self.beats, self.manifest, total_seconds=120.0,
                           audio="narration.mp3")
        path = timeline.write(tl, self.tmp)
        with open(path, encoding="utf-8") as f:
            got = json.load(f)
        self.assertEqual(got["audio"], "narration.mp3")
        self.assertEqual(len(got["scenes"]), 5)
        first = got["scenes"][0]["items"][0]
        for key in ("file", "kind", "start", "duration", "source_start",
                    "placed_by"):
            self.assertIn(key, first)

    def test_rebuilding_produces_an_identical_timeline(self):
        a = timeline.to_dict(timeline.plan(self.beats, self.manifest, 120.0))
        b = timeline.to_dict(timeline.plan(self.beats, self.manifest, 120.0))
        a.pop("generated_at"), b.pop("generated_at")
        self.assertEqual(a, b)

    def test_the_summary_reports_what_an_editor_would_check(self):
        tl = timeline.plan(self.beats, self.manifest, total_seconds=120.0)
        text = tl.summary()
        for word in ("cuts/min", "stills", "distinct lengths"):
            self.assertIn(word, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
