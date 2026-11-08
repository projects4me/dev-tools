"""TestSlots: which runs lease a Prometheus test slot, and how many are free (adt-4372)."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import test_slots  # noqa: E402
from lib.test_slots import TestSlots  # noqa: E402


class TestSlotsTest(unittest.TestCase):
    def test_only_prometheus_runs_lease_and_only_with_a_cap(self):
        self.assertTrue(TestSlots(3).leases("prometheus"))
        self.assertFalse(TestSlots(3).leases("gaia"))
        self.assertFalse(TestSlots(0).leases("prometheus"))
        self.assertEqual(TestSlots(3).env("prometheus"), {"P4M_TEST_LEASE_SLOTS": "3"})
        self.assertEqual(TestSlots(3).env("gaia"), {})
        self.assertEqual(TestSlots(0).env("prometheus"), {})

    def test_free_counts_the_slots_no_one_holds(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(test_slots, "LOCAL_DIR", Path(tmp)):
            self.assertEqual(TestSlots(2).free(), 2)


if __name__ == "__main__":
    unittest.main()
