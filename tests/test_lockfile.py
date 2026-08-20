"""One indexer per library, and a plain sentence when there are two.

Three programs were started on the same library at once — `overnight.bat`,
the Library page, and a build — and every one of them printed a hopeful
first line and then sat there. "Process aage hi nahi badh raha" is a fair
description of three programs waiting for each other.
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from media_index import lockfile                            # noqa: E402


class TestHoldingALibrary(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lock_")
        self.db = os.path.join(self.tmp, "library.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_free_library_is_free(self):
        self.assertEqual(lockfile.held_by(self.db), ())

    def test_the_second_one_is_refused_and_told_who_has_it(self):
        with lockfile.held(self.db, "pictures padhna"):
            with self.assertRaises(lockfile.Busy) as caught:
                with lockfile.held(self.db, "pictures padhna"):
                    self.fail("two indexers held the same library")
        self.assertIn("pictures padhna", str(caught.exception))

    def test_the_lock_is_released_even_when_the_work_crashes(self):
        """The one thing worse than a second indexer is a lock nobody can
        clear."""
        with self.assertRaises(ValueError):
            with lockfile.held(self.db):
                raise ValueError("ffmpeg fell over")
        self.assertEqual(lockfile.held_by(self.db), ())
        with lockfile.held(self.db):
            pass                            # and it can be taken again

    def test_a_lock_nobody_has_touched_is_abandoned(self):
        """Which is the right answer after a laptop lid closes."""
        with lockfile.held(self.db):
            old = time.time() - lockfile.STALE_S - 60
            os.utime(lockfile.path_for(self.db), (old, old))
            self.assertEqual(lockfile.held_by(self.db), ())
            with lockfile.held(self.db, "taking over"):
                self.assertTrue(lockfile.held_by(self.db)[0].startswith("taking over"))

    def test_touching_keeps_a_long_run_alive(self):
        with lockfile.held(self.db, "pictures padhna"):
            old = time.time() - lockfile.STALE_S - 60
            os.utime(lockfile.path_for(self.db), (old, old))
            lockfile.touch(self.db)
            self.assertTrue(lockfile.held_by(self.db))

    def test_a_lock_whose_process_is_gone_is_abandoned_at_once(self):
        """An interrupted run must not block for 30 minutes. A freshly
        written lock owned by a process that has since exited is cleared the
        instant someone else asks — this is the bug that made a one-second
        subtitle re-read look impossible."""
        import subprocess
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()                              # this pid is now dead
        with open(lockfile.path_for(self.db), "w", encoding="utf-8") as f:
            f.write(f"pictures padhna (pid {p.pid})")   # fresh mtime, dead pid
        self.assertEqual(lockfile.held_by(self.db), ())
        # and the ghost file is tidied away, so it cannot mislead later
        self.assertFalse(os.path.exists(lockfile.path_for(self.db)))

    def test_a_lock_whose_process_is_alive_still_holds(self):
        with open(lockfile.path_for(self.db), "w", encoding="utf-8") as f:
            f.write(f"pictures padhna (pid {os.getpid()})")
        self.assertTrue(lockfile.held_by(self.db))

    def test_two_different_libraries_do_not_block_each_other(self):
        other = os.path.join(self.tmp, "got.db")
        with lockfile.held(self.db), lockfile.held(other):
            self.assertTrue(lockfile.held_by(self.db))
            self.assertTrue(lockfile.held_by(other))

    def test_a_library_in_a_folder_that_is_gone_does_not_raise(self):
        gone = os.path.join(self.tmp, "nope", "library.db")
        with lockfile.held(gone):
            pass                            # no lock written, and no crash


if __name__ == "__main__":
    unittest.main()
