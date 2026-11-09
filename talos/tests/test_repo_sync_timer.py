"""Drift tracking for the repo sync timer. Run: python -m unittest discover -s tests"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import repo_sync_timer as timer  # noqa: E402


class DriftTest(unittest.TestCase):
    def test_first_seen_is_kept_and_caught_up_repos_are_forgotten(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(timer, "DRIFT_FILE", Path(tmp) / "drift.json"):
                t0 = datetime(2026, 11, 9, 10, 0)
                timer.record_drift("gaia: behind origin/dev: 2\nhermes: behind origin/dev: 0", t0)
                timer.record_drift("gaia: behind origin/dev: 3\nares: behind origin/dev: 1", t0 + timedelta(minutes=70))
                drift = timer.drift()
                self.assertEqual(sorted(drift), ["ares", "gaia"])
                self.assertEqual(drift["gaia"], t0.isoformat())
                self.assertEqual(timer.stale(drift, t0 + timedelta(minutes=70)), ["gaia"])


if __name__ == "__main__":
    unittest.main()
