"""Queue order, concurrency caps and slot leases."""

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import pool  # noqa: E402


class OrderTest(unittest.TestCase):
    def test_resumptions_first_then_priority_then_fifo(self):
        q = pool.RunQueue()
        q.put(pool.order_key(False, "high"), "new-high")
        q.put(pool.order_key(False, "low"), "new-low")
        q.put(pool.order_key(True, "low"), "resume-low")
        q.put(pool.order_key(False, "high"), "new-high-later")
        q.put(pool.order_key(True, "blocker"), "resume-blocker")
        q.put(pool.order_key(False, None), "new-unknown")
        self.assertEqual([q.get() for _ in range(6)],
                         ["resume-blocker", "resume-low", "new-high", "new-high-later", "new-unknown", "new-low"])

    def test_front_items_come_first_and_none_keys_are_ranked_at_get(self):
        q = pool.RunQueue()
        q.put(None, "a")
        q.put(None, "b")
        q.put(None, "limited", front=True)
        ranks = {"a": (1, 4), "b": (1, 0)}
        self.assertEqual([q.get(ranks.get) for _ in range(3)], ["limited", "b", "a"])

    def test_get_waits_for_a_put(self):
        q, got = pool.RunQueue(), []
        t = threading.Thread(target=lambda: got.append(q.get()))
        t.start()
        q.put((1, 1), "x")
        t.join(2)
        self.assertEqual(got, ["x"])


class GateTest(unittest.TestCase):
    def test_leases_distinct_slots_and_reuses_a_released_one(self):
        gate = pool.Gate(slots=2)
        a, b = gate.acquire(1, "gaia", "sonnet"), gate.acquire(2, "gaia", "sonnet")
        self.assertEqual({a.slot, b.slot}, {1, 2})
        gate.release(a)
        self.assertEqual(gate.acquire(3, "gaia", "sonnet").slot, a.slot)

    def test_slots_only_for_the_slot_repos(self):
        gate = pool.Gate(slots=1, slot_repos={"gaia"})
        self.assertEqual(gate.acquire(1, "prometheus", "sonnet").slot, 0)
        self.assertEqual(gate.acquire(2, "gaia", "sonnet").slot, 1)

    def test_no_slots_means_slot_zero(self):
        self.assertEqual(pool.Gate().acquire(1, "gaia", "sonnet").slot, 0)

    def blocked(self, gate, issue, repo, model):
        """Whether acquire waits: try it on a thread, free nothing, and see if it returned."""
        got = []
        t = threading.Thread(target=lambda: got.append(gate.acquire(issue, repo, model)), daemon=True)
        t.start()
        t.join(0.3)
        return not got, t, got

    def test_waits_for_a_free_slot(self):
        gate = pool.Gate(slots=1)
        first = gate.acquire(1, "gaia", "sonnet")
        waiting, t, got = self.blocked(gate, 2, "gaia", "sonnet")
        self.assertTrue(waiting)
        gate.release(first)
        t.join(2)
        self.assertEqual(got[0].slot, 1)

    def test_repo_cap(self):
        gate = pool.Gate(slots=3, by_repo={"gaia": 1})
        gate.acquire(1, "gaia", "sonnet")
        self.assertTrue(self.blocked(gate, 2, "gaia", "sonnet")[0])
        self.assertFalse(self.blocked(gate, 3, "prometheus", "sonnet")[0])

    def test_model_cap(self):
        gate = pool.Gate(slots=3, by_model={"opus": 1})
        first = gate.acquire(1, "gaia", "opus")
        self.assertTrue(self.blocked(gate, 2, "hermes", "opus")[0])
        self.assertFalse(self.blocked(gate, 3, "hermes", "sonnet")[0])
        gate.release(first)

    def test_three_at_once_each_in_its_own_slot(self):
        gate = pool.Gate(slots=3, by_model={"opus": 1})
        leases = [gate.acquire(n, "gaia", "sonnet") for n in (1, 2, 3)]
        self.assertEqual(sorted(l.slot for l in leases), [1, 2, 3])
        self.assertEqual(len(gate.in_flight()), 3)

    def test_changes_are_reported(self):
        seen = []
        gate = pool.Gate(slots=1, on_change=seen.append)
        lease = gate.acquire(1, "gaia", "sonnet")
        gate.release(lease)
        self.assertEqual([len(s) for s in seen], [1, 0])


class LeaseFileTest(unittest.TestCase):
    def test_round_trip_and_missing_file(self):
        f = pool.LeaseFile(Path(tempfile.mkdtemp()) / "a" / "leases.json")
        self.assertEqual(f.read(), {})
        f.write([pool.Lease(5, "gaia", "opus", 2)])
        self.assertEqual(f.read(), {5: {"issue": 5, "repo": "gaia", "model": "opus", "slot": 2}})


if __name__ == "__main__":
    unittest.main()
