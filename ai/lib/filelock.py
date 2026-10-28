"""A short-lived exclusive lock, for read-modify-write of small shared state files (timers.json).

The lock is an OS lock on a sidecar file (msvcrt on Windows, fcntl elsewhere). The OS drops it when the process dies,
so there is no stale-lock takeover to race on, and the file is never deleted. For long holds (a browser check) use
installer/dblock.py instead.
"""

import os
import time
from contextlib import contextmanager
from pathlib import Path

if os.name == "nt":
    import msvcrt

    def _try_lock(fd):
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd):
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fd):
        fcntl.flock(fd, fcntl.LOCK_UN)

POLL_SECONDS = 0.02


class LockTimeout(Exception):
    """The lock stayed taken for the whole wait."""


@contextmanager
def held(path, wait_seconds=30):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    try:
        deadline = time.time() + wait_seconds
        while not _try_lock(fd):
            if time.time() > deadline:
                raise LockTimeout(f"{path} stayed locked for {wait_seconds}s")
            time.sleep(POLL_SECONDS)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)
