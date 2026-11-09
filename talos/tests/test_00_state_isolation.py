"""Keeps the whole test run off the live Talos state (talos.log, talos.db, ...).

Discovery imports test modules alphabetically, so this one runs before any other imports lib.config: it points
TALOS_STATE_DIR at a temporary directory for the run. Run: python -m unittest discover -s tests"""

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if "TALOS_STATE_DIR" not in os.environ:
    _tmp = tempfile.mkdtemp(prefix="talos-test-state-")
    os.environ["TALOS_STATE_DIR"] = _tmp
    atexit.register(shutil.rmtree, _tmp, True)

from lib import config  # noqa: E402


class StateIsolationTest(unittest.TestCase):
    def test_state_dir_is_not_the_live_one(self):
        self.assertNotEqual(config.STATE_DIR, config.LOCAL_DIR / "talos" / "state")

    def test_log_writes_to_the_temporary_state(self):
        config.log("isolation probe")
        self.assertIn("isolation probe", (config.STATE_DIR / "talos.log").read_text(encoding="utf-8"))
