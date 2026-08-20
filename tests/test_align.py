"""Tests for placing shots that have no dialogue.

This is the case the dialogue index cannot reach on its own. Measured on a
real 71-beat script about the Breaking Bad box cutter scene, 92% of shots had
no dialogue at all — the scene is famous precisely because nobody speaks.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from media_index import align, cutter, library, probe, verify  # noqa: E402
from media_index.demo import make_demo_video as dv             # noqa: E402

HAVE_FFMPEG = probe.ffmpeg_bin() is not None
skip_no_ffmpeg = unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")


def shot(source="Iron Harvest", se="S04E01", dialogue="", target=3.0):
    return {"source": source, "season_episode": se,
            "exact_dialogue": dialogue, "visual": "x",
            "duration_target_sec": target}


def beats_from(shots):
    return [{"beat": i + 1, "narration": f"n{i+1}", "shots": [s]}
            for i, s in enumerate(shots)]


class TestRuns(unittest.TestCase):
    def test_consecutive_same_episode_forms_one_run(self):
        r = align.runs(beats_from([shot(), shot(), shot()]))
        self.assertEqual(len(r), 1)
        self.assertEqual(len(r[0].entries), 3)

    def test_a_cutaway_does_not_split_the_run(self):
        """The shots either side of a cutaway are still the same walk.

        Splitting on every interruption used to leave the third S04E01 shot
        alone in a run of one, and a lone silent shot has no anchor and
        cannot be placed at all. On the real 106-shot script that produced
        36 runs, 23 of them single shots — so the cutaways were not just
        fragmenting the walk, they were deleting shots from the video.
        """
        r = align.runs(beats_from([shot(se="S04E01"), shot(se="S04E01"),
                                   shot(se="S03E13"), shot(se="S04E01")]))
        self.assertEqual([len(x.entries) for x in r], [3, 1])
        self.assertEqual(r[0].season_episode, "S04E01")
        self.assertEqual([e.beat for e in r[0].entries], [1, 2, 4])

    def test_a_different_episode_is_a_different_run(self):
        r = align.runs(beats_from([shot(se="S04E01"), shot(se="S03E13")]))
        self.assertEqual(len(r), 2)
        self.assertEqual({x.season_episode for x in r}, {"S04E01", "S03E13"})

    def test_runs_keep_the_order_they_first_appear_in(self):
        r = align.runs(beats_from([shot(se="S04E13"), shot(se="S01E01"),
                                   shot(se="S04E13")]))
        self.assertEqual([x.season_episode for x in r], ["S04E13", "S01E01"])

    def test_source_change_starts_a_new_run(self):
        r = align.runs(beats_from([shot(source="Breaking Bad"),
                                   shot(source="Better Call Saul")]))
        self.assertEqual(len(r), 2)

    def test_several_shots_in_one_beat_stay_in_order(self):
        beats = [{"beat": 1, "shots": [shot(), shot(), shot()]}]
        r = align.runs(beats)
        self.assertEqual([e.shot for e in r[0].entries], [1, 2, 3])


class TestOneEpisodeIsNotAlwaysOneScene(unittest.TestCase):
    """An essay visits the same hour twice, and those are two walks.

    From a real build, which came back 95% empty cards:

        Breaking Bad S04E01: 31 shot(s), 1 anchor(s)
        the line at shot 4 implies 398x the pace of the script around it
        two lines put this run across 25 minutes of the episode

    Those 31 shots were three parts of that episode — the cold open at
    0-3:30, Gale's apartment at 3:30-13:00, and the box cutter at
    22:00-35:00. As one run their anchors cannot all increase together, so
    `usable_anchors` dropped line after line until one was left holding all
    31 shots. Correct arithmetic on a false premise.

    The script had said so all along: five different `scene_range` values
    inside that single run.
    """

    def ranged(self, *specs):
        beats = []
        for i, (rng, n) in enumerate(specs, 1):
            shots = [shot() for _ in range(n)]
            if rng:
                shots[0]["scene_range"] = rng
            beats.append({"beat": i, "shots": shots})
        return beats

    def test_two_far_apart_ranges_in_one_episode_are_two_runs(self):
        r = align.runs(self.ranged(("27:00-35:00", 3), ("00:00-03:30", 2)))
        self.assertEqual(len(r), 2)
        self.assertEqual([len(x.entries) for x in r], [3, 2])

    def test_overlapping_ranges_stay_one_run(self):
        """19:00-24:30 and 24:00-29:30 is one stretch written in two pieces."""
        r = align.runs(self.ranged(("19:00-24:30", 2), ("24:00-29:30", 2)))
        self.assertEqual(len(r), 1)
        self.assertEqual(len(r[0].entries), 4)

    def test_returning_to_the_opening_scene_rejoins_that_run(self):
        r = align.runs(self.ranged(("27:00-35:00", 2), ("00:00-03:30", 2),
                                   ("28:00-33:00", 2)))
        self.assertEqual(len(r), 2)
        self.assertEqual(sorted(len(x.entries) for x in r), [2, 4])

    def test_shots_with_no_range_stay_in_the_sequence_in_force(self):
        r = align.runs(self.ranged(("27:00-35:00", 2), ("", 3), ("", 4)))
        self.assertEqual(len(r), 1)
        self.assertEqual(len(r[0].entries), 9)

    def test_a_script_stating_no_ranges_behaves_exactly_as_before(self):
        r = align.runs(self.ranged(("", 4), ("", 5)))
        self.assertEqual(len(r), 1)
        self.assertEqual(len(r[0].entries), 9)

    def test_the_second_sequence_says_so_in_its_name(self):
        r = align.runs(self.ranged(("27:00-35:00", 1), ("00:00-03:30", 1)))
        self.assertNotIn("scene", r[0].label)
        self.assertIn("scene 2", r[1].label)

    def test_a_different_episode_is_still_a_different_run(self):
        beats = [{"beat": 1, "shots": [shot(se="S04E01"), shot(se="S03E13")]}]
        self.assertEqual(len(align.runs(beats)), 2)


@skip_no_ffmpeg
class TestAlignWordlessScene(unittest.TestCase):
    """The real shape: a run of shots through one scene, where only the first
    and last carry any dialogue at all."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="align_")
        root = os.path.join(cls.tmp, "Iron Harvest", "Season 04")
        cls.vid = dv.build(os.path.join(root, "Iron.Harvest.S04E01.1080p.mkv"),
                           log=lambda *a: None)
        cls.db = os.path.join(cls.tmp, "library.db")
        library.build(root, cls.db, log=lambda *a: None)

        cls.beats = beats_from([
            shot(dialogue="The first line lands on the red segment"),
            shot(), shot(), shot(), shot(), shot(), shot(),
            shot(dialogue="Grey. Then we burn the field"),
        ])
        cls.places = align.align(cls.db, cls.beats, log=lambda *a: None)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _segment_at(self, seconds):
        got = cutter.average_rgb(self.vid, seconds)
        return min(range(dv.n_segments()),
                   key=lambda k: sum(abs(a - b) for a, b in
                                     zip(got, dv.segment_color(k)[2])))

    def test_everything_is_placed(self):
        self.assertEqual(len(self.places), 8)
        self.assertTrue(all(p.ok for p in self.places))

    def test_the_two_dialogue_shots_are_anchors(self):
        self.assertEqual(self.places[0].method, "anchor")
        self.assertEqual(self.places[-1].method, "anchor")
        self.assertEqual(self.places[0].confidence, "high")

    def test_every_shot_lands_on_the_right_part_of_the_scene(self):
        """Verified by sampling the frame colour, not by trusting the maths."""
        for i, p in enumerate(self.places):
            got = self._segment_at(p.start_ms / 1000 + 0.5)
            self.assertEqual(got, i,
                             f"beat {p.beat} landed on segment {got}, wanted {i}")

    def test_placements_move_forward_through_the_scene(self):
        times = [p.start_ms for p in self.places]
        self.assertEqual(times, sorted(times))

    def test_no_two_shots_land_on_the_same_moment(self):
        times = sorted(p.start_ms for p in self.places)
        for a, b in zip(times, times[1:]):
            self.assertGreater(b - a, align.MIN_SEPARATION_S * 1000 - 1)

    def test_a_run_with_no_dialogue_is_handed_on_for_the_pictures(self):
        """Not placed, but not thrown away either.

        Three runs of a real script quoted nothing anywhere, and all 28 of
        their shots were dropped whole — while the episode was named in the
        script and sitting in the library. So the file is resolved and the
        run is handed on with it, and stays unplaced until something has
        actually looked at the footage.
        """
        places = align.align(self.db, beats_from([shot(), shot(), shot()]),
                             log=lambda *a: None)
        self.assertTrue(all(not p.ok for p in places))
        self.assertTrue(all(p.path for p in places),
                        "the episode is named; its file should be resolved")
        self.assertIn("picture only", places[0].note)

    def test_a_run_naming_no_episode_cannot_even_be_handed_on(self):
        beats = beats_from([shot(se="unknown"), shot(se="unknown"),
                            shot(se="unknown")])
        places = align.align(self.db, beats, log=lambda *a: None)
        self.assertTrue(all(not p.ok and not p.path for p in places))
        self.assertIn("cannot place it", places[0].note)

    def test_a_single_shot_run_is_left_to_ordinary_search(self):
        places = align.align(self.db, beats_from([shot(dialogue="x")]),
                             log=lambda *a: None)
        self.assertIn("too short", places[0].note)

    def test_summary_reports_usable_share(self):
        text = align.summarise(self.places)
        self.assertIn("anchored", text)
        self.assertIn("100%", text)


