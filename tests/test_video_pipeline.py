"""Tests for the parts that touch real video: sync detection and cutting.

These render a small video with ffmpeg and are skipped when ffmpeg is absent,
so the suite still runs on a machine without it.

    cd shared && python -m unittest discover tests -v
"""
from __future__ import annotations

import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from media_index import cutter, library, probe, search, subtitles, sync   # noqa: E402
from media_index.demo import make_combined_demo as cdemo               # noqa: E402
from media_index.demo import make_demo_video as dv                        # noqa: E402

HAVE_FFMPEG = probe.ffmpeg_bin() is not None
skip_no_ffmpeg = unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")


def cues_from(pairs, offset_ms=0, scale=1.0):
    return [subtitles.Cue(i, int(a * scale) + offset_ms,
                          int(b * scale) + offset_ms, t)
            for i, (a, b, t) in enumerate(pairs)]


@skip_no_ffmpeg
class TestProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="probe_")
        cls.vid = dv.build(os.path.join(cls.tmp, "v.mkv"), log=lambda *a: None)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_reads_basic_facts(self):
        info = probe.probe(self.vid)
        self.assertAlmostEqual(info.duration, dv.DURATION, delta=0.5)
        self.assertEqual((info.width, info.height), (dv.WIDTH, dv.HEIGHT))
        self.assertTrue(info.has_audio)

    def test_ffmpeg_fallback_matches_ffprobe(self):
        """The stderr parser is the path used when ffprobe is missing."""
        info = probe._probe_with_ffmpeg(self.vid)
        self.assertAlmostEqual(info.duration, dv.DURATION, delta=0.5)
        self.assertEqual((info.width, info.height), (dv.WIDTH, dv.HEIGHT))

    def test_unreadable_file_raises(self):
        bad = os.path.join(self.tmp, "not_a_video.mkv")
        with open(bad, "wb") as f:
            f.write(b"garbage" * 100)
        with self.assertRaises(probe.ProbeError):
            probe.probe(bad)


