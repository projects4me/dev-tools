"""SlotWaits: runs waiting for a test slot, resumed oldest first as slots free (adt-4372)."""

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.slot_waits import SlotWaits  # noqa: E402

T0 = datetime(2026, 11, 8, 12, 0)


class SlotWaitsTest(unittest.TestCase):
    def park(self, waits, number, minutes):
        waits.park(number, "prometheus", "sonnet", f"s{number}", False, T0 + timedelta(minutes=minutes))

    def test_oldest_waits_are_resumed_first_up_to_the_free_slots(self):
        waits = SlotWaits()
        self.park(waits, 7, 5)
        self.park(waits, 3, 1)
        self.park(waits, 9, 9)
        self.assertEqual(waits.due(2), [3, 7])
        self.assertEqual(waits.due(2), [])          # the two resumed runs may take those slots
        self.assertEqual(waits.due(3), [9])

    def test_a_run_that_waits_again_keeps_its_place(self):
        waits = SlotWaits()
        self.park(waits, 1, 0)
        self.park(waits, 2, 5)
        self.assertEqual(waits.due(1), [1])
        self.park(waits, 1, 30)                    # resumed, no slot again
        self.assertIsNone(waits.resumed(1))
        self.assertEqual(waits.due(1), [1])
        self.assertEqual(waits.resumed(1).since, T0)

    def test_dropped_waits_free_their_place(self):
        waits = SlotWaits()
        self.park(waits, 1, 0)
        self.assertEqual(waits.due(1), [1])
        waits.drop(1)
        self.assertIsNone(waits.resumed(1))
        self.assertEqual(waits.due(1), [])


if __name__ == "__main__":
    unittest.main()