class TestAnchorSanity(unittest.TestCase):
    def test_out_of_order_anchors_are_dropped(self):
        """An anchor that matched the wrong moment would drag everything after
        it backwards, so a crossing one is left out.

        Asserted against the real function. This used to re-implement the
        cleaning inline, which meant it went on passing after the cleaning
        itself was replaced — a test of a copy is a test of nothing.
        """
        found = [(0, 1000, 2000, "p", "high"),
                 (1, 500, 900, "p", "medium"),      # earlier than its predecessor
                 (2, 5000, 6000, "p", "high")]
        self.assertEqual([c[0] for c in align._longest_increasing(found)],
                         [0, 2])


class TestPlaceableGate(unittest.TestCase):
    """What the pre-flight gate must count.

    The gate blocked a real script at 7/106 because it counted only shots
    that matched dialogue. The builder, given the chance, places most of
    those 106 — one quoted line carries every silent shot around it. Blocking
    on the wrong number meant the tool refused to build a video it could
    have built.
    """
    def test_a_run_with_one_anchor_carries_the_whole_run(self):
        beats = beats_from([shot(dialogue="a quoted line"),
                            shot(dialogue=""), shot(dialogue=""),
                            shot(dialogue="")])
        r = align.runs(beats)
        self.assertEqual(len(r), 1)
        self.assertEqual(len(r[0].entries), 4)
        quoted = sum(1 for e in r[0].entries if e.query)
        self.assertEqual(quoted, 1, "one line has to be enough")

    def test_a_run_with_no_quoted_line_anywhere_is_hopeless(self):
        """Interpolation needs something to interpolate between."""
        r = align.runs(beats_from([shot(dialogue=""), shot(dialogue="")]))
        self.assertFalse(any(e.query for e in r[0].entries))



