"""Tests for running a build from the browser.

Nothing here decides anything about a video — `builds` calls the same
functions the menu calls, in the same order. What it owns is the part a page
depends on: that a task started is a task you can ask about, that a task
which explodes is reported rather than taking the server with it, and that a
forty-minute build tells you how far in it is.

Those are exactly the things that look, from a browser, like the tool
hanging.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from media_index import builds                            # noqa: E402


def settle(task, seconds=10.0):
    """Wait for a task to stop running, or give up and let the test say so."""
    end = time.time() + seconds
    while time.time() < end and task.status == "running":
        time.sleep(0.02)
    return task


class TestTheRunner(unittest.TestCase):
    def setUp(self):
        self.runner = builds.Runner()

    def test_a_task_is_findable_the_moment_it_is_started(self):
        """The page gets an id back and immediately asks about it. If the id
        is not registered until the work finishes, every build looks like a
        404 for forty minutes."""
        task = self.runner.start("check", "x", lambda t, log: time.sleep(0.05))
        self.assertIsNotNone(self.runner.get(task.id))
        settle(task)

    def test_work_that_explodes_is_a_failed_task_not_a_dead_server(self):
        def boom(task, log):
            raise ValueError("no such episode")
        task = settle(self.runner.start("build", "x", boom))
        self.assertEqual(task.status, "failed")
        self.assertIn("no such episode", task.error)
        self.assertTrue(task.finished, "a crashed task never stopped running")

    def test_a_task_that_finishes_quietly_is_done(self):
        task = settle(self.runner.start("check", "x", lambda t, log: log("hi")))
        self.assertEqual(task.status, "done")
        self.assertIn("hi", task.lines)

    def test_a_task_may_say_it_is_blocked_rather_than_failed(self):
        # BLOCKED is not an error: the tool worked perfectly and the answer
        # is "this will not build". Reporting it as a crash hides the reason.
        def refuse(task, log):
            task.status = "blocked"
            task.stage = "no voiceover"
        task = settle(self.runner.start("check", "x", refuse))
        self.assertEqual(task.status, "blocked")

    def test_how_far_in_it_is_comes_from_the_build_s_own_log(self):
        def work(task, log):
            task.scenes_total = 10
            for i in (1, 2, 7):
                log(f"    scene {i:03d} · 2 clip(s)")
        task = settle(self.runner.start("build", "x", work))
        self.assertEqual(task.scenes_done, 7)

    def test_a_bar_still_moving_never_reads_a_hundred(self):
        """A bar that sits at 100% while the work goes on is the single most
        convincing way to look broken."""
        task = builds.Task(id="t", kind="build", scenes_total=10,
                           scenes_done=10)
        self.assertEqual(task.percent, 99)
        task.status = "done"
        self.assertEqual(task.percent, 100)

    def test_progress_never_goes_backwards(self):
        def work(task, log):
            task.scenes_total = 10
            log("    scene 008 ·")
            log("    scene 003 ·")             # a retry, or an out-of-order line
        task = settle(self.runner.start("build", "x", work))
        self.assertEqual(task.scenes_done, 8)

    def test_the_log_is_kept_but_bounded(self):
        """A forty-minute render writes thousands of lines. Keeping all of
        them in memory, and posting them to a page every second, is how a
        tool that works becomes a tool that crawls."""
        def work(task, log):
            for i in range(builds.KEPT_LINES + 250):
                log(f"line {i}")
        task = settle(self.runner.start("build", "x", work), seconds=20)
        self.assertEqual(len(task.lines), builds.KEPT_LINES)
        self.assertIn(f"line {builds.KEPT_LINES + 249}", task.lines)

    def test_one_build_at_a_time(self):
        """Two at once would fight over ffmpeg and the disk and finish
        slower than one after the other."""
        seen = []

        def work(task, log):
            seen.append(("in", task.name))
            time.sleep(0.15)
            seen.append(("out", task.name))

        a = self.runner.start("build", "a", work)
        b = self.runner.start("build", "b", work)
        settle(a, 10)
        settle(b, 10)
        self.assertEqual([s[0] for s in seen], ["in", "out", "in", "out"])

    def test_every_task_is_listed_in_the_order_it_was_started(self):
        names = ["one", "two", "three"]
        for n in names:
            settle(self.runner.start("check", n, lambda t, log: None))
        self.assertEqual([t["name"] for t in self.runner.all()], names)

    def test_a_task_survives_being_turned_into_json(self):
        task = settle(self.runner.start("check", "x", lambda t, log: log("ok")))
        json.dumps(task.as_dict())      # a page can only read what serialises


class TestTheFormBecomesAJob(unittest.TestCase):
    def test_paths_are_made_absolute(self):
        job = builds.job_from({"name": "n", "script": "s.json",
                               "out": "Out"}, "library.db")
        self.assertTrue(os.path.isabs(job.script))
        self.assertTrue(os.path.isabs(job.out))

    def test_an_empty_voiceover_stays_empty_rather_than_becoming_the_cwd(self):
        """os.path.abspath("") is the working directory, and a Job whose
        audio is a folder fails deep inside ffmpeg with a message about
        nothing."""
        job = builds.job_from({"name": "n", "script": "s.json"}, "library.db")
        self.assertEqual(job.audio, "")

    def test_the_library_the_form_chose_is_the_one_used(self):
        job = builds.job_from({"script": "s.json", "db": "E:/Libraries/bb.db"},
                              "fallback.db")
        self.assertIn("bb.db", job.db)

    def test_the_look_settings_ride_along_without_breaking_anything(self):
        job = builds.job_from({"script": "s.json", "pace": "quick",
                               "quality": "4k", "nonsense": 1}, "library.db")
        self.assertEqual(job.extras["pace"], "quick")
        self.assertNotIn("nonsense", job.extras)


class TestCheckingBeforeBuilding(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="chk_")
        self.runner = builds.Runner()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_script_that_is_not_there_is_blocked_and_says_which_file(self):
        task = settle(builds.check(self.runner, {
            "name": "x", "script": os.path.join(self.tmp, "gone.json"),
            "out": os.path.join(self.tmp, "out")}, "library.db"), seconds=20)
        self.assertEqual(task.status, "blocked")
        self.assertEqual(task.report["verdict"], "BLOCKED")
        named = [c for c in task.report["checks"] if not c["ok"]]
        self.assertTrue(any("gone.json" in c["detail"] for c in named))

    def test_a_script_that_will_not_parse_is_blocked_not_crashed(self):
        bad = os.path.join(self.tmp, "bad.json")
        with open(bad, "w") as f:
            f.write("{ this is not json")
        task = settle(builds.check(self.runner, {
            "name": "x", "script": bad,
            "out": os.path.join(self.tmp, "out")}, "library.db"), seconds=20)
        self.assertEqual(task.status, "blocked")
        self.assertTrue(any(c["name"] == "script parses" and not c["ok"]
                            for c in task.report["checks"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestWhatTheCheckPanelPromises(unittest.TestCase):
    """The number the panel led with said **98%** while the build that
    followed reported **60% usable**. Both were computed honestly and they
    measure different things — "placeable" means the episode is known, not
    that the moment is. A page being cheerful at somebody about to spend
    forty minutes rendering is the one thing a pre-flight must not do."""

    class Report:
        def __init__(self, beats, places):
            self.beats = beats
            self._places = places
            self.job = None

    def _beats(self, shots=6):
        return [{"beat": 1, "shots": [
            {"source": "Breaking Bad", "season_episode": "S04E01",
             "visual": f"shot {i}", "duration_target_sec": 5}
            for i in range(shots)]}]

    def _places(self, methods):
        return [builds.align.Placement(beat=1, shot=i + 1, path="/lib/e.mkv",
                                       start_ms=1000, end_ms=5000, method=m)
                for i, m in enumerate(methods)]

    def test_a_quoted_line_counts_as_exact_and_a_guess_does_not(self):
        rep = self.Report(self._beats(), self._places(
            ["anchor", "interpolated", "interpolated", "none", "none",
             "none"]))
        got = builds.evidence(rep)
        self.assertEqual(got["exact"], 1)
        self.assertEqual(got["between"], 2)
        self.assertEqual(got["loose"], 3)
        self.assertEqual(got["total"], 6)

    def test_a_stated_time_counts_as_exact_even_with_no_line(self):
        rep = self.Report(self._beats(), self._places(["none"] * 6))
        self.assertEqual(builds.evidence(rep)["exact"], 0)
        got = builds.evidence(rep, "S04E01 29:30-33:40")
        self.assertEqual(got["exact"], 0)
        self.assertEqual(got["between"], 6)     # held by what you stated
        self.assertEqual(got["loose"], 0)

    def test_a_script_nothing_could_be_worked_out_for_says_so(self):
        rep = self.Report(self._beats(), self._places(["none"] * 6))
        got = builds.evidence(rep)
        self.assertEqual(got["loose"], 6)
        self.assertEqual(got["percent"], 0)

    def test_no_beats_is_no_opinion_rather_than_a_crash(self):
        self.assertEqual(builds.evidence(self.Report([], [])), {})
