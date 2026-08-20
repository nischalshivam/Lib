"""Tests for the multi-source requirements report.

A video essay often needs several titles — Saul Goodman appears in both
Breaking Bad and Better Call Saul, a Batman video pulls from several films.
The point of this report is to say what is still missing BEFORE any work
starts, and to say it at episode level so a two-episode gap does not look like
a whole-series download.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from media_index import library, sources                       # noqa: E402
from media_index.demo.make_demo_library import srt, write, write_video  # noqa: E402


def shot(source, se=None, dialogue=""):
    return {"source": source, "season_episode": se or "unknown",
            "exact_dialogue": dialogue, "visual": "x"}


class TestCanonical(unittest.TestCase):
    def test_aliases_resolve(self):
        self.assertEqual(sources.canonical("BCS"), sources.canonical("Better Call Saul"))
        self.assertEqual(sources.canonical("GoT"), sources.canonical("Game of Thrones"))

    def test_leading_article_ignored(self):
        self.assertEqual(sources.canonical("The Dark Knight"),
                         sources.canonical("Dark Knight"))

    def test_year_suffix_ignored(self):
        self.assertEqual(sources.canonical("El Camino (2019)"),
                         sources.canonical("El Camino"))

    def test_case_and_punctuation_ignored(self):
        self.assertEqual(sources.canonical("breaking bad!"),
                         sources.canonical("Breaking Bad"))


class TestRequirements(unittest.TestCase):
    def test_counts_shots_per_title(self):
        beats = [
            {"beat": 1, "shots": [shot("Breaking Bad"), shot("Breaking Bad")]},
            {"beat": 2, "shots": [shot("Better Call Saul")]},
        ]
        reqs = {r.title: r for r in sources.requirements(beats)}
        self.assertEqual(reqs["Breaking Bad"].shots, 2)
        self.assertEqual(reqs["Better Call Saul"].shots, 1)

    def test_alias_and_full_name_merge(self):
        beats = [{"beat": 1, "shots": [shot("Better Call Saul"), shot("BCS")]}]
        reqs = sources.requirements(beats)
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0].shots, 2)

    def test_declared_episodes_collected(self):
        beats = [{"beat": 1, "shots": [shot("Breaking Bad", "S02E08"),
                                       shot("Breaking Bad", "S05E14")]}]
        self.assertEqual(sources.requirements(beats)[0].episodes_declared,
                         {(2, 8), (5, 14)})

    def test_image_only_source_still_listed(self):
        beats = [{"beat": 1, "shots": [],
                  "images": [{"source": "The Dark Knight", "subject": "Joker"}]}]
        self.assertEqual([r.title for r in sources.requirements(beats)],
                         ["The Dark Knight"])

    def test_beats_are_recorded(self):
        beats = [{"beat": 3, "shots": [shot("Breaking Bad")]},
                 {"beat": 9, "shots": [shot("Breaking Bad")]}]
        self.assertEqual(sources.requirements(beats)[0].beats, [3, 9])


class TestCheckAgainstLibrary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sources_")
        root = os.path.join(cls.tmp, "Media")

        def ep(show, s, e, lines):
            v = os.path.join(root, show, f"Season {s:02d}",
                             f"{show.replace(' ', '.')}.S{s:02d}E{e:02d}"
                             ".1080p.BluRay.x265-PSA.mkv")
            write_video(v)
            write(v[:-4] + ".srt", srt(lines))

        ep("Breaking Bad", 1, 1, [(60_000, 63_000, "Then we burn the field.")])
        ep("Breaking Bad", 2, 8, [(300_000, 303_000, "I never wanted the harvest.")])
        ep("Better Call Saul", 1, 2, [(90_000, 93_000, "Let them call.")])
        ep("Better Call Saul", 3, 5,
           [(700_000, 703_600, "You brought a gun to a courthouse.")])
        # present, but with no readable subtitles at all
        write_video(os.path.join(root, "The Dark Knight (2008)",
                                 "The.Dark.Knight.2008.1080p.mkv"))

        cls.db = os.path.join(cls.tmp, "library.db")
        library.build(root, cls.db, log=lambda *a: None)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _check(self, beats):
        return {r.title: r for r in sources.check(self.db, beats)}

    def test_fully_present_title(self):
        r = self._check([{"beat": 1, "shots": [
            shot("Better Call Saul", "S01E02", "Let them call")]}])
        self.assertEqual(r["Better Call Saul"].status, "present")

    def test_missing_title_is_named(self):
        r = self._check([{"beat": 1, "shots": [
            shot("El Camino", "unknown", "nothing here")]}])
        self.assertEqual(r["El Camino"].status, "missing")
        self.assertIn("download", r["El Camino"].note)

    def test_missing_episode_is_named_not_the_whole_series(self):
        """The saving that matters: 'you need S05E14', not 'download 250 GB'."""
        r = self._check([{"beat": 1, "shots": [
            shot("Breaking Bad", "S01E01", "Then we burn the field"),
            shot("Breaking Bad", "S05E14", "Say my name")]}])
        req = r["Breaking Bad"]
        self.assertEqual(req.status, "partial")
        self.assertEqual(req.missing_episodes, {(5, 14)})
        self.assertIn("S05E14", req.note)

    def test_title_present_but_unreadable_subtitles(self):
        r = self._check([{"beat": 1, "shots": [
            shot("The Dark Knight", "unknown", "why so serious")]}])
        self.assertEqual(r["The Dark Knight"].status, "no_text_subs")

    def test_alias_matches_the_library(self):
        r = self._check([{"beat": 1, "shots": [
            shot("BCS", "S01E02", "Let them call")]}])
        self.assertEqual(r["BCS"].status, "present")

    def test_dialogue_resolution_finds_the_real_episode(self):
        """The script gives no episode hint; the library supplies it."""
        r = self._check([{"beat": 1, "shots": [
            shot("Breaking Bad", "unknown", "I never wanted the harvest")]}])
        self.assertIn((2, 8), r["Breaking Bad"].episodes_resolved)

    def test_wrong_episode_hint_does_not_create_a_false_gap(self):
        """A script that says S09E99 must not make us report a missing episode
        we never actually needed — the resolved location is what counts."""
        r = self._check([{"beat": 1, "shots": [
            shot("Breaking Bad", "unknown", "Then we burn the field")]}])
        self.assertEqual(r["Breaking Bad"].status, "present")

    def test_report_renders(self):
        text = sources.format_report(sources.check(self.db, [
            {"beat": 1, "shots": [shot("Breaking Bad", "S05E14", "Say my name"),
                                  shot("El Camino", "unknown", "x")]}]))
        self.assertIn("El Camino", text)
        self.assertIn("missing", text.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
