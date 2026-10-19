"""Unit tests for the pr4m_test lock. Run: python -m unittest discover -s tests"""

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dblock  # noqa: E402


class LockTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "db.lock"

    def tearDown(self):
        self.dir.cleanup()

    def test_released_after_use(self):
        with dblock.held(self.path):
            self.assertTrue(self.path.exists())
        self.assertFalse(self.path.exists())

    def test_second_holder_waits_for_the_first(self):
        order = []

        def second():
            with dblock.held(self.path):
                order.append("second")
        with dblock.held(self.path):
            t = threading.Thread(target=second)
            t.start()
            time.sleep(1.5)
            order.append("first")
        t.join(5)
        self.assertEqual(order, ["first", "second"])

    def test_stale_lock_is_taken_over(self):
        self.path.write_text("12345")
        old = time.time() - dblock.STALE_SECONDS - 5
        os.utime(self.path, (old, old))
        with dblock.held(self.path, wait_seconds=5):
            self.assertEqual(self.path.read_text(), str(os.getpid()))

    def test_live_lock_times_out(self):
        self.path.write_text("12345")             # fresh mtime: a live holder
        with self.assertRaises(dblock.LockTimeout):
            with dblock.held(self.path, wait_seconds=1):
                pass


if __name__ == "__main__":
    unittest.main()