@skip_no_ffmpeg
class TestSyncDetector(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sync_")
        cls.vid = dv.build(os.path.join(cls.tmp, "v.mkv"), write_srt=False,
                           log=lambda *a: None)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _check(self, planted_ms, tolerance_ms=150):
        cues = cues_from(dv.CUES, offset_ms=planted_ms)
        r = sync.detect(self.vid, cues, try_framerates=False)
        # the detector reports the correction, i.e. the negative of the drift
        self.assertLessEqual(abs(r.offset_ms + planted_ms), tolerance_ms,
                             f"planted {planted_ms}, detected {r.offset_ms}")
        return r

    def test_detects_late_subtitles(self):
        self.assertEqual(self._check(3000).confidence, "high")

    def test_detects_early_subtitles(self):
        self.assertEqual(self._check(-2500).confidence, "high")

    def test_detects_small_drift(self):
        self._check(250)

    def test_in_sync_reports_in_sync(self):
        r = self._check(0)
        self.assertTrue(r.in_sync)

    def test_survives_unsubtitled_audio(self):
        """Music and sound effects appear in the audio but not the subtitles."""
        extra = sorted(dv.CUES + [(18_000, 20_500, "x"), (38_000, 40_000, "x"),
                                  (68_000, 71_000, "x"), (100_000, 103_000, "x")])
        vid = dv.build(os.path.join(self.tmp, "noisy.mkv"), cues=extra,
                       write_srt=False, log=lambda *a: None)
        r = sync.detect(vid, cues_from(dv.CUES, offset_ms=-4500),
                        try_framerates=False)
        self.assertLessEqual(abs(r.offset_ms - 4500), 150)
        self.assertEqual(r.confidence, "high")

    def test_stretch_is_refused_when_there_is_too_little_to_measure(self):
        """Two minutes of sparse audio cannot support a stretch measurement.

        Three lines per window match themselves one exchange over as well as
        they match the truth. The required answer is a refusal that says so —
        not a confident stretch, which would be perfect at the start and
        minutes out by the end.
        """
        cues = cues_from(dv.CUES, offset_ms=1200, scale=25.0 / 23.976)
        r = sync.detect(self.vid, cues, try_framerates=True)
        self.assertEqual(r.scale, 1.0, "claimed a stretch it could not measure")
        # A single shift does fit the middle of a stretched track, so this is
        # not a total mismatch — but it is 2.5 s out at both ends, and must
        # never be reported with the confidence of a track that really fits.
        self.assertNotEqual(r.confidence, "high")

    def test_wrong_subtitles_are_not_trusted(self):
        """The safety case: subtitles from another film must not be applied.

        Asserted on the verdict rather than on prominence. Prominence is
        still reported, but it was measured at 0.20-0.30 on this synthetic
        audio and 0.00-0.05 on real film for matches of the same quality, so
        no threshold over it separates a good fit from a bad one.
        """
        other = [(t * 1000, t * 1000 + 2500, "unrelated")
                 for t in (3, 17, 29, 44, 58, 71, 88, 99, 111)]
        r = sync.detect(self.vid, cues_from(other), try_framerates=False)
        self.assertEqual(r.confidence, "low")
        self.assertTrue(r.note, "a refusal has to say why")

    def test_one_season_gets_one_answer(self):
        """Thirteen copies of a problem must not get four different diagnoses.

        This is the failure that made the rewrite necessary. Searching nine
        framerate ratios and keeping the highest score gave four different
        conversions across one season of one download — and `24to25` alone
        displaces the end of an episode by two minutes.
        """
        verdicts = set()
        for shift in (-2500, -1200, 0, 900, 3000):
            r = sync.detect(self.vid, cues_from(dv.CUES, offset_ms=shift),
                            try_framerates=True)
            verdicts.add(r.scale_name)
        self.assertEqual(verdicts, {"none"},
                         f"same source, same framerate, but got {verdicts}")

    def test_a_fit_that_runs_past_the_end_is_refused(self):
        """A stretch is perfect at the start and minutes out by the end, so
        the first line anyone tests looks right either way. Running off the
        end of the file is one thing that can be checked without watching."""
        long_cues = cues_from(dv.CUES + [(600_000, 604_000, "way past the end")])
        r = sync.detect(self.vid, long_cues, try_framerates=True)
        moved = sync.apply(long_cues, r.offset_ms, r.scale)
        self.assertLess(moved[-1].end_ms, 600_000 + 120_000)

    def test_apply_does_not_mutate_input(self):
        cues = cues_from(dv.CUES)
        before = cues[0].start_ms
        sync.apply(cues, 5000, 1.0)
        self.assertEqual(cues[0].start_ms, before)


@skip_no_ffmpeg
class TestCutter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="cut_")
        cls.vid = dv.build(os.path.join(cls.tmp, "v.mkv"), log=lambda *a: None)
        cls.boundaries = cutter.detect_shots(cls.vid)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_finds_most_shot_boundaries(self):
        truth = dv.scene_cut_times()
        found = [c for c in truth
                 if any(abs(b - c) < 0.5 for b in self.boundaries)]
        # ffmpeg's scene score misses low-contrast cuts; the pipeline must
        # cope with that rather than assume perfect detection
        self.assertGreaterEqual(len(found), len(truth) - 2)
        for b in self.boundaries:
            self.assertTrue(any(abs(b - c) < 0.5 for c in truth),
                            f"false positive at {b}")

    def test_frame_colour_matches_segment(self):
        for t in (7.0, 22.0, 52.0, 106.0):
            got = cutter.average_rgb(self.vid, t)
            want = dv.color_at(t)
            for a, b in zip(got, want):
                self.assertLessEqual(abs(a - b), 14, f"at t={t}")

    def test_snap_pulls_clip_inside_one_shot(self):
        # a request straddling the cut at 45 s
        cut = cutter.snap(self.boundaries, 43.0, 48.0, 40.0, 52.0)
        self.assertEqual(cut.crossed_shots, 0)
        self.assertGreaterEqual(cut.start, 45.0)

    def test_snap_leaves_clean_request_alone(self):
        cut = cutter.snap(self.boundaries, 46.0, 50.0, 40.0, 55.0)
        self.assertEqual((cut.start, cut.end), (46.0, 50.0))
        self.assertFalse(cut.snapped_start or cut.snapped_end)

    def test_snap_reports_when_it_cannot_fit(self):
        """A shot shorter than the minimum clip is reported, not hidden."""
        cut = cutter.snap([10.0, 11.0], 9.5, 12.0, 5.0, 15.0,
                          min_clip=3.0, max_clip=8.0)
        self.assertGreater(cut.crossed_shots, 0)
        self.assertIn("crosses", cut.note)

    def test_cut_respects_target_duration(self):
        for target in (3.0, 4.0, 5.0):
            out = os.path.join(self.tmp, f"c{target}.mp4")
            cutter.cut_clip(self.vid, 50.0, 50.0 + target, out)
            self.assertAlmostEqual(probe.probe(out).duration, target, delta=0.25)

    def test_extract_frame_writes_an_image(self):
        out = os.path.join(self.tmp, "still.jpg")
        cutter.extract_frame(self.vid, 53.0, out, width=320)
        self.assertGreater(os.path.getsize(out), 500)

    def test_empty_range_rejected(self):
        with self.assertRaises(ValueError):
            cutter.cut_clip(self.vid, 10.0, 10.0, os.path.join(self.tmp, "x.mp4"))


