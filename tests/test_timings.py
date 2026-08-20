"""Times somebody states, and the runs they rescue.

Every number in here is from a real build log. A run of eighty-five shots
with no quoted line, no picture match above chance, and no opinion about
where in a forty-seven minute episode it happens is not a solvable problem
for any amount of modelling — and it is one line to type.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from media_index import align, timings                     # noqa: E402


class TestReadingATimecode(unittest.TestCase):

    def test_the_shapes_people_actually_write(self):
        for text, want in [("29:30", 1770.0), ("1:29:30", 5370.0),
                           ("0:05", 5.0), ("29:30.5", 1770.5),
                           ("1787", 1787.0), ("29m47s", 1787.0),
                           ("1h2m3s", 3723.0), (1787, 1787.0),
                           ("  29:30  ", 1770.0), ("29;30", 1770.0)]:
            self.assertAlmostEqual(timings.parse_timecode(text), want,
                                   msg=repr(text))

    def test_anything_it_cannot_read_is_refused_rather_than_guessed(self):
        """A misread timecode is worse than none: it is a confident wrong
        answer wearing the one label this tool promises never to check."""
        for text in ("", "   ", None, "soon", "the box cutter scene",
                     "29:xx", "--"):
            self.assertIsNone(timings.parse_timecode(text), msg=repr(text))

    def test_a_range_in_every_dash_a_word_processor_makes(self):
        for text in ("29:30-33:40", "29:30 - 33:40", "29:30–33:40",
                     "29:30 to 33:40", "29:30—33:40"):
            self.assertEqual(timings.parse_range(text), (1770.0, 2020.0),
                             msg=repr(text))

    def test_a_single_time_is_a_point_not_a_range(self):
        self.assertEqual(timings.parse_range("29:30"), (1770.0, 1770.0))
        said = timings.Stated(lo=1770.0, hi=1770.0)
        lo, hi = said.window
        self.assertAlmostEqual(hi - lo, timings.POINT_PAD_S * 2)

    def test_a_backwards_range_is_read_the_way_it_was_meant(self):
        self.assertEqual(timings.parse_range("33:40-29:30"), (1770.0, 2020.0))


class TestTheBoxSomebodyTypesInto(unittest.TestCase):

    def test_the_lines_the_placeholder_shows(self):
        got = timings.parse_lines("S04E01 29:30-33:40\n"
                                  "S03E13 30:05-30:35\n"
                                  "Breaking Bad S04E08 43:40-46:30\n")
        self.assertEqual(len(got), 3)
        self.assertEqual((got[0].season, got[0].episode), (4, 1))
        self.assertEqual(got[0].window, (1770.0, 2020.0))
        self.assertEqual(got[2].show, "Breaking Bad")

    def test_other_ways_of_naming_an_episode(self):
        got = timings.parse_lines("4x01 29:30-33:40\ns4e1: 10:00\n")
        self.assertEqual(len(got), 2)
        self.assertEqual((got[0].season, got[0].episode), (4, 1))
        self.assertEqual((got[1].season, got[1].episode), (4, 1))

    def test_a_stray_line_is_skipped_not_raised_on(self):
        """Somebody pasting six lines out of a chat window will have a
        heading in there. Losing the whole box to it, right before a
        two-hour build, is a poor trade for strictness nobody asked for."""
        got = timings.parse_lines("Here are the timings:\n"
                                  "# my notes\n"
                                  "\n"
                                  "S04E01 29:30-33:40\n"
                                  "thanks!\n")
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0].season, got[0].episode), (4, 1))

    def test_an_empty_box_states_nothing(self):
        self.assertEqual(timings.parse_lines(""), [])
        self.assertEqual(timings.parse_lines(None), [])


def _beats(shots=6, se="S04E01", show="Breaking Bad", **extra):
    return [{"beat": 1, "shots": [
        dict({"kind": "clip", "source": show, "season_episode": se,
              "visual": f"shot {i}", "duration_target_sec": 5}, **extra)
        for i in range(shots)]}]


class TestWhatAStatedTimeDoes(unittest.TestCase):

    def test_the_whole_run_is_confined_to_what_was_stated(self):
        beats = _beats()
        said = timings.parse_lines("S04E01 29:30-33:40")
        got = timings.windows_for(beats, said)
        # Keyed by SHOT, not by beat: a beat routinely draws from several
        # episodes, and one window per beat is one episode's stretch applied
        # to everybody else's footage.
        self.assertEqual(got[(1, 1)], (1770.0, 2020.0))
        self.assertEqual(len(got), 6)

    def test_a_line_about_a_different_episode_is_ignored(self):
        beats = _beats(se="S04E01")
        said = timings.parse_lines("S02E07 29:30-33:40")
        self.assertEqual(timings.windows_for(beats, said), {})

    def test_a_line_naming_a_different_show_is_ignored(self):
        beats = _beats(show="Breaking Bad")
        said = timings.parse_lines("Game of Thrones S04E01 29:30-33:40")
        self.assertEqual(timings.windows_for(beats, said), {})

    def test_the_box_in_front_of_you_beats_the_script_written_days_ago(self):
        beats = _beats(scene_range="10:00-12:00")
        said = (timings.from_script(beats)
                + timings.parse_lines("S04E01 29:30-33:40"))
        self.assertEqual(timings.windows_for(beats, said)[(1, 1)],
                         (1770.0, 2020.0))

    def test_a_script_can_state_the_range_itself(self):
        beats = _beats(scene_range="29:30-33:40")
        said = timings.from_script(beats)
        self.assertEqual(len(said), 1)
        self.assertEqual(said[0].window, (1770.0, 2020.0))

    def test_every_name_a_model_might_use_for_the_field(self):
        for key in timings.RANGE_KEYS:
            said = timings.from_script(_beats(**{key: "29:30-33:40"}))
            self.assertEqual(len(said), 1, msg=key)

    def test_the_runs_nobody_stated_a_time_for_come_back_worst_first(self):
        beats = [{"beat": 1, "shots": [
                    {"source": "Breaking Bad", "season_episode": "S04E01",
                     "visual": f"a {i}"} for i in range(85)]},
                 {"beat": 2, "shots": [
                    {"source": "Breaking Bad", "season_episode": "S03E13",
                     "visual": f"b {i}"} for i in range(6)]}]
        left = timings.unstated(beats, timings.parse_lines("S03E13 30:05"))
        self.assertEqual(len(left), 1)
        self.assertEqual(left[0][0], 85)
        self.assertIn("S04E01", left[0][1])


class TestAStatedShotTime(unittest.TestCase):
    """A time on one shot is an anchor, and enters as the strongest kind
    there is — the only evidence in this package that was never inferred."""

    def _run(self, **extra):
        beats = [{"beat": 1, "shots": [
            dict({"source": "Show", "season_episode": "S01E01",
                  "visual": f"shot {i}", "duration_target_sec": 4},
                 **(extra if i == 2 else {}))
            for i in range(6)]}]
        return align.runs(beats)[0]

    def test_a_stated_shot_becomes_an_anchor_without_any_subtitle(self):
        run = self._run(at="29:30")
        with mock.patch.object(align, "episode_file",
                                        return_value="/lib/ep.mkv"):
            got = align.stated_anchors("db", run)
        self.assertEqual(len(got), 1)
        index, start_ms, _end, path, conf = got[0]
        self.assertEqual(index, 2)
        self.assertEqual(start_ms, 1770_000)
        self.assertEqual(path, "/lib/ep.mkv")
        self.assertEqual(conf, "high")

    def test_every_name_a_model_might_use_for_a_shot_time(self):
        for key in timings.SHOT_TIME_KEYS:
            run = self._run(**{key: 1770})
            with mock.patch.object(align, "episode_file",
                                            return_value="/lib/ep.mkv"):
                self.assertEqual(len(align.stated_anchors("db", run)), 1,
                                 msg=key)

    def test_a_run_stating_nothing_produces_no_anchors(self):
        with mock.patch.object(align, "episode_file",
                                        return_value="/lib/ep.mkv"):
            self.assertEqual(align.stated_anchors("db", self._run()), [])

    def test_an_episode_the_library_cannot_resolve_is_skipped(self):
        run = self._run(at="29:30")
        with mock.patch.object(align, "episode_file",
                                        return_value=""):
            self.assertEqual(align.stated_anchors("db", run), [])


if __name__ == "__main__":
    unittest.main()


class TestARangeGivenTooWide(unittest.TestCase):
    """A real script came back with `S03E13 40:00-47:00` for a six-shot run —
    seven minutes of episode for thirty seconds of video — and three more on
    round five-minute boundaries. Not wrong, and barely worth having."""

    def _beats(self, shots, seconds=5.0, se="S03E13", **extra):
        return [{"beat": 1, "shots": [
            dict({"source": "Breaking Bad", "season_episode": se,
                  "visual": f"shot {i}", "duration_target_sec": seconds},
                 **extra) for i in range(shots)]}]

    def test_a_seven_minute_window_for_thirty_seconds_of_video_is_named(self):
        beats = self._beats(6)
        said = timings.parse_lines("S03E13 40:00-47:00")
        got = timings.too_wide(beats, said)
        self.assertEqual(len(got), 1)
        _ratio, label, shots, room, wanted = got[0]
        self.assertIn("S03E13", label)
        self.assertEqual(shots, 6)
        self.assertAlmostEqual(room, 420.0)
        self.assertAlmostEqual(wanted, 30.0)

    def test_a_range_that_fits_the_run_is_left_alone(self):
        """S04E01: 88 shots, seven minutes of footage, a ten-minute window.
        Loose, but it is placing the run rather than spreading it."""
        beats = self._beats(88, se="S04E01")
        said = timings.parse_lines("S04E01 29:30-40:00")
        self.assertEqual(timings.too_wide(beats, said), [])

    def test_the_worst_offender_comes_first(self):
        beats = (self._beats(2, se="S04E12")
                 + [{"beat": 2, "shots": [
                     {"source": "Breaking Bad", "season_episode": "S04E13",
                      "visual": f"x {i}", "duration_target_sec": 5}
                     for i in range(14)]}])
        said = timings.parse_lines("S04E12 30:00-45:00\nS04E13 20:00-30:00")
        got = timings.too_wide(beats, said)
        self.assertIn("S04E12", got[0][1])

    def test_nothing_stated_means_nothing_to_complain_about(self):
        self.assertEqual(timings.too_wide(self._beats(6), []), [])


class TestWhenAQuotedLineContradictsAStatedTime(unittest.TestCase):
    """A stated time outranks every guess in this package. It does not
    outrank a measurement, and a line matched in the real subtitle file is
    one. On a real script four of five model-written ranges were wrong by
    seven to fifteen minutes, and the build was only good because alignment
    quietly used the lines instead — while the log said "nothing will look
    elsewhere" the whole time."""

    def _run(self, shots=6):
        return [{"beat": 1, "shots": [
            {"source": "Breaking Bad", "season_episode": "S04E01",
             "visual": f"shot {i}", "duration_target_sec": 5}
            for i in range(shots)]}]

    def _places(self, anchor_at=None):
        out = [align.Placement(beat=1, shot=i + 1, path="/lib/ep.mkv",
                               start_ms=1_800_000 + i * 5000,
                               end_ms=1_805_000 + i * 5000,
                               method="interpolated") for i in range(6)]
        if anchor_at is not None:
            out[2].method = "anchor"
            out[2].start_ms = int(anchor_at * 1000)
            out[2].end_ms = out[2].start_ms + 3000
        return out

    def _windows(self, span, shots=6):
        return {(1, i + 1): span for i in range(shots)}

    def test_a_window_the_line_contradicts_is_dropped_and_named(self):
        said = []
        windows = self._windows((2400.0, 2760.0))     # "40:00-46:00"
        got = timings.honour(self._run(), self._places(anchor_at=1836.0),
                             windows, log=said.append)
        self.assertEqual(got, {})
        self.assertTrue(any("30:36" in s for s in said), said)

    def test_a_window_the_line_agrees_with_is_kept(self):
        windows = self._windows((1800.0, 2280.0))     # "30:00-38:00"
        got = timings.honour(self._run(), self._places(anchor_at=1900.0),
                             windows)
        self.assertEqual(got, windows)

    def test_a_run_with_no_quoted_line_keeps_whatever_was_stated(self):
        """Nothing was measured, so there is nothing to contradict — and
        this is the case the whole feature exists for."""
        windows = self._windows((2400.0, 2760.0))
        got = timings.honour(self._run(), self._places(), windows)
        self.assertEqual(got, windows)

    def test_no_stated_windows_at_all_is_not_a_crash(self):
        self.assertEqual(timings.honour(self._run(), self._places(), {}), {})
        self.assertEqual(timings.honour(self._run(), self._places(), None), {})


