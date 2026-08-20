"""Tests for the contact sheet — the review step.

A twenty-minute essay needs well over a hundred stills. Judging them one file
at a time is the manual checking this tool exists to end, and it is why bad
footage kept reaching finished videos: nobody opens a hundred folders, so
nobody looked at all of them.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from media_index import contact, probe                              # noqa: E402

HAVE_FFMPEG = probe.ffmpeg_bin() is not None
skip_no_ffmpeg = unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")


def fake_job(root: str, scenes: int, per_scene: int) -> int:
    made = 0
    for s in range(1, scenes + 1):
        d = os.path.join(root, f"scene_{s:03d}")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "scene.txt"), "w") as f:
            f.write("narration")           # must be ignored by the collector
        for k in range(1, per_scene + 1):
            subprocess.run(
                [probe.ffmpeg_bin(), "-y", "-v", "error", "-f", "lavfi",
                 "-i", f"color=c=0x{(s * 37) % 256:02x}"
                       f"{(k * 61) % 256:02x}a0:s=640x360:d=0.1",
                 "-frames:v", "1", os.path.join(d, f"image_{k:02d}_1.jpg")],
                check=True)
            made += 1
    return made


@skip_no_ffmpeg
class TestContactSheet(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sheet_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.job = os.path.join(self.tmp, "built")
        self.n = fake_job(self.job, scenes=5, per_scene=4)

    def test_collects_only_images_and_keeps_scene_order(self):
        got = contact.collect(self.job)
        self.assertEqual(len(got), self.n)
        self.assertEqual([s for s, _p in got[:4]], ["scene_001"] * 4)
        self.assertTrue(all(p.lower().endswith(".jpg") for _s, p in got))

    def test_one_page_for_a_small_job(self):
        out = os.path.join(self.tmp, "sheet.jpg")
        made = contact.build(self.job, out, columns=8)
        self.assertEqual(made, out)
        self.assertGreater(os.path.getsize(out), 1000)

    def test_a_partial_last_row_still_renders(self):
        """20 images into a grid of 8 leaves a short final row, and an
        incomplete grid is exactly where a tiling filter tends to give up."""
        out = os.path.join(self.tmp, "partial.jpg")
        self.assertEqual(self.n % 8, 4)
        contact.build(self.job, out, columns=8)
        self.assertTrue(os.path.isfile(out))

    def test_the_index_names_every_tile(self):
        """No caption can be burned in without drawtext, so the mapping back
        to a scene folder has to live somewhere — otherwise a wrong tile is
        spotted and cannot be traced."""
        out = os.path.join(self.tmp, "sheet.jpg")
        contact.build(self.job, out, columns=8)
        with open(os.path.join(self.tmp, "sheet_index.txt"),
                  encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text.count("scene_"), self.n)
        self.assertIn("row  1 col  1", text)
        self.assertIn("scene_005/image_04_1.jpg", text)

    def test_a_big_job_is_split_into_pages(self):
        big = os.path.join(self.tmp, "big")
        fake_job(big, scenes=20, per_scene=6)          # 120 stills
        out = os.path.join(self.tmp, "big.jpg")
        first = contact.build(big, out, columns=8)
        self.assertEqual(first, os.path.join(self.tmp, "big_1.jpg"))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "big_2.jpg")))

    def test_an_empty_job_says_so_instead_of_failing(self):
        self.assertIsNone(contact.build(os.path.join(self.tmp, "nothing"),
                                        os.path.join(self.tmp, "x.jpg")))
