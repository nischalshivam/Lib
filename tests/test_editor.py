"""Tests for changing a built video by hand.

The editor exists because the model cannot tell one dim interior from
another and never will. What it must never do is make things worse: an edit
that empties a scene brings back the thirty-second still that cost builds
eleven and twelve, and an edit that leaves start times disagreeing with
durations makes every number on screen a lie.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from media_index import editor                            # noqa: E402


def _timeline(scenes):
    return {"video": "t", "total_seconds": 0.0, "scenes": scenes}


class _Built(unittest.TestCase):
    """A folder shaped like one a build writes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="edit_")
        self.out = os.path.join(self.tmp, "Out")
        os.makedirs(os.path.join(self.out, "segments"))
        os.makedirs(os.path.join(self.out, "scene_001"))
        for n in range(1, 6):
            with open(os.path.join(self.out, "segments",
                                   f"seg_{n:04d}.mp4"), "wb") as f:
                f.write(b"x" * 4096)
        open(os.path.join(self.out, "video.mp4"), "wb").close()
        self.write([
            {"scene": 1, "narration": "one", "start": 0.0, "end": 6.0,
             "items": [
                 {"file": "a.mp4", "kind": "video", "start": 0.0,
                  "duration": 3.0, "placed_by": "anchor",
                  "source": "Ep1.mkv", "source_start": 100.0},
                 {"file": "b.jpg", "kind": "image", "start": 3.0,
                  "duration": 3.0, "placed_by": "filler",
                  "source": "Ep1.mkv", "source_start": 200.0}]},
            {"scene": 2, "narration": "two", "start": 6.0, "end": 9.0,
             "items": [
                 {"file": "c.jpg", "kind": "image", "start": 6.0,
                  "duration": 3.0, "placed_by": "interpolated",
                  "source": "Ep1.mkv", "source_start": 300.0}]}])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, scenes):
        with open(os.path.join(self.out, "timeline.json"), "w") as f:
            json.dump(_timeline(scenes), f)

    def read(self):
        with open(os.path.join(self.out, "timeline.json")) as f:
            return json.load(f)


class TestChangingHowLongAShotHolds(_Built):
    def test_the_whole_video_is_laid_out_again(self):
        """Start times that do not follow durations render one video and
        report another — every number the editor shows would be wrong."""
        editor.set_duration(self.out, 1, "a.mp4", 5.0)
        tl = self.read()
        self.assertEqual(tl["scenes"][0]["items"][0]["duration"], 5.0)
        self.assertEqual(tl["scenes"][0]["items"][1]["start"], 5.0)
        self.assertEqual(tl["scenes"][1]["items"][0]["start"], 8.0)
        self.assertEqual(tl["total_seconds"], 11.0)
        self.assertEqual(tl["scenes"][1]["start"], 8.0)

    def test_a_shot_cannot_be_shortened_to_nothing(self):
        editor.set_duration(self.out, 1, "a.mp4", 0.0)
        self.assertGreater(self.read()["scenes"][0]["items"][0]["duration"], 0)

    def test_everything_rendered_is_thrown_away(self):
        # Timings changed, so every segment is now the wrong length. Keeping
        # them would join old pieces to new ones.
        editor.set_duration(self.out, 1, "a.mp4", 5.0)
        self.assertFalse(os.path.isdir(os.path.join(self.out, "segments")))
        self.assertFalse(os.path.isfile(os.path.join(self.out, "video.mp4")))

    def test_a_shot_that_is_not_there_is_named(self):
        with self.assertRaises(editor.EditError):
            editor.set_duration(self.out, 1, "nope.mp4", 3.0)


class TestRemovingAShot(_Built):
    def test_its_seconds_go_to_the_rest_of_its_scene(self):
        editor.remove(self.out, 1, "a.mp4")
        tl = self.read()
        self.assertEqual(len(tl["scenes"][0]["items"]), 1)
        self.assertEqual(tl["scenes"][0]["items"][0]["duration"], 6.0)
        self.assertEqual(tl["total_seconds"], 9.0)   # the video is as long

    def test_the_last_shot_of_a_scene_cannot_be_removed(self):
        """An empty beat is a hole, and a hole becomes a still that sits for
        thirty seconds — the exact fault that cost builds eleven and twelve."""
        with self.assertRaises(editor.EditError) as caught:
            editor.remove(self.out, 2, "c.jpg")
        self.assertIn("only shot", str(caught.exception))


class TestSwappingAShot(_Built):
    def test_only_that_shot_s_segment_is_thrown_away(self):
        """A swap costs one segment to re-render. Throwing away all of them
        would make a two-click fix cost forty minutes."""
        editor._forget_segment(self.out, self.read(), 1, "b.jpg")
        work = os.path.join(self.out, "segments")
        left = sorted(os.listdir(work))
        self.assertNotIn("seg_0002.mp4", left)      # a.mp4 is 1, b.jpg is 2
        self.assertIn("seg_0001.mp4", left)
        self.assertIn("seg_0003.mp4", left)
        self.assertFalse(os.path.isfile(os.path.join(self.out, "video.mp4")))

    def test_a_segment_never_rendered_is_not_an_error(self):
        shutil.rmtree(os.path.join(self.out, "segments"))
        editor._forget_segment(self.out, self.read(), 1, "a.mp4")


class TestWhatTheHeaderShows(_Built):
    def test_shots_are_counted_by_how_they_got_there(self):
        got = editor.summary(self.out)
        self.assertEqual(got["shots"], 3)
        self.assertEqual(got["scenes"], 2)
        self.assertEqual(got["counts"],
                         {"anchor": 1, "filler": 1, "interpolated": 1})

    def test_a_folder_with_nothing_in_it_is_an_answer(self):
        self.assertEqual(editor.summary(self.tmp), {})


class TestFindingTheEpisodeAgain(_Built):
    def test_a_path_that_is_already_a_file_is_used_as_is(self):
        here = os.path.join(self.tmp, "Ep1.mkv")
        open(here, "wb").close()
        self.assertEqual(editor.episode_path("nope.db", here), here)

    def test_an_episode_that_is_not_in_the_library_is_empty_not_a_guess(self):
        self.assertEqual(editor.episode_path("nope.db", ""), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