class TestSpanComesFromTheScript(unittest.TestCase):
    """Measured on the real run: 70 shots, 1 anchor, span 2188s-2242s.

    Fifty-four seconds for a scene the script itself describes as 254 — one
    shot every 0.77 s. Every placement landed in the same corner of the
    episode, and the contact sheet came back as the same red-lit frame over
    and over. The old code spread a one-anchor run across a fixed 45 second
    window however many shots it held, so the more the script described, the
    more tightly they were crushed together.
    """
    def _run(self, n, each=3.6):
        return align.Run("Breaking Bad", "S04E01",
                         [align.Entry(beat=i + 1, shot=1,
                                      data={"duration_target_sec": each})
                          for i in range(n)])

    def test_the_axis_is_as_long_as_the_script_says(self):
        run = self._run(70)
        ax = align.axis(run)
        self.assertAlmostEqual(ax[-1] + 1.8, 70 * 3.6, places=3)

    def test_one_anchor_still_spreads_the_whole_scene(self):
        run = self._run(70)
        scale, off = align.fit(run, [(50, 2229000, 2232000, "p", "high")])
        times = [a * scale * 1000 + off for a in align.axis(run)]
        span = (max(times) - min(times)) / 1000.0
        self.assertGreater(span, 200, f"70 shots crushed into {span:.0f}s")
        gaps = [b - a for a, b in zip(sorted(times), sorted(times)[1:])]
        self.assertGreater(min(gaps) / 1000.0, 2.0, "shots land on top of each other")

    def test_the_anchor_keeps_its_own_time(self):
        run = self._run(70)
        scale, off = align.fit(run, [(50, 2229000, 2232000, "p", "high")])
        self.assertAlmostEqual(align.axis(run)[50] * scale * 1000 + off,
                               2229000, delta=1)

    def test_two_anchors_measure_the_stretch_rather_than_assume_it(self):
        run = self._run(70)
        anchors = [(8, 2100000, 2103000, "p", "high"),
                   (50, 2229000, 2232000, "p", "high")]
        scale, off = align.fit(run, anchors)
        ax = align.axis(run)
        for i, start, _e, _p, _c in anchors:
            self.assertAlmostEqual(ax[i] * scale * 1000 + off, start, delta=1)

    def test_a_script_that_misjudges_pacing_is_not_taken_literally(self):
        """duration_target_sec is the CLIP length, not how long the moment
        lasts on screen, so a large ratio is normal and must not be clamped
        away — but an absurd one has to be."""
        run = self._run(8, each=3.0)
        scale, _off = align.fit(run, [(0, 5000, 6000, "p", "high"),
                                      (7, 105000, 106000, "p", "high")])
        self.assertGreater(scale, 3.0)
        self.assertLessEqual(scale, align.MAX_SCALE)


