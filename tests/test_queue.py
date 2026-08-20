"""Tests for the job queue and its pre-flight gate.

The behaviour these protect is the one that matters at 3 a.m.: a job that
cannot be built is identified BEFORE any rendering starts, is skipped rather
than half-attempted, and never stops the jobs behind it.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from media_index import cutter, jobs as jobs_mod, library, probe, runner  # noqa: E402
from media_index.demo import make_demo_video as dv                        # noqa: E402

HAVE_FFMPEG = probe.ffmpeg_bin() is not None
skip_no_ffmpeg = unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")


def shot(source, dialogue):
    return {"source": source, "season_episode": "unknown",
            "exact_dialogue": dialogue, "visual": "x"}


def write_script(path, shots):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([{"beat": i + 1, "narration": f"Narration {i + 1}.",
                    "shots": [s]} for i, s in enumerate(shots)], f)


class TestAScriptCopiedOutOfAChatWindow(unittest.TestCase):
    """A chat model asked for JSON returns JSON. Its web page returns
    typographic quotes, and copying out of one is how a real script arrived
    with 6,840 of them.

    The error it produced — "Expecting property name enclosed in double
    quotes: line 3 column 1" — is true and useless: the quotes ARE there,
    they are simply the wrong ones, and nothing in the message says so.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="smart_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, text, name="s.json"):
        p = os.path.join(self.tmp, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_curly_quotes_are_straightened_and_read(self):
        p = self._write('[{“beat”: 1, “narration”: '
                        '“He doesn’t look at them.”}]')
        beats = jobs_mod.read_beats(p)
        self.assertEqual(beats[0]["beat"], 1)
        self.assertEqual(beats[0]["narration"], "He doesn't look at them.")

    def test_a_byte_order_mark_and_crlf_are_not_a_problem(self):
        p = self._write('﻿[\r\n{“beat”: 1}\r\n]')
        self.assertEqual(jobs_mod.read_beats(p)[0]["beat"], 1)

    def test_a_script_that_is_simply_broken_still_says_what_is_wrong(self):
        p = self._write('[{"beat": 1,,}]')
        with self.assertRaises(json.JSONDecodeError):
            jobs_mod.read_beats(p)

    def test_straightening_leaves_ordinary_json_untouched(self):
        text = '[{"beat": 1, "narration": "plain"}]'
        self.assertEqual(jobs_mod.straighten(text), text)


class TestJobFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jobfile_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, payload):
        p = os.path.join(self.tmp, "jobs.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        return p

    def test_defaults_apply_to_every_job(self):
        p = self._write({"defaults": {"clip_seconds": 3.0, "height": 720},
                         "jobs": [{"name": "a", "script": "a.json"},
                                  {"name": "b", "script": "b.json"}]})
        for job in jobs_mod.load_jobs(p):
            self.assertEqual(job.clip_seconds, 3.0)
            self.assertEqual(job.height, 720)

    def test_job_overrides_default(self):
        p = self._write({"defaults": {"clip_seconds": 3.0},
                         "jobs": [{"name": "a", "script": "a.json",
                                   "clip_seconds": 6.0}]})
        self.assertEqual(jobs_mod.load_jobs(p)[0].clip_seconds, 6.0)

    def test_relative_paths_resolve_against_the_job_file(self):
        p = self._write({"jobs": [{"name": "a", "script": "scripts/a.json"}]})
        job = jobs_mod.load_jobs(p)[0]
        self.assertTrue(os.path.isabs(job.script))
        self.assertTrue(job.script.startswith(self.tmp))

    def test_bare_list_is_accepted(self):
        p = self._write([{"name": "a", "script": "a.json"}])
        self.assertEqual(len(jobs_mod.load_jobs(p)), 1)

    def test_missing_name_gets_one(self):
        p = self._write({"jobs": [{"script": "a.json"}]})
        self.assertTrue(jobs_mod.load_jobs(p)[0].name)


class _QueueCase(unittest.TestCase):
    """A real two-title library plus scripts of varying health."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="queue_")
        media = os.path.join(cls.tmp, "Media")
        dv.build(os.path.join(media, "Iron Harvest", "Season 01",
                              "Iron.Harvest.S01E01.1080p-PSA.mkv"),
                 log=lambda *a: None)
        dv.build(os.path.join(media, "The Long Winter (2019)",
                              "The.Long.Winter.2019.1080p-PSA.mkv"),
                 log=lambda *a: None)
        cls.db = os.path.join(cls.tmp, "library.db")
        library.build(media, cls.db, log=lambda *a: None)

        sc = os.path.join(cls.tmp, "scripts")
        write_script(os.path.join(sc, "good.json"), [
            shot("Iron Harvest",
                 "I never wanted the harvest. I wanted the land it grew on."),
            shot("Iron Harvest", "Nobody walks out of this clean"),
            shot("Iron Harvest", "Then we burn the field")])
        write_script(os.path.join(sc, "soft.json"), [
            shot("The Long Winter", "Blue segment, a single sentence"),
            shot("The Long Winter", ""),                 # no dialogue at all
            shot("The Long Winter", "Teal, and the last warning"),
            shot("The Long Winter", "Orange, and it is already too late")])
        write_script(os.path.join(sc, "missing.json"), [
            shot("El Camino", "You never asked me what it cost")])
        # A title that IS in the library, but not one line of it matches.
        # With no anchor anywhere in the run there is nothing to interpolate
        # between, and the honest answer is to build nothing and say so.
        write_script(os.path.join(sc, "unanchored.json"), [
            shot("The Long Winter", "qqq zzz not a line in this film"),
            shot("The Long Winter", "another sentence nobody ever said")])
        write_script(os.path.join(sc, "mostly.json"), [
            shot("Iron Harvest", "I never wanted the harvest"),
            shot("Iron Harvest", "Then we burn the field"),
            shot("Iron Harvest", "Nobody walks out of this clean"),
            shot("El Camino", "You never asked me what it cost")])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def job_file(cls, entries, name="jobs.json", **defaults):
        p = os.path.join(cls.tmp, name)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"defaults": {"db": cls.db, "clip_seconds": 4.0,
                                    "height": 360, **defaults},
                       "jobs": entries}, f)
        return p


@skip_no_ffmpeg
class TestPreflightGate(_QueueCase):
    def _report(self, script, out, **kw):
        p = self.job_file([{"name": "j", "script": f"scripts/{script}",
                            "out": f"gate/{out}", **kw}], name=f"g_{out}.json")
        return jobs_mod.preflight_all(jobs_mod.load_jobs(p))[0]

    def test_healthy_script_is_ready(self):
        self.assertEqual(self._report("good.json", "a").status, "READY")

    def test_one_soft_scene_builds_with_gaps_not_blocked(self):
        """The important call: a video with one weak scene is still a video."""
        rep = self._report("soft.json", "b")
        self.assertEqual(rep.status, "GAPS")
        self.assertFalse(rep.blocked)

    def test_whole_title_missing_is_blocked(self):
        rep = self._report("missing.json", "c")
        self.assertEqual(rep.status, "BLOCKED")
        self.assertTrue(any("sources in library" == c.name and not c.ok
                            for c in rep.checks))

    def test_one_title_of_four_missing_only_downgrades(self):
        """A missing title that costs a quarter of the shots is a gap, not a
        blocker — the other three quarters are still worth building."""
        self.assertEqual(self._report("mostly.json", "d").status, "GAPS")

    def test_missing_script_file_is_blocked(self):
        rep = self._report("does_not_exist.json", "e")
        self.assertEqual(rep.status, "BLOCKED")

    def test_missing_library_is_blocked(self):
        rep = self._report("good.json", "f", db="/nowhere/library.db")
        self.assertEqual(rep.status, "BLOCKED")
        self.assertTrue(any("library index" == c.name and not c.ok
                            for c in rep.checks))

    def test_missing_audio_is_blocked(self):
        rep = self._report("good.json", "g", audio="/nowhere/narration.mp3")
        self.assertEqual(rep.status, "BLOCKED")

    def test_preflight_never_raises(self):
        """A malformed script must produce a report, not an exception."""
        bad = os.path.join(self.tmp, "scripts", "bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        rep = self._report("bad.json", "h")
        self.assertEqual(rep.status, "BLOCKED")


@skip_no_ffmpeg
class TestQueueRun(_QueueCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.jf = cls.job_file([
            {"name": "All good", "script": "scripts/good.json", "out": "run/a"},
            {"name": "One soft scene", "script": "scripts/soft.json", "out": "run/b"},
            {"name": "Title missing", "script": "scripts/missing.json", "out": "run/c"},
            {"name": "No anchors", "script": "scripts/unanchored.json",
             "out": "run/d"},
        ], name="run.json")
        cls.results = runner.run_queue(cls.jf, log=lambda *a: None)

    def test_blocked_job_is_skipped_not_attempted(self):
        blocked = self.results[2]
        self.assertEqual(blocked.status, "skipped")
        self.assertEqual(blocked.clips, 0)
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, "run", "c",
                                                    "scene_001")))

    def test_a_blocked_job_does_not_stop_the_others(self):
        self.assertEqual(self.results[0].status, "done")
        self.assertEqual(self.results[1].status, "done")

    def test_healthy_job_cuts_every_scene(self):
        r = self.results[0]
        self.assertEqual(len(r.scenes), 3)
        self.assertEqual(r.clips, 3)
        self.assertEqual(r.gaps, 0)

    def test_a_silent_shot_is_placed_along_the_scene(self):
        """The beat with no dialogue at all used to produce nothing.

        On a real scene breakdown that case is not the exception — 92% of
        shots quote no line, because the best scenes are the quiet ones. It
        is now placed between the shots that did match, so the beat gets
        footage instead of a hole.
        """
        r = self.results[1]
        self.assertEqual(r.gaps, 0)
        self.assertGreater(r.clips, 2)

    def test_an_interpolated_shot_is_labelled_as_one(self):
        """Placed is not the same as matched, and the manifest has to say so
        while it can still be checked."""
        with open(os.path.join(self.tmp, "run", "b", "manifest.json"),
                  encoding="utf-8") as f:
            man = json.load(f)
        placed_by = [a["placed_by"] for s in man["scenes"] for a in s["assets"]]
        self.assertIn("anchor", placed_by)
        self.assertIn("interpolated", placed_by)

    def test_a_guess_never_becomes_a_moving_clip(self):
        """Fail-closed: a moving clip claims 'this is the moment', so only a
        located/verified placement may make it. An interpolated guess is shown
        as a STILL — an honest 'roughly this scene' — never confident motion."""
        with open(os.path.join(self.tmp, "run", "b", "manifest.json"),
                  encoding="utf-8") as f:
            man = json.load(f)
        video_methods = {a["placed_by"] for s in man["scenes"]
                         for a in s["assets"] if a["kind"] == "video"}
        # Every moving clip is from a trusted method; no guess among them.
        self.assertTrue(video_methods)
        self.assertTrue(video_methods <= runner.MOTION_OK,
                        f"a guess shipped as motion: {video_methods}")
        self.assertNotIn("interpolated", video_methods)
        # ...but the interpolated shot is still present, as a still.
        still_methods = {a["placed_by"] for s in man["scenes"]
                         for a in s["assets"] if a["kind"] == "image"}
        self.assertIn("interpolated", still_methods)

    def test_a_run_with_no_anchor_never_reaches_rendering(self):
        """Interpolation needs something to interpolate between.

        A script whose lines match nothing has no anchors, so nothing can be
        placed from it. The gate catches that during pre-flight, before any
        encoding — which is the whole point of pre-flighting first.
        """
        r = self.results[3]
        self.assertEqual(r.status, "skipped")
        self.assertEqual(r.clips, 0)
        self.assertFalse(os.path.isdir(
            os.path.join(self.tmp, "run", "d", "scene_001")))

    def test_a_scene_whose_shots_cannot_be_placed_says_so(self):
        """A beat reached at build time with no usable placement and no
        episode to fall back on writes no footage and gives a reason."""
        job = jobs_mod.load_jobs(self.jf)[0]
        job.out = os.path.join(self.tmp, "unplaceable")
        beat = {"beat": 1, "narration": "N.", "shots": [{"source": "x"}]}
        nowhere = [runner.align.Placement(beat=1, shot=1)]
        scene = runner.build_scene(job, 1, beat, nowhere, [],
                                   log=lambda *a: None, mode=runner.tiers.DRAFT)
        self.assertFalse(scene.clips)
        self.assertFalse(scene.stills)
        self.assertEqual(scene.status, "empty")
        self.assertIn("could be placed", scene.note)

    @unittest.skipUnless(probe.ffmpeg_bin(), "ffmpeg not installed")
    def test_in_strict_mode_an_unproven_beat_becomes_a_card(self):
        """The trade Strict makes: less footage, and complete trust in the
        footage there is. The beat keeps its duration as a NEEDS VISUAL card
        rather than being covered by a neighbour nobody labelled."""
        job = jobs_mod.load_jobs(self.jf)[0]
        job.out = os.path.join(self.tmp, "strict")
        beat = {"beat": 1, "narration": "N.", "shots": [{"source": "x"}]}
        nowhere = [runner.align.Placement(beat=1, shot=1)]
        scene = runner.build_scene(job, 1, beat, nowhere, [],
                                   log=lambda *a: None,
                                   mode=runner.tiers.STRICT)
        self.assertTrue(scene.cards, "no card was drawn")
        self.assertFalse(scene.clips)
        self.assertEqual(scene.status, "needs_visual")
        self.assertEqual(scene.tiers[os.path.basename(scene.cards[0])], "C")

    def test_output_layout_matches_the_editor_tools(self):
        scene = os.path.join(self.tmp, "run", "a", "scene_001")
        names = os.listdir(scene)
        self.assertTrue(any(n.startswith("clip_") and n.endswith(".mp4")
                            for n in names))
        self.assertTrue(any(n.startswith("image_") and n.endswith(".jpg")
                            for n in names))
        self.assertIn("scene.txt", names)

    def test_every_asset_names_its_own_episode(self):
        """A beat routinely draws from two episodes — the scene, and the
        flashback it refers to. Labelling every asset with whichever one the
        FIRST shot came from reported six shots as Season 4 Episode 1 while
        they were sitting in Season 3 Episode 13, which is exactly the kind
        of wrong label that sends an investigation into the wrong file."""
        with open(os.path.join(self.tmp, "run", "a", "manifest.json"),
                  encoding="utf-8") as f:
            man = json.load(f)
        for scene in man["scenes"]:
            for asset in scene["assets"]:
                self.assertIn("source", asset)
                self.assertTrue(asset["source"], asset["file"])

    def test_manifest_carries_scores_and_provenance(self):
        with open(os.path.join(self.tmp, "run", "a", "manifest.json"),
                  encoding="utf-8") as f:
            man = json.load(f)
        self.assertEqual(len(man["scenes"]), 3)
        first = man["scenes"][0]
        self.assertTrue(first["assets"])
        self.assertIn("score", first["assets"][0])
        self.assertTrue(first["source"])          # which episode it came from

    def test_queue_report_is_written(self):
        self.assertTrue(os.path.isfile(
            os.path.splitext(self.jf)[0] + "_report.json"))

    def test_rerun_resumes_instead_of_rebuilding(self):
        again = runner.run_queue(self.jf, log=lambda *a: None)
        self.assertEqual(again[0].status, "done")
        self.assertTrue(all(s.status == "reused" for s in again[0].scenes))

    def test_dry_run_builds_nothing(self):
        jf = self.job_file(
            [{"name": "dry", "script": "scripts/good.json", "out": "run/dry"}],
            name="dry.json")
        runner.run_queue(jf, log=lambda *a: None, dry_run=True)
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, "run", "dry",
                                                    "scene_001")))


@skip_no_ffmpeg
class TestJobIsolation(_QueueCase):
    def test_a_crashing_job_does_not_kill_the_queue(self):
        """Whatever goes wrong inside one job, the next one still runs."""
        original = cutter.cut_clip
        calls = {"n": 0}

        def exploding(path, start, end, out, **kw):
            calls["n"] += 1
            if "boom" in out:
                raise RuntimeError("simulated encoder failure")
            return original(path, start, end, out, **kw)

        jf = self.job_file([
            {"name": "Boom", "script": "scripts/good.json", "out": "boom"},
            {"name": "After", "script": "scripts/good.json", "out": "after"},
        ], name="isolate.json")
        cutter.cut_clip = exploding
        try:
            results = runner.run_queue(jf, log=lambda *a: None)
        finally:
            cutter.cut_clip = original

        self.assertEqual(results[0].clips, 0)          # every shot failed
        self.assertGreater(results[1].clips, 0)        # the next job still ran
        self.assertEqual(results[1].status, "done")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestTwoStillsFromOneShotAreTwoPictures(unittest.TestCase):
    """A shot asking for two stills must not return one image twice.

    On a real build 75 of 103 still-shots produced a pair, and side by side
    on the contact sheet many of those pairs are plainly the same picture.
    The de-duplicator was not at fault: asked for the two best frames in a
    1.5-second window of a static two-hander, it correctly returned the two
    best, and in 1.5 seconds of that shot nothing moves.

    So the window widens with the number of stills wanted, and so does the
    minimum distance between them.
    """

    def test_the_window_grows_with_the_number_of_stills(self):
        one = runner.STILL_WINDOW_S * 1
        four = runner.STILL_WINDOW_S * 4
        self.assertGreater(four, one)
        self.assertGreaterEqual(four, 4.0,
                                "four stills need seconds of footage to differ")

    @unittest.skipUnless(probe.ffmpeg_bin(), "ffmpeg not installed")
    def test_two_stills_of_one_moment_land_seconds_apart(self):
        # 25-35s spans a cut in the demo video, so two genuinely different
        # pictures exist. They must be found, and they must not be adjacent
        # frames of the same instant.
        tmp = tempfile.mkdtemp(prefix="stills_")
        try:
            vid = dv.build(os.path.join(tmp, "v.mkv"), log=lambda *a: None)
            got = runner._stills_for(vid, 25.0, 35.0, tmp, 1, 2, [],
                                     log=lambda *a: None)
            self.assertEqual(len(got), 2, "expected two distinct stills")
            (_p1, t1), (_p2, t2) = got
            self.assertGreater(abs(t2 - t1), 1.5,
                               f"{t1:.1f}s and {t2:.1f}s is the same moment")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @unittest.skipUnless(probe.ffmpeg_bin(), "ffmpeg not installed")
    def test_a_genuinely_static_moment_yields_one_still_not_two_alike(self):
        # Asked for two stills of a stretch where nothing moves, the honest
        # answer is one. Returning two would return the same image twice,
        # which is what the contact sheet has been full of.
        tmp = tempfile.mkdtemp(prefix="stills_")
        try:
            vid = dv.build(os.path.join(tmp, "v.mkv"), log=lambda *a: None)
            got = runner._stills_for(vid, 18.0, 22.0, tmp, 1, 2, [],
                                     log=lambda *a: None)
            self.assertEqual(len(got), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestOneMomentGetsOnScreenOnce(unittest.TestCase):
    """The visible half of the placement failure, and its last net.

    Thirty-one of the first sixty-six pictures of a finished video came out
    of one six-second stretch of episode. Nothing objected, because each
    frame was technically a different frame — a hand moving through a shot
    makes every frame slightly different, which is exactly where a
    perceptual comparison is weakest. Time cannot be argued with that way.
    """

    def _job(self, tmp):
        return jobs_mod.Job(name="j", script="s.json", out=tmp)

    def test_a_shot_landing_where_one_already_played_moves_aside(self):
        """Refusing it outright emptied seven scenes of a real build, and the
        holes became stills sitting on screen for half a minute. The
        placement is an interpolated guess anyway; moving it two seconds
        costs nothing and keeps the scene."""
        moved = runner._free_moment({"/ep.mkv": [1930.0]}, "/ep.mkv", 1930.4)
        self.assertIsNotNone(moved)
        self.assertFalse(runner._repeated({"/ep.mkv": [1930.0]}, "/ep.mkv",
                                          moved))
        self.assertLess(abs(moved - 1930.4), runner.SHIFT_REACH_S)

    def test_a_shot_with_nowhere_to_go_is_dropped_not_repeated(self):
        packed = {"/ep.mkv": [float(t) for t in range(0, 400)]}
        self.assertIsNone(runner._free_moment(packed, "/ep.mkv", 200.0))


class TestABeatNobodyCouldPlaceStillShowsSomething(unittest.TestCase):
    """Three runs of a real script carried no quoted line and matched no
    picture. 198 seconds of an eleven-minute video had nothing at all to
    show, the shots around those holes were stretched to cover them, and a
    twelve-second still ran for thirty.

    The script still names the episode. Footage from the right episode is
    what an editor reaches for when the exact frame cannot be found, and it
    is labelled `filler` everywhere it appears so nobody mistakes it for a
    match.
    """

    def test_filler_moments_spread_across_the_episode(self):
        used, got = {}, []
        for _ in range(6):
            at = runner._filler_moment(used, "/e.mkv", 2800.0,
                                       len(used.get("/e.mkv", ())))
            got.append(at)
            used.setdefault("/e.mkv", []).append(at)
        self.assertEqual(len(set(got)), 6)
        for a in got:                       # never the titles, never the credits
            self.assertGreater(a, 2800.0 * runner.FILLER_SPREAD[0] - 1)
            self.assertLess(a, 2800.0 * runner.FILLER_SPREAD[1] + 1)
        for a in got:                       # and never twice the same corner
            self.assertEqual(sum(1 for b in got
                                 if abs(a - b) < runner.FILLER_APART_S), 1)

    def test_no_episode_means_no_filler_rather_than_a_crash(self):
        self.assertEqual(runner._filler_for("", {}, lambda *a: None),
                         (None, ""))
        self.assertEqual(runner._filler_for("/nope.mkv", {}, lambda *a: None),
                         (None, ""))

    @unittest.skipUnless(probe.ffmpeg_bin(), "ffmpeg not installed")
    def test_an_unplaceable_beat_is_filled_from_its_own_episode(self):
        tmp = tempfile.mkdtemp(prefix="filler_")
        try:
            vid = dv.build(os.path.join(tmp, "e.mkv"), log=lambda *a: None)
            job = jobs_mod.Job(name="j", script="s.json",
                               out=os.path.join(tmp, "out"))
            beat = {"beat": 1, "narration": "N.", "shots": [{"source": "x"}]}
            nowhere = [runner.align.Placement(beat=1, shot=1)]
            scene = runner.build_scene(job, 1, beat, nowhere, [],
                                       log=lambda *a: None, used={},
                                       episode=vid, mode=runner.tiers.DRAFT)
            self.assertTrue(scene.ok, "the beat still has nothing to show")
            self.assertEqual(scene.filler, len(scene.methods))
            self.assertIn("filled from this episode", scene.note)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_far_enough_away_is_a_different_shot(self):
        self.assertFalse(runner._repeated({"/ep.mkv": [1930.0]}, "/ep.mkv",
                                          1930.0 + runner.REPEAT_APART_S))
        self.assertTrue(runner._repeated({"/ep.mkv": [1930.0]}, "/ep.mkv",
                                         1930.0 + runner.REPEAT_APART_S / 2))

    def test_the_same_second_of_a_different_episode_is_fine(self):
        self.assertFalse(runner._repeated({"/a.mkv": [1930.0]}, "/b.mkv",
                                          1930.0))

    def test_without_a_record_nothing_is_refused(self):
        # build_scene is called directly by tests and by tools that do not
        # track a whole video; no record means no de-duplication, not a crash.
        self.assertFalse(runner._repeated(None, "/ep.mkv", 1930.0))
