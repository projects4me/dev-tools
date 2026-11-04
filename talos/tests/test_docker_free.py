"""docker_free: which test containers may be stopped when Docker is short of memory."""

import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import docker_free  # noqa: E402

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


def ago(hours):
    return (NOW - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def pick(issues, running=(), boxes=None):
    boxes = boxes or {n: [f"c{n}"] for n in issues}
    return [i for i, _, _ in docker_free.stoppable(boxes, issues, set(running), NOW)]


class StoppableTest(unittest.TestCase):
    def test_in_flight_issue_is_never_chosen(self):
        self.assertEqual(pick({1: {"status": "Deferred", "modified": ago(500)}}, running={1}), [])

    def test_deferred_and_wont_fix_are_chosen(self):
        issues = {1: {"status": "Deferred", "modified": ago(1)}, 2: {"status": "Won't Fix", "modified": ago(1)},
                  3: {"status": "in_progress", "modified": ago(1)}}
        self.assertEqual(pick(issues), [1, 2])

    def test_stale_over_72_hours_chosen_71_not(self):
        issues = {1: {"status": "in_review", "modified": ago(73)}, 2: {"status": "in_review", "modified": ago(71)}}
        self.assertEqual(pick(issues), [1])

    def test_unreadable_issue_is_not_chosen(self):
        self.assertEqual(pick({}, boxes={1: ["c1"]}), [])
        self.assertEqual(pick({1: {"status": "in_review", "modified": None}}), [])
        self.assertEqual(pick({1: {"status": "in_review", "modified": "garbage"}}), [])


class StopTest(unittest.TestCase):
    def test_stop_runs_docker_stop(self):
        calls = []
        docker_free.stop(["a", "b"], run=lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0))
        self.assertEqual(calls, [["docker", "stop", "a", "b"]])

    def test_failure_and_missing_docker_raise_oserror(self):
        with self.assertRaises(OSError):
            docker_free.stop(["a"], run=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "boom"))

        def missing(argv, **kw):
            raise FileNotFoundError("docker")
        with self.assertRaises(OSError):
            docker_free.stop(["a"], run=missing)


if __name__ == "__main__":
    unittest.main()