class TestAnchorsSurviveAMisplacedLine(unittest.TestCase):
    """The famous closing line was also quoted at beat 1 as an opener.

    Unwinding backwards from that one crossing took five anchors down to one,
    and seventy shots then hung off a single point. Keeping the longest run
    that IS in order drops the odd misplaced line instead of everything after
    it.
    """
    def test_a_line_quoted_out_of_order_costs_only_itself(self):
        found = [(0, 2229000, 2232000, "p", "high"),     # the ending, first
                 (8, 2100000, 2103000, "p", "high"),
                 (35, 2205000, 2208000, "p", "high"),
                 (50, 2229000, 2232000, "p", "high")]
        kept = align._longest_increasing(found)
        self.assertEqual([k[0] for k in kept], [8, 35, 50])

    def test_an_already_ordered_set_is_kept_whole(self):
        found = [(1, 1000, 1500, "p", "high"), (5, 4000, 4500, "p", "high"),
                 (9, 9000, 9500, "p", "high")]
        self.assertEqual(len(align._longest_increasing(found)), 3)

    def test_the_same_line_at_three_beats_yields_one_anchor(self):
        found = [(0, 2229000, 2232000, "p", "high"),
                 (50, 2229000, 2232000, "p", "high"),
                 (61, 2229000, 2232000, "p", "high")]
        self.assertEqual(len(align._longest_increasing(found)), 1)

    def test_nothing_in_means_nothing_out(self):
        self.assertEqual(align._longest_increasing([]), [])



