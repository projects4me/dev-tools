"""An exclusive lock on the shared pr4m_test database.

Every api run re-seeds pr4m_test, so a seed or a run must never overlap another. The holder refreshes the
lock file every few seconds; a lock whose file stopped being refreshed belongs to a killed process and is
taken over. Used by `p4m db prepare-test` and `p4m test api`.
"""

import os
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
            if age > STALE_SECONDS:              # holder stopped refreshing: killed
                try:
                    os.replace(path, f"{path}.stale-{os.getpid()}")   # atomic: only one taker wins
                    os.unlink(f"{path}.stale-{os.getpid()}")
                except FileNotFoundError:
                    pass
                continue
            if time.time() > deadline:
                raise LockTimeout(f"{path} is held by a running process (pid {path.read_text(errors='replace')})")
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