@skip_no_ffmpeg
class TestEndToEnd(unittest.TestCase):
    """Script quote -> correct clip on disk, with the subtitles deliberately
    mistimed so the sync correction is exercised on the way through."""

    DRIFT_MS = 3500

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="e2e_")
        root = os.path.join(cls.tmp, "media", "Iron Harvest", "Season 01")
        cls.vid = dv.build(
            os.path.join(root, "Iron.Harvest.S01E01.1080p.WEB-DL-KOGi.mkv"),
            srt_offset_ms=cls.DRIFT_MS, log=lambda *a: None)
        cls.db = os.path.join(cls.tmp, "library.db")
        library.build(os.path.join(cls.tmp, "media"), cls.db,
                      verify_sync=True, log=lambda *a: None)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_drift_was_corrected_during_indexing(self):
        con = library.connect(self.db)
        row = con.execute("SELECT sub_offset_ms, sync_conf FROM media").fetchone()
        con.close()
        self.assertEqual(row["sync_conf"], "high")
        self.assertLessEqual(abs(row["sub_offset_ms"] + self.DRIFT_MS), 150)

    def test_quote_resolves_to_true_position(self):
        hit = search.find(
            self.db, "I never wanted the harvest. I wanted the land it grew on.")[0]
        self.assertEqual(hit.confidence, "high")
        # ground truth from the generator, NOT from the (mistimed) subtitle file
        self.assertLessEqual(abs(hit.start_ms - 52_000), 200)

    def test_clip_shows_the_right_scene(self):
        hit = search.find(self.db, "I never wanted the harvest")[0]
        out = os.path.join(self.tmp, "clip.mp4")
        cut = cutter.clip_for_hit(hit, out, target_seconds=4.0)
        self.assertAlmostEqual(cut.duration, 4.0, delta=0.2)
        want = dv.color_at(53.0)
        got = cutter.average_rgb(out, cut.duration / 2)
        for a, b in zip(got, want):
            self.assertLessEqual(abs(a - b), 14,
                                 f"clip colour {got} != segment colour {want}")


