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


def minutes_ago(minutes):
    return (NOW - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def pick_idle(entries, boxes=None, minutes=15):
    boxes = boxes or {issue: [f"c{issue}"] for _, issue in entries}
    return [i for i, _, _ in docker_free.idle(boxes, entries, NOW, minutes)]


class IdleTest(unittest.TestCase):
    def test_last_run_ended_past_the_window_is_chosen_inside_it_not(self):
        entries = {("odysseus", 1): {"last_ended_at": minutes_ago(16)},
                   ("odysseus", 2): {"last_ended_at": minutes_ago(14)}}
        self.assertEqual(pick_idle(entries), [1])

    def test_reason_names_the_idle_minutes(self):
        chosen = docker_free.idle({1: ["c1"]}, {("odysseus", 1): {"last_ended_at": minutes_ago(40)}}, NOW, 15)
        self.assertEqual(chosen, [(1, ["c1"], "idle for 40m")])

    def test_a_run_in_flight_on_any_agent_keeps_it(self):
        entries = {("odysseus", 1): {"last_ended_at": minutes_ago(60)},
                   ("nestor", 1): {"running": 1, "last_ended_at": minutes_ago(60)}}
        self.assertEqual(pick_idle(entries), [])

    def test_the_latest_end_of_any_agent_counts(self):
        entries = {("odysseus", 1): {"last_ended_at": minutes_ago(60)},
                   ("nestor", 1): {"last_ended_at": minutes_ago(5)}}
        self.assertEqual(pick_idle(entries), [])

    def test_a_conflict_test_in_use_keeps_it_and_its_end_restarts_the_window(self):
        testing = {("odysseus", 1): {"last_ended_at": minutes_ago(60), "conflict_test_running": 1,
                                     "conflict_test_at": minutes_ago(20)}}
        self.assertEqual(pick_idle(testing), [])
        ended = {("odysseus", 1): {"last_ended_at": minutes_ago(60), "conflict_test_at": minutes_ago(5)}}
        self.assertEqual(pick_idle(ended), [])

    def test_a_conflict_test_mark_left_behind_is_ignored(self):
        entries = {("odysseus", 1): {"last_ended_at": minutes_ago(300), "conflict_test_running": 1,
                                     "conflict_test_at": minutes_ago(180)}}
        self.assertEqual(pick_idle(entries), [1])
        untimed = {("odysseus", 1): {"last_ended_at": minutes_ago(300), "conflict_test_running": 1,
                                     "conflict_test_at": None}}
        self.assertEqual(pick_idle(untimed), [1])

    def test_no_recorded_end_or_no_talos_row_is_left_alone(self):
        self.assertEqual(pick_idle({("odysseus", 1): {"last_ended_at": None}}), [])
        self.assertEqual(pick_idle({}, boxes={1: ["c1"]}), [])

    def test_zero_minutes_turns_it_off(self):
        self.assertEqual(pick_idle({("odysseus", 1): {"last_ended_at": minutes_ago(600)}}, minutes=0), [])


class PlanIdleTest(unittest.TestCase):
    def test_only_running_containers_in_run_workspaces_are_chosen(self):
        """testenv.containers lists only checkouts under <root>/.runs (an interactive checkout or a gaia slot
        container never matches); a stopped one is not stopped again."""
        root = Path("/ws")
        asked = []

        class FakeTestEnv:
            TestEnvError = RuntimeError

            @staticmethod
            def containers(under):
                asked.append(Path(under))
                return [("p4m-test-a", "issue-1/prometheus"), ("p4m-test-b", "issue-2/prometheus")]

        def ps(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, "p4m-test-a\ngaia-test-slot-1\n", "")
        entries = {("odysseus", 1): {"last_ended_at": minutes_ago(30)},
                   ("odysseus", 2): {"last_ended_at": minutes_ago(30)}}
        chosen = docker_free.plan_idle(root, entries, FakeTestEnv, 15, now=NOW, run=ps)
        self.assertEqual(chosen, [(1, ["p4m-test-a"], "idle for 30m")])
        self.assertEqual(asked, [root / ".runs"])

    def test_docker_ps_failing_raises_oserror(self):
        with self.assertRaises(OSError):
            docker_free.running(run=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "down"))


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