class TestAQuoteUsedAsAHook(unittest.TestCase):
    """An essay opens by quoting its ending, then earns it.

    On the real script "Well? Get back to work." — the closing line of the
    box-cutter scene — is quoted at shot 1 as a hook and again at 51 and 62
    where it belongs. All three resolve to the same moment, 37:09. Anchoring
    on the first pinned the END of the scene to the START of the run and laid
    all seventy shots after it: the finished sheet opened on Walt hosing down
    the lab, which is what happens once the killing is over.
    """
    def _run(self, n, each=3.6):
        return align.Run("Breaking Bad", "S04E01",
                         [align.Entry(beat=i + 1, shot=1,
                                      data={"duration_target_sec": each})
                          for i in range(n)])

    def test_the_later_occurrence_wins(self):
        found = [(0, 2229000, 2232000, "p", "high"),
                 (50, 2229000, 2232000, "p", "high"),
                 (61, 2229000, 2232000, "p", "high")]
        kept = align.\
            _longest_increasing(align._last_of_each_moment(found))
        self.assertEqual([k[0] for k in kept], [61])

    def test_the_run_then_sits_before_the_line_not_after_it(self):
        run = self._run(70)
        ax = align.axis(run)
        early, late = [], []
        for idx, out in ((0, early), (61, late)):
            scale, off = align.fit(run, [(idx, 2229000, 2232000, "p", "high")])
            out += [a * scale * 1000 + off for a in ax]
        self.assertGreater(min(early) / 1000, 2225)     # starts at the line
        self.assertLess(min(late) / 1000, 2100)         # starts well before it
        self.assertLess(abs(max(late) / 1000 - 2318), 90)

    def test_distinct_moments_are_all_kept(self):
        """Only identical times collapse — two different lines are two anchors."""
        found = [(3, 1000, 1500, "p", "high"), (9, 5000, 5500, "p", "high")]
        self.assertEqual(len(align._last_of_each_moment(found)), 2)

    def test_order_is_preserved(self):
        found = [(9, 5000, 5500, "p", "high"), (3, 1000, 1500, "p", "high")]
        self.assertEqual([a[0] for a in align._last_of_each_moment(found)],
                         [3, 9])



class TestHookQuotes(unittest.TestCase):
    """A line quoted out of sequence must not decide the sequence."""

    def test_a_hook_is_recognised(self):
        e = align.Entry(beat=1, shot=1,
                        data={"exact_dialogue": "x", "hook": True})
        self.assertTrue(e.is_hook)

    def test_an_ordinary_shot_is_not_a_hook(self):
        e = align.Entry(beat=1, shot=1, data={"exact_dialogue": "x"})
        self.assertFalse(e.is_hook)

    def test_a_hook_keeps_its_quote(self):
        """It still names a real moment worth cutting."""
        e = align.Entry(beat=1, shot=1,
                        data={"exact_dialogue": "Well? Get back to work.",
                              "hook": True})
        self.assertTrue(e.query)



@skip_no_ffmpeg
class TestAnchorsStayInTheDeclaredEpisode(unittest.TestCase):
    """A run declared S04E01 must not be cut from S02E07.

    The search was filtered by SHOW and not by episode, so a quoted line
    matched wherever it happened to appear in the series. align_run cuts the
    whole run from its first anchor's file, so one stray match dragged the
    entire scene into another episode: 41 of 52 scenes in a real build came
    out of "Negro y Azul", and every number in the report looked healthy.
    """
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ep_")
        root = os.path.join(cls.tmp, "Iron Harvest", "Season 04")
        for ep in (1, 7):
            dv.build(os.path.join(root, f"Iron.Harvest.S04E{ep:02d}.1080p.mkv"),
                     log=lambda *a: None)
        cls.db = os.path.join(cls.tmp, "library.db")
        library.build(root, cls.db, log=lambda *a: None)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _anchors(self, se):
        run = align.Run("Iron Harvest", se, [
            align.Entry(beat=1, shot=1, data={
                "exact_dialogue": "Then we burn the field",
                "duration_target_sec": 3.0}),
            align.Entry(beat=2, shot=1, data={"duration_target_sec": 3.0})])
        return align.anchors_for(self.db, run)

    def test_the_anchor_comes_from_the_named_episode(self):
        """Both files hold the same line — only the named one may answer."""
        for ep in ("S04E01", "S04E07"):
            got = self._anchors(ep)
            self.assertTrue(got, f"no anchor for {ep}")
            self.assertIn(f"S04E{ep[-2:]}", got[0][3])

    def test_an_undeclared_episode_still_settles_on_one_file(self):
        """A run is cut from a single file, so anchors from another are not
        measurements of it."""
        got = self._anchors("unknown")
        self.assertTrue(got)
        self.assertEqual(len({a[3] for a in got}), 1)