@skip_no_ffmpeg
class TestCombinedSeasonFile(unittest.TestCase):
    """A single file holding several episodes — a very common download shape.

    The danger is silent: "S01E01-E07.mkv" also matches the plain "S01E01"
    pattern, so without care an entire season is filed as episode one.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="combined_")
        root = os.path.join(cls.tmp, "Iron Harvest")
        cls.vid = cdemo.build(
            os.path.join(root, "Iron_Harvest_S01_COMBINED_720p_BluRay_HEVC.mkv"),
            log=lambda *a: None)
        cls.db = os.path.join(cls.tmp, "library.db")
        cls.res = library.build(root, cls.db, log=lambda *a: None)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_not_mistaken_for_episode_one(self):
        con = library.connect(self.db)
        row = con.execute("SELECT is_combined, season FROM media").fetchone()
        con.close()
        self.assertTrue(row["is_combined"])
        self.assertEqual(row["season"], 1)

    def test_scan_warns_that_the_file_is_combined(self):
        self.assertTrue(any("several episodes" in w
                            for _, w in self.res.warnings))

    def test_chapters_were_stored(self):
        con = library.connect(self.db)
        n = con.execute("SELECT COUNT(*) FROM chapter").fetchone()[0]
        con.close()
        self.assertEqual(n, len(cdemo.EPISODES))

    def test_hit_names_its_episode(self):
        hit = search.find(self.db, "I came back for the people on it")[0]
        self.assertTrue(hit.is_combined)
        self.assertEqual(hit.chapter_index, 2)          # third episode
        self.assertIn("E03", hit.label)

    def test_timecode_is_reported_within_the_episode(self):
        hit = search.find(self.db, "I came back for the people on it")[0]
        # 52 s into episode 3, which itself starts two episodes in
        self.assertAlmostEqual(hit.chapter_offset_ms, 52_000, delta=1500)
        self.assertAlmostEqual(hit.start_ms, 2 * cdemo.EPISODE_SECONDS * 1000 + 52_000,
                               delta=1500)

    def test_recap_and_original_are_both_found(self):
        """Every episode opens with a recap, so the same line really does
        occur more than once inside one file. Keeping only the best hit per
        file hid the second one entirely."""
        hits = search.find(self.db, "I never wanted the harvest", limit=4)
        chapters = {h.chapter_index for h in hits if h.confidence == "high"}
        self.assertIn(0, chapters)      # the original, in episode 1
        self.assertIn(1, chapters)      # the recap, in episode 2

    def test_cut_from_a_combined_file_lands_correctly(self):
        hit = search.find(self.db, "I came back for the people on it")[0]
        out = os.path.join(self.tmp, "clip.mp4")
        cut = cutter.clip_for_hit(hit, out, target_seconds=4.0)
        want = dv.color_at(53.0)                 # colour 53 s into any episode
        got = cutter.average_rgb(out, cut.duration / 2)
        for a, b in zip(got, want):
            self.assertLessEqual(abs(a - b), 14)


class TestSubtitleScript(unittest.TestCase):
    """Hindi subtitles indexed against an English script match nothing, with
    no explanation — unless we notice and say so."""

    def _cues(self, text):
        return [subtitles.Cue(0, 0, 1000, text)]

    def test_detects_latin(self):
        self.assertEqual(
            subtitles.detect_script(self._cues("I never wanted the harvest")),
            "latin")

    def test_detects_devanagari(self):
        self.assertEqual(
            subtitles.detect_script(self._cues("मैंने कभी फ़सल नहीं चाही थी")),
            "devanagari")

    def test_romanised_hindi_reads_as_latin(self):
        self.assertEqual(
            subtitles.detect_script(self._cues("Maine kabhi fasal nahi chahi")),
            "latin")

    def test_detects_cjk(self):
        self.assertEqual(
            subtitles.detect_script(self._cues("私は収穫を望んでいなかった")), "cjk")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestDriftMeasurement(unittest.TestCase):
    """The stretch maths, on an episode-length timeline, without ffmpeg.

    The demo video is two minutes long, which is far too short to say
    anything about framerate conversion. These build the speech timeline
    directly, at the length and density of a real episode: ~600 lines of
    irregularly spaced dialogue across 45 minutes, which is what the real
    Breaking Bad season looks like.
    """
    DURATION_S = 45 * 60
    BIN = sync.FINE_BIN_MS

    def _timeline(self, seed=7):
        """[(start_s, end_s)] of speech, irregular on purpose."""
        rnd = random.Random(seed)
        out, t = [], 12.0
        while t < self.DURATION_S - 30:
            length = rnd.uniform(0.8, 3.4)
            out.append((t, t + length))
            t += length + rnd.uniform(0.4, 6.0)
        return out

    def _cues(self, speech, offset_ms=0, scale=1.0):
        return [subtitles.Cue(idx=i, start_ms=int(a * 1000 * scale) + offset_ms,
                    end_ms=int(b * 1000 * scale) + offset_ms, text="line")
                for i, (a, b) in enumerate(speech, 1)]

    def _measure(self, offset_ms=0, scale=1.0):
        speech = self._timeline()
        n_bins = int(self.DURATION_S * 1000) // self.BIN + 2
        n_coarse = int(self.DURATION_S * 1000) // sync.COARSE_BIN_MS + 2
        fine = sync._bits_from_intervals(speech, self.BIN, n_bins)
        coarse = sync._bits_from_intervals(speech, sync.COARSE_BIN_MS, n_coarse)
        cues = self._cues(speech, offset_ms, scale)
        return sync.measure_drift(coarse, fine, cues, n_coarse, n_bins,
                                  float(self.DURATION_S), 0)

    def test_enough_lines_to_measure(self):
        self.assertGreater(len(self._cues(self._timeline())), 400)

    def test_a_clean_track_is_left_alone(self):
        scale, offset, residual, _e, _l = self._measure()
        self.assertEqual(scale, 1.0)
        self.assertLess(abs(offset), 150)
        self.assertLess(residual, 150)

    def test_a_constant_offset_is_not_mistaken_for_stretch(self):
        """The failure that corrupted the real index: a plain shift being
        read as a framerate conversion, which then bends the whole episode."""
        for shift in (-8000, -2500, 1500, 6000):
            scale, offset, residual, _e, _l = self._measure(offset_ms=shift)
            self.assertEqual(scale, 1.0, f"{shift} ms read as {scale}")
            # the detector reports the correction, i.e. the negative of the drift
            self.assertLess(abs(offset + shift), 200, shift)
            self.assertLess(residual, 200, shift)

    def test_pal_speedup_is_measured(self):
        """24->25 displaces the end of an episode by two minutes.

        The reported scale is the CORRECTION, so a track running fast comes
        back as its reciprocal — that is what gets multiplied into the cues.
        """
        true_scale = 25.0 / 24.0
        scale, offset, residual, _e, _l = self._measure(
            offset_ms=1200, scale=true_scale)
        self.assertAlmostEqual(scale, 1.0 / true_scale, places=4)
        # the correction undoes the planted shift after undoing the stretch
        self.assertLess(abs(offset + 1200 / true_scale), 300)
        self.assertLess(residual, 300)

    def test_ntsc_pulldown_is_measured(self):
        """23.976->24 is a thousandth — only a long lever arm reaches it."""
        true_scale = 24.0 / 23.976
        scale, offset, residual, _e, _l = self._measure(scale=true_scale)
        self.assertAlmostEqual(scale, 1.0 / true_scale, places=5)
        self.assertLess(residual, 300)

    def test_a_wrong_stretch_is_never_preferred_to_no_stretch(self):
        """Nine ratios are tried; eight of them must lose to leaving it be."""
        for shift in (-30_000, -5000, 0, 4000, 22_000):
            scale, _o, _r, _e, _l = self._measure(offset_ms=shift)
            self.assertEqual(scale, 1.0, f"{shift} ms bent the episode")

    def test_correcting_a_measured_stretch_lands_the_last_line(self):
        """The end of the episode is where a wrong stretch shows up."""
        speech = self._timeline()
        true = self._cues(speech)
        bent = self._cues(speech, offset_ms=1200, scale=25.0 / 24.0)
        scale, offset, _r, _e, _l = self._measure(
            offset_ms=1200, scale=25.0 / 24.0)
        fixed = sync.apply(bent, offset, scale)
        self.assertLess(abs(fixed[-1].start_ms - true[-1].start_ms), 500)
        self.assertLess(abs(fixed[0].start_ms - true[0].start_ms), 500)

    def test_an_unexplained_drift_is_not_snapped_to_a_framerate(self):
        """A different edit drifts too, but not by a framerate ratio. Bending
        the episode to the nearest conversion would invent a correction."""
        scale, _o, residual, _e, _l = self._measure(scale=1.0 + 0.012)
        self.assertEqual(scale, 1.0)
        self.assertGreater(residual, 1000, "the disagreement must be reported")


@skip_no_ffmpeg
class TestScoredAudio(unittest.TestCase):
    """Audio with a score under it — which is to say, a film.

    The demo video is speech over silence, and every sync test passed on it
    for weeks. Real drama is scored end to end: music, room tone, traffic,
    weather. Almost none of that drops under a fixed -30 dB floor, so the
    "speech" timeline came back as one unbroken block, and correlating a
    solid block against subtitle cues gives the same answer at every offset.

    Measured on a real Breaking Bad season, all thirteen episodes scored
    0.43-0.68 with prominence 0.00 — which is what two unrelated signals
    score, sqrt(speech_share x cue_share). Every offset it reported was
    noise, and the season it "corrected" had never needed correcting.
    """
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="scored_")
        clean = dv.build(os.path.join(cls.tmp, "clean.mkv"), write_srt=False,
                         log=lambda *a: None)
        cls.vid = os.path.join(cls.tmp, "scored.mkv")
        subprocess.run(
            [probe.ffmpeg_bin(), "-y", "-v", "error", "-i", clean,
             "-f", "lavfi", "-t", str(int(dv.DURATION)),
             "-i", "anoisesrc=c=pink:a=0.06",
             "-filter_complex",
             "[0:a][1:a]amix=inputs=2:duration=first:normalize=0[a]",
             "-map", "0:v", "-map", "[a]", "-c:v", "copy", cls.vid],
            check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_old_measurement_cannot_see_anything_here(self):
        """Not a requirement — the reason the requirement below exists."""
        speech, analysed = sync.speech_intervals(self.vid)
        covered = sum(b - a for a, b in speech) / analysed
        self.assertGreater(covered, 0.98,
                           "this fixture is meant to defeat silencedetect")

    def test_the_offset_is_still_found_under_a_score(self):
        for planted in (-6000, -3000, 0, 2500):
            r = sync.detect(self.vid, cues_from(dv.CUES, offset_ms=planted),
                            try_framerates=False)
            self.assertEqual(r.method, "loudness")
            self.assertLessEqual(abs(r.offset_ms + planted), 200,
                                 f"planted {planted}, got {r.offset_ms}")
            self.assertEqual(r.confidence, "high", f"planted {planted}")

    def test_a_real_fit_stands_well_above_coincidence(self):
        r = sync.detect(self.vid, cues_from(dv.CUES, offset_ms=-3000),
                        try_framerates=False)
        self.assertGreater(r.lift, 2.0)

    def test_wrong_subtitles_still_refused_under_a_score(self):
        other = [(t * 1000, t * 1000 + 2500, "unrelated")
                 for t in (3, 17, 29, 44, 58, 71, 88, 99, 111)]
        r = sync.detect(self.vid, cues_from(other), try_framerates=False)
        self.assertNotEqual(r.confidence, "high")