class TestWhatCanBeGivenATime(unittest.TestCase):

    def test_a_press_portrait_is_never_asked_for_a_timecode(self):
        """It was appearing as `unknown 29:30-33:40 — koi timing nahi`,
        asking for the timecode of a photograph."""
        beats = [{"beat": 1, "shots": [
            {"source": "Vince Gilligan press portrait", "type": "real_world",
             "season_episode": "", "visual": "a man at a desk"}]},
            {"beat": 2, "shots": [
                {"source": "Breaking Bad", "season_episode": "S04E01",
                 "visual": "a lab"}]}]
        left = timings.unstated(beats, [])
        self.assertEqual(len(left), 1)
        self.assertIn("S04E01", left[0][1])

    def test_each_run_is_named_once_however_many_ways_it_was_stated(self):
        """The script's range and the typed line are both `stated`, and the
        pre-flight was listing every run twice because of it."""
        beats = [{"beat": 1, "shots": [
            {"source": "Breaking Bad", "season_episode": "S03E13",
             "visual": f"shot {i}", "duration_target_sec": 5}
            for i in range(6)]}]
        said = (timings.parse_lines("S03E13 40:00-47:00")
                + timings.parse_lines("S03E13 40:00-47:00"))
        self.assertEqual(len(timings.too_wide(beats, said)), 1)


