"""Unit tests for the p4m test in-use marks. Run: python -m unittest discover -s tests"""

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import inuse  # noqa: E402


class UsingTest(unittest.TestCase):
    """using / usage: the in-use mark a p4m test run leaves for Talos' idle sweep."""

    def test_in_use_inside_the_block_and_last_use_after_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(inuse.usage(tmp, "box"), (False, None))
            with inuse.using(tmp, "box"):
                busy, _ = inuse.usage(tmp, "box")
                self.assertTrue(busy)
                self.assertFalse(inuse.usage(tmp, "other")[0])
            busy, last = inuse.usage(tmp, "box")
            self.assertFalse(busy)
            self.assertAlmostEqual(last, time.time(), delta=60)
            self.assertEqual(list((Path(tmp) / inuse.USE_DIR).glob("*.lock")), [])

    def test_the_mark_is_released_when_the_run_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError), inuse.using(tmp, "box"):
                raise RuntimeError("boom")
            self.assertFalse(inuse.usage(tmp, "box")[0])
            self.assertIsNotNone(inuse.usage(tmp, "box")[1])

    def test_a_mark_no_process_holds_is_not_in_use_and_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / inuse.USE_DIR
            folder.mkdir()
            stale = folder / "box.999999.lock"
            stale.write_text("999999")     # left by a killed run
            self.assertFalse(inuse.usage(tmp, "box")[0])
            self.assertFalse(stale.exists())

    def test_a_failing_cleanup_keeps_the_runs_own_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = Path.unlink

            def refuse(self, missing_ok=False):
                raise PermissionError("in use")
            Path.unlink = refuse
            try:
                with self.assertRaises(RuntimeError), inuse.using(tmp, "box"):
                    raise RuntimeError("boom")
            finally:
                Path.unlink = original

    def test_state_dir_is_p4ms(self):
        self.assertEqual(inuse.state_dir(Path("ws")), Path("ws") / "dev-tools" / ".p4m" / "test")


if __name__ == "__main__":
    unittest.main()
