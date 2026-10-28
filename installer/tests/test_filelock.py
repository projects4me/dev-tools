"""Unit tests for the shared file lock. Run: python -m unittest discover -s tests"""

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import filelock  # noqa: E402

HOLDER = """
import sys, time
sys.path.insert(0, sys.argv[1])
import filelock
with filelock.held(sys.argv[2]):
    print("locked", flush=True)
    time.sleep(60)
"""


class LockTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "db.lock"

        self.addCleanup(self.dir.cleanup)       # runs after the holder's kill, which is registered later

    def test_file_is_kept_and_free_after_use(self):
        with filelock.held(self.path):
            self.assertTrue(filelock.is_held(self.path))
        self.assertTrue(self.path.exists())
        self.assertFalse(filelock.is_held(self.path))

    def test_holder_pid_is_readable_while_held(self):
        with filelock.held(self.path):
            self.assertEqual(filelock.holder_pid(self.path), os.getpid())

    def test_second_holder_waits_for_the_first(self):
        order = []

        def second():
            with filelock.held(self.path):
                order.append("second")
        with filelock.held(self.path):
            t = threading.Thread(target=second)
            t.start()
            time.sleep(0.5)
            order.append("first")
        t.join(5)
        self.assertEqual(order, ["first", "second"])

    def _start_holder(self):
        p = subprocess.Popen([sys.executable, "-c", HOLDER, str(Path(filelock.__file__).parent), str(self.path)],
                             stdout=subprocess.PIPE, text=True)
        self.addCleanup(p.stdout.close)
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        self.assertEqual(p.stdout.readline().strip(), "locked")
        return p

    def test_other_process_holds_the_lock(self):
        self._start_holder()
        self.assertTrue(filelock.is_held(self.path))
        told = []
        with self.assertRaises(filelock.LockTimeout):
            with filelock.held(self.path, wait_seconds=0.5, on_wait=told.append):
                pass
        self.assertEqual(told, [self.path])

    def test_killed_holder_releases_the_lock(self):
        p = self._start_holder()
        p.kill()
        p.wait(10)
        with filelock.held(self.path, wait_seconds=5):
            pass


if __name__ == "__main__":
    unittest.main()
