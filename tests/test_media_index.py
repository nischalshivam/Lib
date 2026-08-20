"""Tests for the dialogue index. Stdlib unittest — no pytest needed.

    cd shared && python -m unittest discover tests -v
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from media_index import library, naming, search, subtitles          # noqa: E402
from media_index.demo import make_demo_library                      # noqa: E402


class TestNaming(unittest.TestCase):
    def test_episode_release_name(self):
        m = naming.parse("/m/Iron Harvest/Season 01/"
                         "Iron.Harvest.S01E01.1080p.WEB-DL.x265-KOGi.mkv")
        self.assertEqual((m.kind, m.show, m.season, m.episode),
                         ("episode", "Iron Harvest", 1, 1))

    def test_last_word_of_title_survives(self):
        """Regression: a greedy release-group strip turned 'The Long Winter'
        into 'The' and 'Iron Harvest' into 'Iron'."""
        m = naming.parse("/m/The Long Winter (2019)/"
                         "The.Long.Winter.2019.2160p.UHD.BluRay.x265-TERMiNAL.mkv")
        self.assertEqual(m.kind, "movie")
        self.assertEqual(m.show, "The Long Winter")
        self.assertEqual(m.year, 2019)

    def test_x_notation(self):
        m = naming.parse("/m/Iron Harvest/Season 02/"
                         "Iron Harvest - 2x01 - Frost Line.mkv")
        self.assertEqual((m.season, m.episode), (2, 1))
        self.assertEqual(m.show, "Iron Harvest")

    def test_season_word_notation(self):
        m = naming.parse("/m/Show/Season 3/Show Season 3 Episode 12.mkv")
        self.assertEqual((m.season, m.episode), (3, 12))

    def test_title_recovered_from_folder(self):
        m = naming.parse("/m/Breaking Bad/Season 01/S01E01.mkv")
        self.assertEqual(m.show, "Breaking Bad")


class TestSubtitleParsing(unittest.TestCase):
    def test_srt_tags_and_labels_stripped(self):
        text = make_demo_library.srt([
            (1000, 2000, "<i>Italic line.</i>"),
            (3000, 4000, "MARLOW: With a speaker label."),
            (5000, 6000, "[DOOR SLAMS]"),
            (7000, 8000, "♪ ♪"),
        ])
        cues = subtitles.parse_srt(text)
        kept = [c.text for c in cues]
        self.assertIn("Italic line.", kept)
        self.assertIn("With a speaker label.", kept)
        # pure sound-effect / music cues carry no dialogue and are dropped
        self.assertEqual(len(kept), 2)

    def test_ass_parsing(self):
        text = make_demo_library.ass([(61_500, 64_000, "A line from an ASS file.")])
        cues = subtitles.parse_ass(text)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0].text, "A line from an ASS file.")
        self.assertEqual(cues[0].start_ms, 61_500)

    def test_multiline_cue_is_joined(self):
        text = "1\n00:00:10,000 --> 00:00:13,000\nfirst half\nsecond half\n"
        cues = subtitles.parse_srt(text)
        self.assertEqual(cues[0].text, "first half second half")


class TestNormalize(unittest.TestCase):
    def test_contractions_expand_both_ways(self):
        self.assertEqual(library.normalize("The cold doesn't negotiate."),
                         library.normalize("The cold does not negotiate"))
        self.assertEqual(library.normalize("I can't"), library.normalize("I can not"))
        self.assertEqual(library.normalize("We'll go"), library.normalize("We will go"))

    def test_accents_and_quotes(self):
        self.assertEqual(library.normalize("Café — “done”"), "cafe done")


class _LibraryCase(unittest.TestCase):
    """Shared fixture: a built demo library."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="mediaindex_test_")
        cls.media = os.path.join(cls.tmp, "media")
        cls.db = os.path.join(cls.tmp, "library.db")
        make_demo_library.build(cls.media)
        library.build(cls.media, cls.db, log=lambda *a: None)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)


class TestIndexBuild(_LibraryCase):
    def test_all_files_identified(self):
        st = library.stats(self.db)
        self.assertEqual(st["media_files"], 5)
        self.assertEqual(st["with_subs"], 4)
        self.assertEqual(st["without_subs"], 1)   # 2x02 genuinely has none

    def test_shared_subs_folder_not_misattributed(self):
        """Regression: a Subs/ folder shared by a season handed episode 1's
        subtitles to every other episode in that folder."""
        con = library.connect(self.db)
        row = con.execute(
            "SELECT cue_count FROM media WHERE path LIKE '%2x02%'").fetchone()
        con.close()
        self.assertEqual(row["cue_count"], 0)

    def test_rescan_is_incremental(self):
        res = library.build(self.media, self.db, log=lambda *a: None)
        self.assertEqual(res.added, 0)
        self.assertEqual(res.updated, 0)
        self.assertEqual(res.skipped, 5)


