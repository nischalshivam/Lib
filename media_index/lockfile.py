"""One indexer per library, and a plain sentence when there are two.

Reading the pictures of a library saturates the machine: it is the picture
model on the CPU, ffmpeg decoding, and a stream of writes into one SQLite
file. Two of those at once do not go twice as fast. They go slower than one,
they fight over the same database, and — this is the part that matters —
**neither of them looks broken while it happens.**

That is exactly what happened. A run was started from `overnight.bat`, the
Library page in the browser was used to start a second one on the same
library, and a build was left running in a third. Everything printed a
hopeful first line and then sat there. The report was "process aage hi nahi
badh raha", which is a fair description of three programs waiting for each
other.

So: a lock beside the database. Not to be clever about concurrency — to say
the sentence out loud.

## Why a heartbeat and not a process id

Checking whether the holder is still alive is the obvious design and it is
not portable. `os.kill(pid, 0)` is the POSIX way to ask; on Windows Python's
`os.kill` calls `TerminateProcess`, so the "are you alive" check would kill
the very process it was asking about. This tool runs on Windows.

So the holder touches the file as it works, and a lock nobody has touched
for a while is treated as abandoned — which is also the right answer after
the laptop the first run was on went to sleep.
"""
from __future__ import annotations

import contextlib
import os
import re
import time

# A lock nobody has touched for this long belonged to something that is no
# longer running: a closed window, a crash, a laptop lid. Comfortably longer
# than one episode takes to index (a 47-minute episode is about 8 minutes on
# a CPU), so a working run can never be mistaken for an abandoned one.
STALE_S = 1800.0
SUFFIX = ".lock"
_PID = re.compile(r"pid\s+(\d+)")


class Busy(Exception):
    """Something else is already working on this library."""


def path_for(db_path: str) -> str:
    return os.path.abspath(db_path) + SUFFIX


def _alive(pid: int) -> bool:
    """Is a process with this id running right now? Never disturbs it.

    The reason this module leaned on a heartbeat alone was that the obvious
    liveness check, `os.kill(pid, 0)`, is not portable: on Windows Python's
    `os.kill` calls `TerminateProcess`, so asking "are you alive" would kill
    the very process being asked about. So each OS is asked the read-only
    way. This turns an interrupted run — a closed window, Ctrl-C, a crash,
    the tool restarted — from a 30-minute wait into an immediate all-clear:
    a lock whose owner no longer exists is not holding anything.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        QUERY = 0x1000                        # PROCESS_QUERY_LIMITED_INFORMATION
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(QUERY, False, pid)
        if not handle:
            return False                      # no such process
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True                   # exists; couldn't read: assume alive
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)                       # signal 0 only checks, never sends
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True                           # exists, just not ours to signal
    return True


def held_by(db_path: str) -> tuple:
    """(what it is doing, seconds since it last said anything) or ().

    A lock file that cannot be read is treated as absent. It is a hint, not
    a permission system, and refusing to work because of an unreadable hint
    would be the worse failure.
    """
    lock = path_for(db_path)
    try:
        with open(lock, "r", encoding="utf-8") as f:
            what = f.read(200).strip() or "kuch"
        idle = time.time() - os.path.getmtime(lock)
    except OSError:
        return ()
    # A lock whose owning process is gone is abandoned no matter how recently
    # it was written. An interrupted indexer leaves its file behind, and
    # making a one-second subtitle re-read wait 30 minutes for that ghost to
    # "expire" is exactly what made updating a library feel impossible.
    m = _PID.search(what)
    if m and not _alive(int(m.group(1))):
        with contextlib.suppress(OSError):
            os.remove(lock)                   # tidy the ghost away
        return ()
    if idle > STALE_S:
        return ()
    return (what, idle)


def touch(db_path: str) -> None:
    """Say we are still here. Called as each episode finishes."""
    with contextlib.suppress(OSError):
        os.utime(path_for(db_path), None)


@contextlib.contextmanager
def held(db_path: str, what: str = "indexing", log=lambda *a: None):
    """Hold the library for the duration, or raise `Busy` saying who has it.

    Released on the way out however the block ends, including a crash — the
    one thing worse than a second indexer is a lock nobody can clear.
    """
    owner = held_by(db_path)
    if owner:
        raise Busy(
            f"is library par pehle se '{owner[0]}' chal raha hai "
            f"({owner[1] / 60:.0f} min pehle tak zinda tha). "
            "Do kaam ek saath chalane se dono ruk jaate hain — pehle wale ko "
            "khatam hone do, ya us window ko band kar do aur "
            f"{int(STALE_S / 60)} minute baad dobara koshish karo.")
    lock = path_for(db_path)
    try:
        with open(lock, "w", encoding="utf-8") as f:
            f.write(f"{what} (pid {os.getpid()})")
    except OSError:
        log("      (lock nahi bana — do kaam ek saath chalane se bachna)")
        yield
        return
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            os.remove(lock)
