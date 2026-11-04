"""Unit tests for Agamemnon's Docker verbs (docker, start-docker, restart-docker, stop-stale-containers,
hold-until-docker) and the release of Docker holds. Run: python -m unittest discover -s tests"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.agamemnon import holds, journal, verbs  # noqa: E402
from lib.agamemnon.verbs import Refused  # noqa: E402
import test_agamemnon_aga as base  # noqa: E402
from test_agamemnon_aga import FakeTracker, ISSUE, MY_ID, OWNER, WORKER_ID, an_issue  # noqa: E402

UP = {"cli": True, "daemon": True, "error": "", "memTotal": 100, "memUsed": 10, "containers": []}
PRESSURED = {**UP, "memUsed": 95}
DOWN = {**UP, "daemon": False, "error": "cannot connect"}


class DockerVerbsTest(base.AgaTest):
    def setUp(self):
        super().setUp()
        self.docker = [UP]
        self.engine_calls, self.stopped, self.stale = [], [], []
        self.ports.docker_read = lambda: self.docker[0]
        self.ports.docker_engine = self.engine
        self.ports.stale_containers = lambda: self.stale
        self.ports.stop_containers = lambda names: self.stopped.append(names)

    def engine(self, action):
        self.engine_calls.append(action)
        self.docker[0] = UP
        return {"action": action, "ok": True, "stackMissing": ["p4m-proxy"], "error": ""}

    def test_docker_reads_state_and_verdict(self):
        self.docker[0] = PRESSURED
        out = self.aga("everything", "docker")
        self.assertEqual(out["verdict"], "pressured")
        self.assertEqual(out["state"]["memUsed"], 95)
        self.assertEqual(journal.count(self.incident), 0)

    def test_new_verbs_have_their_tiers(self):
        self.assertFalse(verbs.VERBS["docker"].acts)
        for name, tier in (("start-docker", "engine"), ("restart-docker", "engine"),
                           ("stop-stale-containers", "free"), ("hold-until-docker", "wait")):
            self.assertEqual((verbs.VERBS[name].tier, verbs.VERBS[name].acts), (tier, True))

    def test_tier_not_granted_is_refused(self):
        with self.assertRaisesRegex(Refused, "needs tier 'engine'"):
            self.aga("ci_failed", "start-docker")

    def test_start_docker_refused_when_daemon_up(self):
        with self.assertRaisesRegex(Refused, "already up"):
            self.aga("everything", "start-docker")
        self.assertEqual(self.engine_calls, [])

    def test_start_docker_reports_missing_stack(self):
        self.docker[0] = DOWN
        out = self.aga("everything", "start-docker")
        self.assertEqual(self.engine_calls, ["start"])
        self.assertEqual(out["stackMissing"], ["p4m-proxy"])
        self.assertTrue(out["ok"])
        self.assertEqual(journal.count(self.incident), 1)

    def test_restart_refused_with_another_run_in_flight_unless_down(self):
        with self.assertRaisesRegex(Refused, "#8"):
            self.aga("everything", "restart-docker")
        self.docker[0] = PRESSURED
        with self.assertRaisesRegex(Refused, "would kill"):
            self.aga("everything", "restart-docker")
        self.assertEqual(self.engine_calls, [])
        self.docker[0] = DOWN
        self.aga("everything", "restart-docker")
        self.assertEqual(self.engine_calls, ["restart"])

    def test_restart_allowed_when_only_its_own_issue_runs(self):
        self.ports.in_flight = lambda: [ISSUE]
        self.aga("everything", "restart-docker")
        self.assertEqual(self.engine_calls, ["restart"])

    def test_stop_stale_containers_stops_and_reports(self):
        self.stale = [(3, ["c1", "c2"], "deferred"), (4, ["c3"], "unchanged for 80h")]
        out = self.aga("everything", "stop-stale-containers")
        self.assertEqual(self.stopped, [["c1", "c2"], ["c3"]])
        self.assertEqual([s["issue"] for s in out["stopped"]], [3, 4])

    def test_stop_stale_containers_refused_when_none_qualify_or_docker_unreachable(self):
        with self.assertRaisesRegex(Refused, "no test container"):
            self.aga("everything", "stop-stale-containers")

        def unreachable():
            raise OSError("no docker")
        self.ports.stale_containers = unreachable
        with self.assertRaisesRegex(Refused, "no docker"):
            self.aga("everything", "stop-stale-containers")

    def test_stop_stale_containers_keeps_going_when_one_fails(self):
        self.stale = [(3, ["c1"], "deferred"), (4, ["c3"], "deferred")]

        def stop(names):
            if names == ["c1"]:
                raise OSError("busy")
            self.stopped.append(names)
        self.ports.stop_containers = stop
        out = self.aga("everything", "stop-stale-containers")
        self.assertEqual(self.stopped, [["c3"]])
        self.assertEqual(out["failed"][0]["error"], "busy")

    def test_hold_until_docker_refusals(self):
        self.tracker.issues[ISSUE]["status"] = "in_review"
        with self.assertRaisesRegex(Refused, "in_review"):
            self.aga("everything", "hold-until-docker")
        self.tracker.issues[ISSUE]["status"] = "feedback"
        self.ports.worker_state = lambda n: {"running": True}
        with self.assertRaisesRegex(Refused, "run in flight"):
            self.aga("everything", "hold-until-docker")
        self.ports.worker_state = lambda n: {}
        self.aga("everything", "hold-until-docker")
        with self.assertRaisesRegex(Refused, "already held"):
            self.aga("everything", "hold-until-docker")

    def test_hold_until_docker_assigns_and_undo_releases(self):
        out = self.aga("everything", "hold-until-docker")
        self.assertEqual(self.tracker.issues[ISSUE]["assignee"], MY_ID)
        self.assertEqual(out["heldUntil"], "2026-11-03T10:00:00+05:00")     # 72h
        self.assertEqual(journal.active_hold(ISSUE)["condition"], "docker")
        self.assertEqual(journal.due_holds("2030-01-01T00:00:00+05:00"), [])    # not a time hold
        self.undoer().undo(out["action"])
        self.assertEqual(self.tracker.issues[ISSUE]["assignee"], OWNER)
        self.assertIsNone(journal.active_hold(ISSUE))


class DockerHoldReleaseTest(base.AgaTest):
    NOW = "2026-10-31T12:00:00+05:00"

    def setUp(self):
        super().setUp()
        self.tracker = FakeTracker([an_issue(number=n, assignee=MY_ID, status="in_progress") for n in (21, 22, 23)])
        self.ok = [True]
        for n, until in ((22, "2026-11-02T10:00:00+05:00"), (21, "2026-11-01T10:00:00+05:00"),
                         (23, "2026-11-03T10:00:00+05:00")):
            journal.hold(n, until, OWNER, journal.DOCKER)

    def release(self, now=None):
        return holds.release_due(self.tracker, WORKER_ID, MY_ID, now or self.NOW, "a",
                                 docker_ok=lambda: self.ok[0], owner=OWNER)

    def assignee(self, n):
        return self.tracker.issues[n]["assignee"]

    def test_waits_while_docker_is_not_ok(self):
        self.ok[0] = False
        self.assertEqual(self.release(), "")
        self.assertEqual(len(journal.docker_holds()), 3)

    def test_gives_back_one_per_sweep_oldest_first(self):
        self.assertIn("#21 given back", self.release())
        self.assertEqual([self.assignee(n) for n in (21, 22, 23)], [WORKER_ID, MY_ID, MY_ID])
        self.assertIn("#22 given back", self.release())
        self.assertIn("#23 given back", self.release())
        self.assertEqual(self.release(), "")

    def test_without_a_docker_check_docker_holds_stay(self):
        self.assertEqual(holds.release_due(self.tracker, WORKER_ID, MY_ID, self.NOW, "a"), "")
        self.assertEqual(len(journal.docker_holds()), 3)

    def test_deferred_and_wont_fix_end_without_giving_back(self):
        self.tracker.issues[21]["status"] = "deferred"
        self.tracker.issues[22]["status"] = "wontfix"
        note = self.release()
        self.assertIn("#21 Docker hold ended (deferred), not given back", note)
        self.assertIn("#22 Docker hold ended (wontfix)", note)
        self.assertIn("#23 given back", note)
        self.assertEqual([self.assignee(n) for n in (21, 22)], [MY_ID, MY_ID])
        self.assertEqual(journal.docker_holds(), [])

    def test_reassigned_issue_is_only_released(self):
        self.tracker.issues[21]["assignee"] = OWNER
        note = self.release()
        self.assertIn("#21 Docker hold ended (reassigned", note)
        self.assertIn("#22 given back", note)
        self.assertEqual(self.assignee(21), OWNER)

    def test_expired_hold_goes_to_the_owner_with_a_comment(self):
        self.ok[0] = False
        note = self.release("2026-11-01T10:00:00+05:00")
        self.assertIn("#21 Docker hold expired", note)
        self.assertEqual(self.assignee(21), OWNER)
        self.assertEqual(self.tracker.comments[0][0], 21)
        self.assertEqual(self.assignee(22), MY_ID)
        self.assertEqual(len(journal.docker_holds()), 2)

    def test_dry_run_changes_nothing(self):
        note = holds.release_due(self.tracker, WORKER_ID, MY_ID, self.NOW, "a", True, lambda: True, OWNER)
        self.assertIn("(dry run)", note)
        self.assertEqual(len(journal.docker_holds()), 3)
        self.assertEqual(self.tracker.assigned, [])


if __name__ == "__main__":
    unittest.main()