class TestTimingsTheBuildWorksOutForItself(unittest.TestCase):
    """The answer to "how will I know the times for the next video". Mostly
    you will not have to: a run that quoted a line has already said where it
    is, exactly, and that can be written back in the box's own form."""

    def _beats(self, shots=6, se="S04E01"):
        return [{"beat": 1, "shots": [
            {"source": "Breaking Bad", "season_episode": se,
             "visual": f"shot {i}", "duration_target_sec": 5}
            for i in range(shots)]}]

    def _places(self, anchors_at=()):
        out = [align.Placement(beat=1, shot=i + 1, path="/lib/ep.mkv",
                               start_ms=1_800_000, end_ms=1_805_000,
                               method="interpolated") for i in range(6)]
        for i, at in anchors_at:
            out[i].method = "anchor"
            out[i].start_ms = int(at * 1000)
            out[i].end_ms = out[i].start_ms + 3000
        return out

    def test_the_line_is_the_span_of_the_lines_that_matched(self):
        got = timings.derive(self._beats(),
                             self._places([(1, 1836.0), (4, 2280.0)]))
        self.assertEqual(len(got), 1)
        shots, line, count = got[0]
        self.assertEqual(shots, 6)
        self.assertEqual(count, 2)
        self.assertEqual(line, "S04E01 29:36-39:00")     # one minute either side

    def test_a_run_with_no_matched_line_produces_nothing_to_paste(self):
        self.assertEqual(timings.derive(self._beats(), self._places()), [])

    def test_a_press_portrait_never_produces_a_line(self):
        beats = [{"beat": 1, "shots": [
            {"source": "Vince Gilligan press portrait", "type": "real_world",
             "season_episode": "", "visual": "a man at a desk"}]}]
        places = [align.Placement(beat=1, shot=1, path="x", start_ms=1000,
                                  end_ms=4000, method="anchor")]
        self.assertEqual(timings.derive(beats, places), [])

    def test_the_biggest_run_comes_first(self):
        beats = (self._beats(shots=6, se="S03E13")
                 + [{"beat": 2, "shots": [
                     {"source": "Breaking Bad", "season_episode": "S04E01",
                      "visual": f"x {i}", "duration_target_sec": 5}
                     for i in range(20)]}])
        places = self._places([(0, 1790.0)]) + [
            align.Placement(beat=2, shot=1, path="/lib/a.mkv",
                            start_ms=1_836_000, end_ms=1_839_000,
                            method="anchor")]
        got = timings.derive(beats, places)
        self.assertEqual(got[0][0], 20)
        self.assertIn("S04E01", got[0][1])


