"""Unit tests for the test slots `ai test` leases while it tests. Run: python -m unittest discover -s tests"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import testlease  # noqa: E402


class LeaseTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.folder = Path(self.dir.name)
        self.slept = []

    def tearDown(self):
        self.dir.cleanup()

    def lease(self, count=2, repo="prometheus"):
        return testlease.leased(self.folder, repo, count, sleep=self.slept.append)

    def test_leases_the_first_free_slot_and_frees_it_at_the_end(self):
        with self.lease() as first:
            self.assertEqual(first, 1)
            self.assertEqual(testlease.free_count(self.folder, "prometheus", 2), 1)
            with self.lease() as second:
                self.assertEqual(second, 2)
                self.assertEqual(testlease.free_count(self.folder, "prometheus", 2), 0)
        self.assertEqual(testlease.free_count(self.folder, "prometheus", 2), 2)

    def test_no_slot_when_every_slot_is_held_after_one_more_look(self):
        with self.lease() as a, self.lease() as b, self.lease() as c:
            self.assertEqual((a, b, c), (1, 2, None))
        self.assertEqual(self.slept, [testlease.RETRY_SECONDS])

    def test_repos_lease_from_their_own_slots(self):
        with self.lease(count=1), self.lease(count=1, repo="hermes") as other:
            self.assertEqual(other, 1)

    def test_slot_count_comes_from_the_environment(self):
        self.assertEqual(testlease.slots_from_env({testlease.ENV: "3"}), 3)
        self.assertEqual(testlease.slots_from_env({}), 0)
        self.assertEqual(testlease.slots_from_env({testlease.ENV: "x"}), 0)
        self.assertEqual(testlease.env_for(4), {testlease.ENV: "4"})
        self.assertEqual(testlease.env_for(0), {})


if __name__ == "__main__":
    unittest.main()