class TestOneBadAnchorIsWorseThanNone(unittest.TestCase):
    """Two anchors measure the stretch between them. That is right when both
    are right, and worse than one anchor when either is not.

    On the real script a beat about the AUDIENCE — Bryan Cranston's daughter
    fainting at a screening, which happens nowhere in the episode — carried a
    quote that matched far from the scene. Fitted against a good anchor it
    gave x2.14 and spread 103 shots across 20:47-37:11 for a sequence that
    runs 33:00-37:15, so the video opened on Hank and Marie at home.
    """
    def _run(self, n=103, total=462.0):
        return align.Run("Breaking Bad", "S04E01",
                         [align.Entry(beat=i + 1, shot=1,
                                      data={"duration_target_sec": total / n})
                          for i in range(n)])

    def _span(self, anchors):
        run = self._run()
        scale, off = align.fit(run, anchors)
        times = [a * scale * 1000 + off for a in align.axis(run)]
        return (max(times) - min(times)) / 1000.0

    def test_a_stray_anchor_stretches_the_run_past_any_sequence(self):
        wide = self._span([(16, 1247000, 1250000, "p", "medium"),
                           (94, 2231000, 2234000, "p", "high")])
        self.assertGreater(wide, align.MAX_RUN_SPAN_S)

    def test_the_strongest_anchor_alone_keeps_it_plausible(self):
        one = self._span([(94, 2231000, 2234000, "p", "high")])
        self.assertLess(one, align.MAX_RUN_SPAN_S)
        self.assertAlmostEqual(one, 462.0, delta=20)

    def test_a_pair_that_agrees_is_left_alone(self):
        """The guard must not fire on anchors that really do bracket a scene."""
        near = self._span([(10, 2000000, 2003000, "p", "high"),
                           (90, 2240000, 2243000, "p", "high")])
        self.assertLess(near, align.MAX_RUN_SPAN_S)

    def test_a_run_with_no_quoted_line_keeps_its_own_length(self):
        """Five shots the script says are 22 seconds long were spread 570
        seconds apart as a "harmless placeholder". It was not harmless: the
        picture layer moved one of them and the rest inherited that spacing,
        landing at 1209s, 1778s, 2348s, 2918s and 3487s of a 2848-second
        episode — two off the end of the film, the others on a lawyer's
        office in a scene about somebody else.

        Whatever else is unknown about a run, its shots are seconds apart.
        """
        run = align.Run("Breaking Bad", "S03E01",
                        [align.Entry(beat=36 + i, shot=1,
                                     data={"duration_target_sec": 4.4})
                         for i in range(5)])
        ax = align.axis(run)
        mid, centre = ax[len(ax) // 2], 2848.0 / 2
        placed = [max(0.0, centre + (a - mid)) for a in ax]
        self.assertLess(max(placed) - min(placed), 30.0,
                        "a 22-second run was spread across the episode")
        # ...and when the pictures move one of them, the rest follow closely.
        settled = verify.interpolate(placed, ax, {2: 2348.0},
                                     verify.MIN_APART_S)
        self.assertLess(max(settled) - min(settled), 30.0)
        self.assertTrue(all(0 <= t <= 2848.0 for t in settled))

    def test_every_quoted_line_is_used_not_just_the_two_at_the_ends(self):
        """A global line through the first and last anchor ignores everything
        between them. With four quoted lines it used two — and the shots
        near the middle ones landed wherever the straight line put them
        rather than on the millisecond that was actually measured."""
        run = self._run(n=40, total=200.0)
        anchors = [(0, 1000000, 1003000, "p", "high"),
                   (20, 1200000, 1203000, "p", "high"),   # not on the line
                   (39, 1250000, 1253000, "p", "high")]
        times = align.stretch(run, anchors)
        for i, ms, *_rest in anchors:
            self.assertAlmostEqual(times[i], ms, delta=1500,
                                   msg=f"the line at shot {i + 1} was ignored")

    def test_a_wrong_line_costs_its_neighbours_and_nothing_more(self):
        """The measured failure, but with three good lines around it. The
        stray one is dropped for the rate it implies, not for how far away
        it is — and the other three keep their millisecond."""
        run = self._run(n=103, total=462.0)
        good = [(0, 1980000, 1983000, "p", "high"),
                (50, 2100000, 2103000, "p", "high"),
                (102, 2235000, 2238000, "p", "high")]
        stray = (60, 1247000, 1250000, "p", "medium")     # far, and backwards
        kept = align.usable_anchors(run, sorted(good + [stray]),
                                    log=lambda *a: None)
        self.assertNotIn(stray, kept)
        for a in good:
            self.assertIn(a, kept)

    def test_two_lines_that_disagree_still_fall_back_to_the_clearest(self):
        # With only two there is nothing to arbitrate between them, so the
        # old rule stands: past ten minutes, trust the stronger one alone.
        run = self._run()
        kept = align.usable_anchors(
            run, [(16, 1247000, 1250000, "p", "medium"),
                  (94, 2231000, 2234000, "p", "high")], log=lambda *a: None)
        self.assertEqual(len(kept), 2)      # pruning is the caller's job here


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestCountingTheQuotesTheScriptClaims(unittest.TestCase):
    """The script's own summary counts its verbatim lines. This counts the
    real ones.

    On a real build the script reported fifteen verbatim lines and six of
    them matched anything; the rest were paraphrases, close enough to read as
    quotes and not close enough to be found. Nothing said so, and the
    shortfall surfaced three stages later as a hundred-shot run hanging off a
    single anchor at its far end. The gap between "the script says it did the
    right thing" and "the right thing is in the index" has to be measured
    while the script can still be sent back and rewritten.
    """
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="quotes_")
        cls.db = os.path.join(cls.tmp, "library.db")
        con = library.connect(cls.db)
        con.execute("INSERT INTO media (id, path, kind, show, show_norm, "
                    "season, episode) VALUES (1,?,?,?,?,?,?)",
                    ("/x/Show S01E01.mkv", "episode", "Show", "show", 1, 1))
        lines = [(1000, "Well? Get back to work."),
                 (60000, "I have made a decision and there is no going back.")]
        for i, (at, text) in enumerate(lines):
            con.execute("INSERT INTO cue (media_id, idx, start_ms, end_ms, "
                        "text, text_norm) VALUES (1,?,?,?,?,?)",
                        (i, at, at + 2000, text, library.normalize(text)))
        con.commit()
        con.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _beats(self, *quotes, episode="S01E01", filler=0):
        shots = [{"source": "Show", "season_episode": episode,
                  "exact_dialogue": q, "visual": "something"} for q in quotes]
        shots += [{"source": "Show", "season_episode": episode,
                   "visual": "something"} for _ in range(filler)]
        return [{"beat": 1, "shots": shots}]

    def test_a_real_quote_is_counted_as_found(self):
        rep = align.quote_report(self.db, self._beats("Well? Get back to work."))
        self.assertEqual((rep.given, rep.matched), (1, 1))
        self.assertEqual(rep.misses, [])

    def test_a_paraphrase_is_named_rather_than_counted(self):
        rep = align.quote_report(
            self.db, self._beats("I decided there was no going back at all"))
        self.assertEqual((rep.given, rep.matched), (1, 0))
        self.assertEqual(len(rep.misses), 1)
        self.assertIn("not in the subtitles", " ".join(rep.advice()))

    def test_a_run_with_no_quote_at_all_is_named(self):
        rep = align.quote_report(self.db, self._beats(filler=6))
        self.assertEqual(rep.given, 0)
        self.assertEqual(len(rep.runs_without_anchor), 1)
        self.assertIn("no quoted line", " ".join(rep.advice()))

    def test_the_longest_unanchored_stretch_is_measured(self):
        # One quote at the front, then nine silent shots: the prompt asks for
        # one line per ten shots SPREAD, and a clump at one end satisfies the
        # count while leaving the far end with nothing to hold it.
        rep = align.quote_report(
            self.db, self._beats("Well? Get back to work.", filler=9))
        self.assertEqual(rep.matched, 1)
        self.assertEqual(rep.longest_gap, 9)

    def test_the_rate_is_what_a_gate_can_act_on(self):
        rep = align.quote_report(
            self.db, self._beats("Well? Get back to work.",
                                 "this line does not exist anywhere"))
        self.assertAlmostEqual(rep.rate, 0.5)

    def test_an_empty_script_is_not_a_crash(self):
        rep = align.quote_report(self.db, [])
        self.assertEqual((rep.given, rep.matched, rep.runs), (0, 0, 0))
        self.assertEqual(rep.advice(), [])


