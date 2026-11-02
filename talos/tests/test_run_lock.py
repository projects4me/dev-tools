"""`talos run` is single-instance: a second one fails naming the holder while the first runs."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import talos  # noqa: E402
from lib.config import TalosError  # noqa: E402


class RunLockTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = mock.patch.object(talos, "STATE_DIR", Path(tmp.name))
        state.start()
        self.addCleanup(state.stop)
        self.args = SimpleNamespace(agent=None, dry_run=True)

    def test_second_run_is_refused_while_the_lock_is_held_and_allowed_after(self):
        entered = []

        def first_run(args):
            entered.append(True)
            with self.assertRaises(TalosError) as refused:
                talos.cmd_run(self.args)
            self.assertIn("already running", str(refused.exception))
            self.assertIn(f"pid {os.getpid()}", str(refused.exception))
            return 0

        with mock.patch.object(talos, "_run", first_run):
            self.assertEqual(talos.cmd_run(self.args), 0)
        self.assertEqual(entered, [True])
        with mock.patch.object(talos, "_run", lambda args: 7):
            self.assertEqual(talos.cmd_run(self.args), 7)

    def test_lock_is_released_when_the_run_raises(self):
        def crash(args):
            raise RuntimeError("boom")

        with mock.patch.object(talos, "_run", crash):
            with self.assertRaises(RuntimeError):
                talos.cmd_run(self.args)
        with mock.patch.object(talos, "_run", lambda args: 3):
            self.assertEqual(talos.cmd_run(self.args), 3)


if __name__ == "__main__":
    unittest.main()
