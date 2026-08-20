"""Tests for what the tool owns, grouped as a person sees it.

The Library screen is the first thing anyone looks at and the last thing
they will double-check, so what it claims has to be true even when the disk
disagrees with the database: a title whose footage was deleted must not sit
there saying `ready`, and a title with no subtitles must not be reported as
merely un-indexed, because that sends someone off to run the four-hour step
that cannot possibly fix it.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from media_index import libraries, library, visual        # noqa: E402


class _Shelf(unittest.TestCase):
    """A database holding real rows, with frames beside it on disk."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="libs_")
        self.db = os.path.join(self.tmp, "library.db")
        self.store = visual.store_dir(self.db)
        os.makedirs(self.store, exist_ok=True)
        self.con = library.connect(self.db)

    def tearDown(self):
        try:
            self.con.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, show, n=1, kind="episode", subtitled=True, indexed=0,
            on_disk=True, frame_bytes=2048, folder=None):
        root = os.path.join(self.tmp, "Media", folder or show)
        os.makedirs(root, exist_ok=True)
        for i in range(1, n + 1):
            path = os.path.join(root, f"{show} S01E{i:02d}.mkv")
            if on_disk:
                open(path, "wb").close()
            self.con.execute(
                "INSERT INTO media(path,kind,show,show_norm,season,episode,"
                "cue_count) VALUES(?,?,?,?,?,?,?)",
                (path, kind, show, show.lower(),
                 None if kind == "movie" else 1,
                 None if kind == "movie" else i, 800 if subtitled else 0))
            if i <= indexed:
                npz = os.path.join(self.store, f"{show[:2]}{i:04d}.npz")
                with open(npz, "wb") as f:
                    f.write(b"x" * frame_bytes)
                self.con.execute(
                    "INSERT INTO visual(path,file_size,file_mtime,model,fps,"
                    "frames,dim,vectors,built_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (path, 0, 0, "m", 0.5, 100, 768, npz, 0))
        self.con.commit()

    def one(self, name=None):
        rows = libraries.titles(self.db)
        if name:
            rows = [r for r in rows if r.name == name]
        return rows[0]


class TestWhatATitleKnows(_Shelf):
    def test_a_show_is_one_row_however_many_episodes_it_has(self):
        self.add("Breaking Bad", n=12, indexed=12)
        rows = libraries.titles(self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].files, 12)
        self.assertEqual(rows[0].status, libraries.READY)

    def test_frames_are_counted_from_the_files_beside_the_database(self):
        self.add("Breaking Bad", n=3, indexed=3, frame_bytes=1024 * 1024)
        self.assertEqual(self.one().frame_bytes, 3 * 1024 * 1024)
        self.assertEqual(libraries.human_size(self.one().frame_bytes), "3 MB")

    def test_a_half_indexed_show_says_so_rather_than_ready(self):
        """Nine of sixty-two indexed was the real state of the library for
        six builds, and nothing on screen ever said it."""
        self.add("Breaking Bad", n=10, indexed=3)
        t = self.one()
        self.assertEqual(t.status, libraries.PARTIAL)
        self.assertEqual(t.detail, "3 of 10 indexed")

    def test_missing_subtitles_beat_missing_frames_in_what_is_reported(self):
        # Telling someone to run the four-hour picture step, when what is
        # actually wrong is a missing .srt, wastes the four hours.
        self.add("Shawshank", n=1, kind="movie", subtitled=False)
        t = self.one()
        self.assertEqual(t.status, libraries.ATTENTION)
        self.assertIn("without subtitle", t.detail)

    def test_footage_that_is_gone_is_not_reported_as_ready(self):
        self.add("Titanic", n=1, kind="movie", indexed=1, on_disk=False)
        t = self.one()
        self.assertEqual(t.missing, 1)
        self.assertEqual(t.status, libraries.ATTENTION)
        self.assertIn("missing", t.detail)

    def test_a_movie_is_told_apart_from_a_series(self):
        self.add("Titanic", n=1, kind="movie", indexed=1)
        self.add("Breaking Bad", n=2, indexed=2)
        kinds = {t.name: t.kind for t in libraries.titles(self.db)}
        self.assertEqual(kinds["Titanic"], "movie")
        self.assertEqual(kinds["Breaking Bad"], "series")

    def test_the_folder_a_title_lives_in_is_worked_out_from_its_files(self):
        self.add("Breaking Bad", n=3, indexed=1)
        self.assertTrue(self.one().media_root.endswith("Breaking Bad"))

    def test_a_database_that_is_not_there_is_an_empty_shelf_not_a_crash(self):
        self.assertEqual(libraries.titles(os.path.join(self.tmp, "no.db")), [])


class TestFindingLibraries(_Shelf):
    def test_a_database_beside_the_tool_is_still_found(self):
        """The tool put its first library next to start.bat. Telling someone
        their existing work is in the wrong place is not an answer."""
        self.add("Breaking Bad", n=2, indexed=2)
        cat = libraries.catalogue("", self.db)
        self.assertEqual(cat["counts"]["all"], 1)
        self.assertIn(os.path.abspath(self.db),
                      [os.path.abspath(d) for d in cat["databases"]])

    def test_one_library_per_title_under_a_libraries_folder(self):
        root = os.path.join(self.tmp, "Libraries")
        for show in ("Breaking Bad", "Titanic"):
            here = os.path.join(root, show)
            os.makedirs(here)
            db = os.path.join(here, "library.db")
            con = library.connect(db)
            con.execute(
                "INSERT INTO media(path,kind,show,show_norm,season,episode,"
                "cue_count) VALUES(?,?,?,?,?,?,?)",
                (os.path.join(here, "a.mkv"), "movie", show, show.lower(),
                 None, None, 500))
            con.commit()
            con.close()
        cat = libraries.catalogue(root)
        self.assertEqual(cat["counts"]["all"], 2)
        self.assertEqual(len(cat["databases"]), 2)

    def test_the_same_show_in_two_places_is_still_one_row(self):
        self.add("Breaking Bad", n=2, indexed=2)
        cat = libraries.catalogue("", self.db)
        # The fallback database is also reachable as itself; counting it
        # twice would show the same show twice on screen.
        cat2 = libraries.catalogue(os.path.dirname(self.db), self.db)
        self.assertEqual(cat["counts"]["all"], 1)
        self.assertEqual(cat2["counts"]["all"], 1)

    def test_the_counts_add_up_to_what_the_chips_show(self):
        self.add("Breaking Bad", n=2, indexed=2)
        self.add("Titanic", n=1, kind="movie", indexed=1)
        self.add("Shawshank", n=1, kind="movie", subtitled=False)
        c = libraries.catalogue("", self.db)["counts"]
        self.assertEqual(c["all"], 3)
        self.assertEqual(c["series"], 1)
        self.assertEqual(c["movies"], 2)
        self.assertEqual(c["attention"], 1)


class TestSizes(unittest.TestCase):
    def test_nothing_is_a_dash_rather_than_zero_bytes(self):
        self.assertEqual(libraries.human_size(0), "—")

    def test_a_series_reads_in_megabytes_not_bytes(self):
        self.assertEqual(libraries.human_size(271 * 1024 * 1024), "271 MB")
        self.assertEqual(libraries.human_size(3 * 1024 ** 3), "3.0 GB")


if __name__ == "__main__":
    unittest.main(verbosity=2)