class TestABeatThatDrawsFromSeveralEpisodes(unittest.TestCase):
    """The bug that produced "kuch bhi clips crop ho rahi hai".

    Windows used to be keyed by BEAT. A beat routinely draws from several
    episodes — on a real 34-beat script, **24 of the 34 did** — so the last
    episode processed silently overwrote every other episode's window in
    that beat.

    Measured on that build: S04E01 was told 5:00-8:00 and S03E01 was told
    10:00-15:00, and both runs were laid out at 39.7 minutes, because
    S03E13's window (38:00-42:00) had been written into the beats they
    shared. Three episodes, one window, two of them completely wrong.
    """

    def _beats(self):
        # One beat, three episodes — exactly the shape that broke.
        return [{"beat": 1, "shots": [
            {"source": "Breaking Bad", "season_episode": se,
             "visual": f"a shot of {se}", "duration_target_sec": 5}
            for se in ("S04E01", "S03E01", "S03E13")]}]

    def test_each_episode_keeps_its_own_window(self):
        said = timings.parse_lines("S04E01 5:00-8:00\n"
                                   "S03E01 10:00-15:00\n"
                                   "S03E13 38:00-42:00")
        got = timings.windows_for(self._beats(), said)
        self.assertEqual(got[(1, 1)], (300.0, 480.0))       # S04E01
        self.assertEqual(got[(1, 2)], (600.0, 900.0))       # S03E01
        self.assertEqual(got[(1, 3)], (2280.0, 2520.0))     # S03E13

    def test_no_episode_can_overwrite_another(self):
        """The whole point: three windows in, three windows out."""
        said = timings.parse_lines("S04E01 5:00-8:00\n"
                                   "S03E01 10:00-15:00\n"
                                   "S03E13 38:00-42:00")
        got = timings.windows_for(self._beats(), said)
        self.assertEqual(len(set(got.values())), 3)

    def test_pacing_uses_the_right_episode_for_each_shot(self):
        from media_index import verify

        beats = [{"beat": 1, "shots": [
            {"source": "Breaking Bad", "season_episode": "S04E01",
             "visual": f"a {i}", "duration_target_sec": 5} for i in range(5)]
            + [{"source": "Breaking Bad", "season_episode": "S03E01",
                "visual": f"b {i}", "duration_target_sec": 5}
               for i in range(5)]}]
        places = ([align.Placement(beat=1, shot=i + 1, path="/lib/e401.mkv",
                                   start_ms=0, end_ms=5000, method="none")
                   for i in range(5)]
                  + [align.Placement(beat=1, shot=i + 6, path="/lib/e301.mkv",
                                     start_ms=0, end_ms=5000, method="none")
                     for i in range(5)])
        said = timings.parse_lines("S04E01 5:00-8:00\nS03E01 40:00-45:00")
        verify.pace_runs("db", beats, places,
                         timings.windows_for(beats, said))
        first = [p.start_ms / 1000.0 for p in places[:5]]
        second = [p.start_ms / 1000.0 for p in places[5:]]
        self.assertTrue(all(300.0 <= at <= 480.0 for at in first), first)
        self.assertTrue(all(2400.0 <= at <= 2700.0 for at in second), second)
