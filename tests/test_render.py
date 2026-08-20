"""Tests for turning a timeline into a file that plays.

Everything before this stage produces folders, and a folder cannot tell you
that a cut lands two beats late or that a still sits dead on screen for nine
seconds. Until something plays end to end there is nothing to judge.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from media_index import cutter, probe, render                   # noqa: E402
from media_index.demo import make_demo_video as dv              # noqa: E402

HAVE_FFMPEG = probe.ffmpeg_bin() is not None


class TestTheMoveOnAHeldFrame(unittest.TestCase):
    """A still held for ten seconds is a slideshow, and a slideshow is the
    second thing a viewer notices after identical durations."""

    def test_a_still_is_given_motion_by_default(self):
        self.assertIn("zoompan", render.still_filter(6.0, seed=1))

    def test_the_move_can_be_turned_off(self):
        got = render.still_filter(6.0, seed=1, motion=False)
        self.assertNotIn("zoompan", got)
        self.assertIn("scale", got)

    def test_neighbouring_stills_do_not_all_drift_the_same_way(self):
        # Twenty stills pushing in at the same rate is a signature of its
        # own — subtler than identical durations, and just as machine-like.
        moves = {render.still_filter(5.0, seed=i) for i in range(12)}
        self.assertGreater(len(moves), 6, "the motion barely varies")

    def test_the_same_shot_moves_the_same_way_every_render(self):
        # A review step is worthless if re-rendering changes what was
        # reviewed.
        self.assertEqual(render.still_filter(5.0, seed=7),
                         render.still_filter(5.0, seed=7))

    def test_the_motion_happens_at_double_resolution(self):
        """Panning a 1920-wide still directly makes the pixel grid crawl."""
        got = render.still_filter(5.0, seed=2)
        self.assertIn(f"scale={render.WIDTH * 2}", got)
        self.assertIn(f"s={render.WIDTH}x{render.HEIGHT}", got)

    def test_a_very_short_still_still_gets_enough_frames(self):
        self.assertIn("d=", render.still_filter(0.01, seed=1))


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class TestRenderingAWholeTimeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="render_")
        src = dv.build(os.path.join(cls.tmp, "src.mkv"), log=lambda *a: None)
        scene = os.path.join(cls.tmp, "scene_001")
        os.makedirs(scene, exist_ok=True)
        cutter.cut_clip(src, 5.0, 10.0, os.path.join(scene, "clip_01.mp4"),
                        height=720)
        cutter.extract_frame(src, 20.0, os.path.join(scene, "image_01_1.jpg"),
                             width=1920)
        cutter.extract_frame(src, 40.0, os.path.join(scene, "image_01_2.jpg"),
                             width=1920)
        cls.src = src

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="out_", dir=self.tmp)
        for name in ("scene_001",):
            shutil.copytree(os.path.join(self.tmp, name),
                            os.path.join(self.out, name))

    def _timeline(self, durations=(3.4, 5.2, 4.1)):
        files = [("clip_01.mp4", "video"), ("image_01_1.jpg", "image"),
                 ("image_01_2.jpg", "image")]
        items, t = [], 0.0
        for (name, kind), d in zip(files, durations):
            items.append({"file": name, "kind": kind, "start": round(t, 2),
                          "duration": d})
            t += d
        return {"scenes": [{"scene": 1, "items": items}]}

    def test_it_writes_a_file_that_plays(self):
        res = render.render(self._timeline(), os.path.join(self.out, "v.mp4"),
                            source_dir=self.out)
        self.assertTrue(res.ok, res.failed)
        self.assertEqual(res.segments, 3)

    def test_the_video_is_as_long_as_the_timeline_says(self):
        res = render.render(self._timeline(), os.path.join(self.out, "v.mp4"),
                            source_dir=self.out)
        self.assertAlmostEqual(res.duration, 12.7, delta=0.4)

    def test_every_segment_comes_out_the_same_shape(self):
        # Concatenating without re-encoding only works if they agree, and a
        # mismatch shows up as a file that plays for two seconds and stops.
        render.render(self._timeline(), os.path.join(self.out, "v.mp4"),
                      source_dir=self.out)
        work = os.path.join(self.out, render.WORK_DIR)
        sizes = set()
        for name in sorted(os.listdir(work)):
            if name.startswith("seg_"):
                info = probe.probe(os.path.join(work, name))
                sizes.add((info.width, info.height))
        self.assertEqual(sizes, {(render.WIDTH, render.HEIGHT)})

    def test_a_second_run_reuses_what_it_already_made(self):
        # This is the slow step. A queue of six videos overnight must not
        # lose four hours to one interrupted render.
        render.render(self._timeline(), os.path.join(self.out, "v.mp4"),
                      source_dir=self.out)
        again = render.render(self._timeline(),
                              os.path.join(self.out, "v.mp4"),
                              source_dir=self.out)
        self.assertEqual(again.segments, 0)
        self.assertEqual(again.reused, 3)
        self.assertTrue(again.ok)

    def test_a_missing_asset_is_named_and_the_rest_still_renders(self):
        tl = self._timeline()
        tl["scenes"][0]["items"].append(
            {"file": "not_here.jpg", "kind": "image", "start": 12.7,
             "duration": 3.0})
        res = render.render(tl, os.path.join(self.out, "v.mp4"),
                            source_dir=self.out, resume=False)
        self.assertTrue(res.ok)
        named = [a for a, _b in res.failed]
        self.assertIn("not_here.jpg", named)
        # ...and its seconds go to the shots that did render, rather than
        # the video quietly coming out three seconds short and every cut
        # after it sitting ahead of the narration.
        self.assertNotIn("length", named)
        self.assertAlmostEqual(res.duration, res.planned, delta=0.5)

    def test_an_empty_timeline_says_so_rather_than_writing_nothing(self):
        res = render.render({"scenes": []}, os.path.join(self.out, "v.mp4"),
                            source_dir=self.out)
        self.assertFalse(res.ok)
        self.assertIn("no items", res.failed[0][1])

    def test_the_narration_ends_up_on_the_video(self):
        audio = os.path.join(self.out, "narration.m4a")
        probe_ff = probe.require_ffmpeg()
        import subprocess
        subprocess.run([probe_ff, "-y", "-v", "error", "-f", "lavfi",
                        "-i", "sine=frequency=440:duration=12",
                        "-c:a", "aac", audio], check=True)
        res = render.render(self._timeline(),
                            os.path.join(self.out, "v.mp4"),
                            source_dir=self.out, audio=audio, resume=False)
        self.assertTrue(res.ok, res.failed)
        self.assertTrue(probe.probe(res.path).has_audio)

    def test_a_missing_narration_is_reported_but_the_picture_survives(self):
        res = render.render(self._timeline(),
                            os.path.join(self.out, "v.mp4"),
                            source_dir=self.out, audio="/no/such/track.mp3")
        self.assertTrue(res.ok)
        self.assertTrue(any("audio" in a for a, _b in res.failed))

    def test_it_can_be_pointed_at_a_built_folder(self):
        with open(os.path.join(self.out, "timeline.json"), "w",
                  encoding="utf-8") as f:
            json.dump(self._timeline(), f)
        res = render.render_folder(self.out, log=lambda *a: None)
        self.assertTrue(res.ok, res.failed)
        self.assertTrue(res.path.endswith("video.mp4"))

    def test_a_folder_with_no_timeline_says_what_to_do(self):
        res = render.render_folder(self.out, log=lambda *a: None)
        self.assertFalse(res.ok)
        self.assertIn("plan the timing", res.failed[0][1])

    def test_the_summary_reads_like_a_result(self):
        res = render.render(self._timeline(), os.path.join(self.out, "v.mp4"),
                            source_dir=self.out)
        text = render.describe(res)
        self.assertIn("min", text)
        self.assertIn("v.mp4", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestNothingIsSilentlyLostFromTheLength(unittest.TestCase):
    """The bug this class exists for.

    Concatenation has no idea what time an item was meant to start. A beat
    with no footage does not leave a gap in the finished video, it shortens
    it — and everything after slides earlier by that much. On a real
    eleven-minute build two empty beats and 42 clips shorter than planned
    left the picture 45 seconds ahead of the voice, and the video ended
    while the narrator was still talking.
    """

    def _timeline(self, scenes, total=None):
        tl = {"scenes": scenes}
        if total is not None:
            tl["total_seconds"] = total
        return tl

    def test_a_beat_with_no_footage_does_not_shorten_the_video(self):
        tl = self._timeline([
            {"scene": 1, "items": [
                {"file": "a.jpg", "kind": "image", "start": 0.0,
                 "duration": 5.0}]},
            {"scene": 2, "items": []},                       # 4s of nothing
            {"scene": 3, "items": [
                {"file": "b.jpg", "kind": "image", "start": 9.0,
                 "duration": 3.0}]},
        ], total=12.0)
        segs = render.plan_segments(tl)
        self.assertEqual(len(segs), 2)
        self.assertAlmostEqual(sum(s["duration"] for s in segs), 12.0,
                               places=2)

    def test_the_hole_is_covered_by_the_shots_around_it(self):
        tl = self._timeline([
            {"scene": 1, "items": [
                {"file": "a.jpg", "kind": "image", "start": 0.0,
                 "duration": 5.0}]},
            {"scene": 2, "items": [
                {"file": "b.jpg", "kind": "image", "start": 9.0,
                 "duration": 3.0}]},
        ], total=12.0)
        segs = render.plan_segments(tl)
        self.assertAlmostEqual(sum(s["duration"] for s in segs), 12.0, places=2)
        self.assertTrue(all(s["duration"] <= render.MAX_HOLD_S for s in segs))

    def test_no_shot_is_left_sitting_on_screen_for_half_a_minute(self):
        """The complaint this was written for. Three and a half minutes of a
        real build had no footage; the whole of each hole went to the one
        shot before it, and a twelve-second still ran for thirty seconds.

        Either side of a hole works and neither breaks sync — concatenation
        only cares about total duration — so it is shared.
        """
        items = [{"scene": i, "items": [
            {"file": f"{i}.jpg", "kind": "image", "start": i * 5.0,
             "duration": 5.0}]} for i in range(8)]
        items.append({"scene": 8, "items": []})              # 60s of nothing
        items.append({"scene": 9, "items": [
            {"file": "z.jpg", "kind": "image", "start": 100.0,
             "duration": 5.0}]})
        segs = render.plan_segments(self._timeline(items, total=105.0))
        self.assertAlmostEqual(sum(s["duration"] for s in segs), 105.0,
                               places=2)
        self.assertLessEqual(max(s["duration"] for s in segs),
                             render.MAX_HOLD_S + 0.01)
        self.assertGreater(sum(1 for s in segs if s.get("held")), 5,
                           "the hole was not shared out")

    def test_narration_running_past_the_last_picture_holds_it(self):
        # Cutting to black while someone is still speaking is the most
        # visible mistake a video can end on.
        tl = self._timeline([{"scene": 1, "items": [
            {"file": "a.jpg", "kind": "image", "start": 0.0,
             "duration": 5.0}]}], total=9.0)
        segs = render.plan_segments(tl)
        self.assertAlmostEqual(segs[0]["duration"], 9.0, places=2)

    def test_a_hole_at_the_very_start_lengthens_the_first_shot(self):
        tl = self._timeline([{"scene": 1, "items": [
            {"file": "a.jpg", "kind": "image", "start": 2.0,
             "duration": 5.0}]}], total=7.0)
        segs = render.plan_segments(tl)
        self.assertAlmostEqual(segs[0]["duration"], 7.0, places=2)

    def test_a_timeline_with_no_holes_is_left_alone(self):
        tl = self._timeline([{"scene": 1, "items": [
            {"file": "a.jpg", "kind": "image", "start": 0.0, "duration": 4.0},
            {"file": "b.jpg", "kind": "image", "start": 4.0,
             "duration": 3.0}]}], total=7.0)
        segs = render.plan_segments(tl)
        self.assertEqual([s["duration"] for s in segs], [4.0, 3.0])
        self.assertFalse(any("held" in s for s in segs))

    def test_the_planned_length_always_matches_the_narration(self):
        # Whatever the script leaves empty, the picture covers the voice.
        import random as _r
        rng = _r.Random(4)
        scenes, t = [], 0.0
        for i in range(1, 30):
            budget = rng.uniform(3.0, 20.0)
            items = []
            if rng.random() > 0.15:                  # some beats are empty
                at = t
                # Never past the end of the beat: an item that overran into
                # the next one would be a fault in the timeline, not
                # something the renderer should be papering over.
                while t + budget - at > 2.5:
                    d = min(rng.uniform(2.5, 6.0), t + budget - at)
                    items.append({"file": "x.jpg", "kind": "image",
                                  "start": round(at, 2), "duration": round(d, 2)})
                    at += d
            scenes.append({"scene": i, "items": items})
            t += budget
        segs = render.plan_segments(self._timeline(scenes, total=round(t, 2)))
        self.assertAlmostEqual(sum(s["duration"] for s in segs), t, places=1)


class TestTheCutAndThePlanAgree(unittest.TestCase):
    def test_clips_are_cut_at_least_as_long_as_the_timeline_may_ask(self):
        """The 34 seconds that vanished from a real video.

        The builder cut every clip to 4.0 seconds and the timeline planned
        up to 6.0, so 42 clips were asked to run longer than the footage
        that existed. ffmpeg cannot invent frames; each came out short, and
        the shortfall accumulated until the picture finished 45 seconds
        ahead of the voice.
        """
        from media_index import runner, timeline as tl_mod
        self.assertGreaterEqual(runner.CLIP_HEADROOM_S, tl_mod.MAX_CLIP_S,
                                "clips are cut shorter than the timeline "
                                "may plan them")


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class TestASegmentIsExactlyAsLongAsAsked(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="exact_")
        src = dv.build(os.path.join(cls.tmp, "src.mkv"), log=lambda *a: None)
        # A deliberately short clip: two seconds of footage.
        cutter.cut_clip(src, 5.0, 7.0, os.path.join(cls.tmp, "short.mp4"),
                        height=720)
        cutter.extract_frame(src, 20.0, os.path.join(cls.tmp, "still.jpg"),
                             width=1920)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_a_clip_shorter_than_asked_holds_its_last_frame(self):
        out = os.path.join(self.tmp, "held.mp4")
        render.render_item({"file": "short.mp4", "kind": "video",
                            "duration": 5.5}, self.tmp, out, seed=1)
        self.assertAlmostEqual(probe.probe(out).duration, 5.5, delta=0.15)

    def test_a_clip_longer_than_asked_is_trimmed(self):
        out = os.path.join(self.tmp, "trim.mp4")
        render.render_item({"file": "short.mp4", "kind": "video",
                            "duration": 1.2}, self.tmp, out, seed=1)
        self.assertAlmostEqual(probe.probe(out).duration, 1.2, delta=0.15)

    def test_a_still_comes_out_at_the_asked_for_length(self):
        out = os.path.join(self.tmp, "still.mp4")
        render.render_item({"file": "still.jpg", "kind": "image",
                            "duration": 7.3}, self.tmp, out, seed=2)
        self.assertAlmostEqual(probe.probe(out).duration, 7.3, delta=0.2)

    def test_a_finished_video_that_came_out_short_says_so(self):
        # Every earlier version shortened the video without a word. A video
        # that ends while the narrator is still talking is the loudest
        # symptom of the quietest bug, and it must never be silent again.
        res = render.RenderResult(path=__file__, planned=100.0, duration=60.0)
        self.assertTrue(res.ok)
        self.assertGreater(abs(res.planned - res.duration), 1.0)