class TestSearch(_LibraryCase):
    def test_quote_split_across_two_cues(self):
        """The case naive cue-by-cue matching cannot solve."""
        hits = search.find(self.db,
                           "I never wanted the harvest. I wanted the land it grew on.")
        self.assertTrue(hits)
        h = hits[0]
        self.assertEqual((h.season, h.episode), (1, 1))
        self.assertEqual(h.confidence, "high")
        self.assertEqual(h.start_ms, 842_300)      # first cue of the pair
        self.assertEqual(h.end_ms, 848_200)        # second cue of the pair

    def test_window_never_spans_silence(self):
        """Regression: merging cues that are consecutive by INDEX but minutes
        apart in TIME produced a nine-minute 'clip'."""
        hits = search.find(
            self.db, "a debt is not a rope its a road it goes in both directions")
        self.assertTrue(hits)
        span_ms = hits[0].end_ms - hits[0].start_ms
        self.assertLess(span_ms, search.MAX_WINDOW_MS)

    def test_contraction_misquote_still_high(self):
        hits = search.find(self.db, "The cold does not negotiate")
        self.assertEqual(hits[0].confidence, "high")
        self.assertEqual(hits[0].show, "The Long Winter")

    def test_ambiguous_line_returns_both(self):
        hits = search.find(self.db, "Then we burn the field")
        strong = [h for h in hits if h.confidence == "high"]
        self.assertGreaterEqual(len(strong), 2)
        self.assertEqual({(h.season, h.episode) for h in strong}, {(1, 1), (1, 2)})

    def test_scoping_disambiguates(self):
        hits = search.find(self.db, "Then we burn the field", season=1, episode=2)
        self.assertEqual((hits[0].season, hits[0].episode), (1, 2))

    def test_wrong_wording_downgrades_not_lies(self):
        """A misquote should still find the line, but must NOT claim high
        confidence — that is what routes it to human review."""
        hits = search.find(self.db, "Nobody leaves Kessler County clean")
        self.assertTrue(hits)
        self.assertEqual(hits[0].start_ms, 2_101_000)
        self.assertEqual(hits[0].confidence, "medium")

    def test_nonsense_finds_nothing(self):
        hits = search.find(self.db, "quantum banana helicopter protocol")
        self.assertEqual(hits, [])

    def test_cut_window_has_air(self):
        h = search.find(self.db, "The cold does not negotiate")[0]
        a, b = h.cut_window()
        self.assertLess(a, h.start_ms)
        self.assertGreater(b, h.end_ms)
        self.assertGreaterEqual(a, 0)


class TestResolveScript(_LibraryCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import json
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(here, "..", "media_index", "demo", "demo_script.json")
        with open(path, encoding="utf-8") as f:
            cls.beats = json.load(f)
        cls.rows = search.resolve_script(cls.db, cls.beats)

    def _status(self, beat, shot=1):
        return next(r.status for r in self.rows
                    if r.beat == beat and r.shot == shot)

    def test_exact_quotes_resolve(self):
        self.assertEqual(self._status(1), "resolved")
        self.assertEqual(self._status(2), "resolved")

    def test_montage_becomes_two_shots(self):
        beat5 = [r for r in self.rows if r.beat == 5]
        self.assertEqual(len(beat5), 2)
        self.assertTrue(all(r.status == "resolved" for r in beat5))

    def test_wrong_episode_hint_is_corrected(self):
        row = next(r for r in self.rows if r.beat == 3)
        self.assertEqual(row.status, "resolved")
        self.assertEqual((row.hit.season, row.hit.episode), (2, 1))
        self.assertIn("library", row.note)

    def test_repeated_line_flagged_ambiguous(self):
        self.assertEqual(self._status(4), "ambiguous")

    def test_misquote_flagged_weak(self):
        self.assertEqual(self._status(6), "weak")

    def test_silent_shot_uses_nearest_dialogue(self):
        row = next(r for r in self.rows if r.beat == 7)
        self.assertIn(row.status, ("resolved", "weak"))
        self.assertIn("verify", row.note)

    def test_missing_title_reported_not_guessed(self):
        self.assertEqual(self._status(8), "not_found")

    def test_no_dialogue_routed_to_visual_search(self):
        self.assertEqual(self._status(9), "no_query")

    def test_nothing_is_silently_wrong(self):
        """Every shot ends up in a known bucket — none can slip through."""
        allowed = {"resolved", "ambiguous", "weak", "not_found", "no_query"}
        self.assertTrue(all(r.status in allowed for r in self.rows))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestReindexWhenSubtitlesChange(unittest.TestCase):
    """Replacing a bad .srt must actually take effect.

    The skip check compared only the video's size and mtime, so swapping in a
    corrected subtitle and rebuilding reported "skipped 13" and changed
    nothing. The repair was applied and silently ignored, and the index kept
    serving the timings that were wrong — the exact failure this tool exists
    to prevent, produced by the tool itself.
    """
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "Show Season 1")
        make_demo_library.build(self.root)
        self.db = os.path.join(self.tmp, "library.db")
        self.srts = [os.path.join(dp, f)
                     for dp, _d, fs in os.walk(self.root) for f in fs
                     if f.lower().endswith(".srt")]

    def test_an_unchanged_library_is_skipped(self):
        library.build(self.root, self.db, log=lambda *a: None)
        again = library.build(self.root, self.db, log=lambda *a: None)
        self.assertEqual(again.added, 0)
        self.assertGreater(again.skipped, 0)

    def test_a_rewritten_subtitle_is_picked_up(self):
        library.build(self.root, self.db, log=lambda *a: None)
        with open(self.srts[0], "a", encoding="utf-8") as f:
            f.write("\n999\n00:40:00,000 --> 00:40:02,000\n"
                    "a line that was not there before\n\n")
        after = library.build(self.root, self.db, log=lambda *a: None)
        self.assertEqual(after.updated, 1, "the new subtitle was ignored")

    def test_the_new_line_is_searchable(self):
        """Counting the rebuild is not enough — the line has to be findable."""
        library.build(self.root, self.db, log=lambda *a: None)
        with open(self.srts[0], "a", encoding="utf-8") as f:
            f.write("\n999\n00:40:00,000 --> 00:40:02,000\n"
                    "a line that was not there before\n\n")
        library.build(self.root, self.db, log=lambda *a: None)
        hits = search.find(self.db, "a line that was not there before")
        self.assertTrue(hits)
        self.assertEqual(hits[0].start_ms, 2_400_000)
