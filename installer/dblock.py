"""An exclusive lock on the shared pr4m_test database.

Every api run re-seeds pr4m_test, so a seed or a run must never overlap another. The holder refreshes the
lock file every few seconds; a lock whose file stopped being refreshed, or whose holder PID is no longer alive, belongs to a
killed process and is taken over. Used by `p4m db prepare-test` and `p4m test api`.
"""

import csv
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

HEARTBEAT_SECONDS = 5
STALE_SECONDS = 60


class LockTimeout(Exception):
    """The lock was held by a live process for longer than the wait limit."""


def _age(path):
    try:
        return time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return None


def holder_pid(path):
    """The PID written in the lock file, or None if it is missing or unreadable."""
    try:
        return int(Path(path).read_text(errors="replace").strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid):
    """Whether a process with this PID exists. Our own PID counts as alive."""
    if pid is None:
        return False
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        cp = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                            capture_output=True, text=True, errors="replace")
        return any(len(row) > 1 and row[1] == str(pid) for row in csv.reader(cp.stdout.splitlines()))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def describe_holder(path):
    """`pid N (command line)` for the process holding the lock, so a user can kill it."""
    pid = holder_pid(path)
    if pid is None:
        return "unknown holder"
    cmdline = ""
    try:
        if sys.platform == "win32":
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


@contextmanager
def held(path, wait_seconds=3600, on_wait=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline, told = time.time() + wait_seconds, False
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            age = _age(path)
            if age is None:                      # released between our create and our stat
                continue
            pid = holder_pid(path)    # None while the holder is between create and write: unknown, not dead
            if age > STALE_SECONDS or (pid is not None and not pid_alive(pid)):   # killed, or an orphan's heartbeat is gone
                try:
                    os.replace(path, f"{path}.stale-{os.getpid()}")   # atomic: only one taker wins
                    os.unlink(f"{path}.stale-{os.getpid()}")
                except FileNotFoundError:
                    pass
                continue
            if time.time() > deadline:
                raise LockTimeout(f"{path} is held by a running process ({describe_holder(path)})")
            if on_wait and not told:
                on_wait(path)
                told = True
            time.sleep(1)
    stop = threading.Event()

    def beat():
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                os.utime(path)
            except FileNotFoundError:
                return
    t = threading.Thread(target=beat, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join(timeout=2)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