class TestManyAnchorsThatDisagree(unittest.TestCase):
    """`_longest_increasing` asks the wrong question once there are many
    anchors: it keeps the longest chain that runs forwards in time, which a
    handful of scattered wrong matches can win — they are in order too, just
    in order across the whole episode.

    Measured: one model wrote 48 quotes for an 88-shot run, plenty matched,
    and the pipeline kept ONE. The run then hung off that single point and
    landed nine minutes early."""

    def _run(self, shots=20):
        beats = [{"beat": 1, "shots": [
            {"source": "Show", "season_episode": "S01E01",
             "visual": f"shot {i}", "duration_target_sec": 5}
            for i in range(shots)]}]
        return align.runs(beats)[0]

    def _anchors(self, pairs):
        return [(i, int(at * 1000), int(at * 1000) + 2000, "/lib/ep.mkv",
                 "high") for i, at in pairs]

    def test_the_cluster_wins_over_a_tidy_line_of_strays(self):
        # Forty shots is 195 seconds of script, so the window this run is
        # allowed to occupy is a few minutes — the real case was 88 shots.
        run = self._run(shots=40)
        # six lines agreeing about 30-33 minutes...
        cluster = [(4, 1800.0), (6, 1830.0), (8, 1900.0), (10, 1950.0),
                   (12, 1980.0), (14, 2000.0)]
        # ...and three strays that also happen to increase
        strays = [(0, 300.0), (2, 700.0), (18, 2700.0)]
        kept = align._densest(self._anchors(sorted(cluster + strays)), run)
        times = sorted(a[1] / 1000.0 for a in kept)
        self.assertEqual(len(kept), 6)
        self.assertGreaterEqual(times[0], 1800.0)
        self.assertLessEqual(times[-1], 2000.0)

    def test_too_few_anchors_to_cluster_are_all_kept(self):
        run = self._run()
        got = self._anchors([(0, 300.0), (5, 1800.0), (9, 2500.0)])
        self.assertEqual(align._densest(got, run), got)

    def test_when_nothing_has_a_majority_everything_is_kept(self):
        """Four lines in four different places is not evidence of a scene.
        Throwing three of them away would be inventing a cluster."""
        run = self._run()
        got = self._anchors([(0, 200.0), (4, 900.0), (8, 1700.0),
                             (12, 2500.0)])
        self.assertEqual(align._densest(got, run), got)

    def test_the_whole_pipeline_keeps_the_cluster(self):
        run = self._run()
        found = align._densest(
            self._anchors([(0, 300.0), (2, 1800.0), (4, 1850.0),
                           (6, 1900.0), (8, 1950.0), (10, 2600.0)]), run)
        kept = align._longest_increasing(align._last_of_each_moment(found))
        self.assertEqual(len(kept), 4)
        self.assertTrue(all(1800.0 <= a[1] / 1000.0 <= 1950.0 for a in kept))
