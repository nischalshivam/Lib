"""Tests for pulling many good stills out of a scene.

A twenty-minute video at a 50-60% image split needs well over a hundred
stills. They cannot come from a script listing them one by one, so they come
from the footage — which makes "which frames" the whole problem.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from media_index import cutter, frames, probe                  # noqa: E402
from media_index.demo import make_demo_video as dv             # noqa: E402

HAVE_FFMPEG = probe.ffmpeg_bin() is not None


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class TestFrameScan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="frames_")
        cls.vid = dv.build(os.path.join(cls.tmp, "v.mkv"), log=lambda *a: None)
        cls.cands = frames.scan(cls.vid)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _segment_at(self, t):
        got = cutter.average_rgb(self.vid, t)
        return min(range(dv.n_segments()),
                   key=lambda k: sum(abs(a - b) for a, b in
                                     zip(got, dv.segment_color(k)[2])))

    def test_scan_covers_the_whole_file(self):
        self.assertAlmostEqual(len(self.cands),
                               dv.DURATION * frames.SCAN_FPS, delta=4)
        self.assertLess(self.cands[0].time, 1.0)
        self.assertGreater(self.cands[-1].time, dv.DURATION - 2)

    def test_scan_respects_a_range(self):
        part = frames.scan(self.vid, 30.0, 45.0)
        self.assertAlmostEqual(len(part), 15 * frames.SCAN_FPS, delta=3)
        self.assertGreaterEqual(part[0].time, 29.5)
        self.assertLessEqual(part[-1].time, 46.0)

    def test_every_candidate_carries_a_colour_signature(self):
        self.assertTrue(all(len(c.colour) == 48 for c in self.cands))

    def test_duplicates_are_not_returned(self):
        """The demo holds 8 distinct pictures; asking for 20 must not pad the
        answer with copies of the same shot."""
        picked = frames.pick(self.cands, 20)
        segs = [self._segment_at(c.time) for c in picked]
        self.assertEqual(len(segs), len(set(segs)))

    def test_all_distinct_shots_are_found(self):
        picked = frames.pick(self.cands, 20)
        segs = {self._segment_at(c.time) for c in picked}
        self.assertEqual(segs, set(range(dv.n_segments())))

    def test_asking_for_fewer_returns_fewer(self):
        self.assertEqual(len(frames.pick(self.cands, 3)), 3)

    def test_results_are_in_time_order(self):
        picked = frames.pick(self.cands, 8)
        self.assertEqual([c.time for c in picked],
                         sorted(c.time for c in picked))

    def test_colour_is_required_to_call_two_frames_duplicates(self):
        """A luminance hash alone cannot tell a red room from a green one of
        the same brightness — which collapsed eight shots down to two."""
        a, b = self.cands[10], self.cands[10]
        self.assertLess(frames._colour_distance(a.colour, b.colour), 1.0)
        far = [c for c in self.cands
               if frames._colour_distance(c.colour, a.colour) > 20]
        self.assertTrue(far, "no frame differed in colour — check the signature")

    def test_black_frames_are_rejected(self):
        black = frames.Candidate(time=1.0, sharpness=99.0, brightness=2.0,
                                 phash=0, colour=(0.0,) * 48)
        self.assertFalse(black.usable)
        self.assertEqual(black.score, 0.0)

    def test_blown_out_frames_are_rejected(self):
        white = frames.Candidate(time=1.0, sharpness=99.0, brightness=252.0,
                                 phash=0, colour=(255.0,) * 48)
        self.assertFalse(white.usable)

    def test_extract_writes_real_images(self):
        out = os.path.join(self.tmp, "stills")
        paths = frames.extract_stills(self.vid, out, 6, width=640)
        self.assertEqual(len(paths), 6)
        for p in paths:
            self.assertGreater(os.path.getsize(p), 800)

    def test_extract_from_a_range_only(self):
        out = os.path.join(self.tmp, "stills_range")
        paths = frames.extract_stills(self.vid, out, 3, start=0.0, end=15.0,
                                      prefix="seg0", width=320)
        self.assertTrue(paths)
        for p in paths:
            self.assertIn("seg0", os.path.basename(p))

    def test_describe_reports_what_happened(self):
        text = frames.describe(self.cands, frames.pick(self.cands, 8))
        self.assertIn("candidates", text)
        self.assertIn("distinct", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
