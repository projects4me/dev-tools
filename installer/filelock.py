"""The one exclusive file lock every dev tool uses (timers, dev stack, pr4m_test, test slots, the template).

The lock is an OS lock on a sidecar file (msvcrt on Windows, fcntl elsewhere). The OS drops it when the holder dies,
so an orphan never blocks anyone and there is no stale-file takeover to race on. The file is never deleted; it holds
the holder's PID as text, so a waiter can say who to kill. Stdlib only: ai, p4m and talos all import it from here.
"""

import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

POLL_SECONDS = 0.05
LOCK_OFFSET = 1 << 30      # the locked byte lies far past the PID text: Windows refuses reads of a locked region

if os.name == "nt":
    import msvcrt

    def _try_lock(fd):
        try:
            os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd):
        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
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


class LockTimeout(Exception):
    """The lock stayed held by a live process for the whole wait."""


def holder_pid(path):
    """The PID the last holder wrote, or None if there is none. Only meaningful while the lock is held."""
    try:
        return int(Path(path).read_text(errors="replace").strip())
    except (OSError, ValueError):
        return None


def describe_holder(path):
    """`pid N (command line)` for the process holding the lock, so a user can kill it."""
    pid = holder_pid(path)
    if pid is None:
        return "unknown holder"
    cmdline = ""
    try:
        if os.name == "nt":
            cp = subprocess.run(["powershell", "-NoProfile", "-Command",
                                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                                capture_output=True, text=True, errors="replace", timeout=10)
        else:
            cp = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True,
                                errors="replace", timeout=10)
        cmdline = cp.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return f"pid {pid}" + (f" ({cmdline})" if cmdline else "")


def is_held(path):
    """Whether a live process holds the lock right now (a probe: it takes and drops the lock if it is free)."""
    path = Path(path)
    if not path.exists():
        return False
    fd = os.open(path, os.O_RDWR)
    try:
        if _try_lock(fd):
            _unlock(fd)
            return False
        return True
    finally:
        os.close(fd)


@contextmanager
def held(path, wait_seconds=30, on_wait=None):
    """Hold the lock for the block; wait up to `wait_seconds`, calling `on_wait(path)` once if it is taken."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    try:
        deadline, told = time.time() + wait_seconds, False
        while not _try_lock(fd):
            if time.time() > deadline:
                raise LockTimeout(f"{path} is held by a running process ({describe_holder(path)})")
            if on_wait and not told:
                on_wait(path)
                told = True
            time.sleep(POLL_SECONDS)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)
