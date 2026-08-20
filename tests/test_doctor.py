"""Tests for the pre-index folder check.

This is the command someone runs right after a download finishes, so its job
is to give a correct verdict on a folder it has never seen — and above all to
name the fix, not just the problem.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from media_index import doctor, probe                            # noqa: E402
from media_index.demo import make_demo_video as dv               # noqa: E402
from media_index.demo.make_demo_library import srt               # noqa: E402

HAVE_FFMPEG = probe.ffmpeg_bin() is not None


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class TestDoctor(unittest.TestCase):
    """A folder shaped exactly like a freshly downloaded season, with one
    file per real-world subtitle situation."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="doctor_")
        cls.root = os.path.join(cls.tmp, "Breaking Bad Season 2")
        os.makedirs(cls.root, exist_ok=True)

        base = dv.build(os.path.join(cls.tmp, "_base.mkv"), write_srt=False,
                        log=lambda *a: None)
        en = os.path.join(cls.tmp, "en.srt")
        with open(en, "w", encoding="utf-8") as f:
            f.write(srt(dv.CUES))
        hi = os.path.join(cls.tmp, "hi.srt")
        with open(hi, "w", encoding="utf-8") as f:
            f.write(srt([(a, b, "मैंने कभी फ़सल नहीं चाही थी")
                         for a, b, _ in dv.CUES]))

        def mux(episode, args):
            out = os.path.join(cls.root,
                               f"Breaking Bad Season 2 Episode {episode}.mkv")
            subprocess.run([probe.require_ffmpeg(), "-y", "-loglevel", "error"]
                           + args + [out], check=True)
            return out

        # 1: embedded English subtitles — the healthy case
        mux(1, ["-i", base, "-i", en, "-map", "0", "-map", "1",
                "-c", "copy", "-c:s", "srt", "-metadata:s:s:0", "language=eng"])
        # 2: Hindi audio first, Hindi subtitles only
        mux(2, ["-i", base, "-i", base, "-i", hi,
                "-map", "0:v", "-map", "1:a", "-map", "0:a", "-map", "2",
                "-c", "copy", "-c:s", "srt",
                "-metadata:s:a:0", "language=hin",
                "-metadata:s:a:1", "language=eng",
                "-metadata:s:s:0", "language=hin"])
        # 3: no subtitles at all
        mux(3, ["-i", base, "-map", "0", "-c", "copy"])
        # 4: sidecar .srt beside the file
        v = mux(4, ["-i", base, "-map", "0", "-c", "copy"])
        shutil.copy(en, os.path.splitext(v)[0] + ".en.srt")
        # 5: not a video at all
        with open(os.path.join(cls.root,
                               "Breaking Bad Season 2 Episode 5.mkv"), "wb") as f:
            f.write(b"\x00" * 400_000)

        cls.reports = {r.label: r for r in doctor.inspect_folder(cls.root)}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_every_file_is_identified(self):
        self.assertEqual(len(self.reports), 5)
        for e in range(1, 6):
            self.assertIn(f"Breaking Bad S02E{e:02d}", self.reports)

    def test_embedded_english_is_ready(self):
        r = self.reports["Breaking Bad S02E01"]
        self.assertEqual(r.verdict, doctor.VERDICT_OK)
        self.assertEqual(r.sub_source, "embedded")
        self.assertGreater(r.cue_count, 0)

    def test_hindi_subtitles_are_flagged_with_a_fix(self):
        r = self.reports["Breaking Bad S02E02"]
        self.assertEqual(r.verdict, doctor.VERDICT_NEEDS_ENGLISH)
        self.assertEqual(r.sub_script, "devanagari")
        self.assertIn(".en.srt", r.fix)

    def test_no_subtitles_names_the_file_to_download(self):
        r = self.reports["Breaking Bad S02E03"]
        self.assertEqual(r.verdict, doctor.VERDICT_NEEDS_SUBS)
        self.assertIn("Breaking Bad Season 2 Episode 3.en.srt", r.fix)

    def test_sidecar_is_found(self):
        r = self.reports["Breaking Bad S02E04"]
        self.assertEqual(r.verdict, doctor.VERDICT_OK)
        self.assertEqual(r.sub_source, "sidecar")

    def test_corrupt_file_is_unreadable_not_a_crash(self):
        r = self.reports["Breaking Bad S02E05"]
        self.assertEqual(r.verdict, doctor.VERDICT_UNREADABLE)
        self.assertTrue(r.problem)

    def test_single_audio_track_is_not_warned_about(self):
        """Only a real choice deserves a warning; noise buries the real ones."""
        self.assertFalse(self.reports["Breaking Bad S02E01"].problem)

    def test_multiple_audio_tracks_are_reported(self):
        r = self.reports["Breaking Bad S02E02"]
        self.assertEqual(len(r.audio), 2)

    def test_report_names_the_action(self):
        text = doctor.format_report(list(self.reports.values()), self.root)
        self.assertIn("ready", text)
        self.assertIn(".en.srt", text)

    def test_empty_folder_is_explained(self):
        empty = os.path.join(self.tmp, "nothing")
        os.makedirs(empty, exist_ok=True)
        text = doctor.format_report(doctor.inspect_folder(empty), empty)
        self.assertIn("no video files", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
