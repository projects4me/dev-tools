"""Orchestrator (Agent Agamemnon): the pure policy, the registry entry, and sweeps against a fake tracker."""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import db, registry, sessions  # noqa: E402
from lib.config import TalosError  # noqa: E402
from lib.orchestrator import github, policy  # noqa: E402
from lib.orchestrator import state as store  # noqa: E402
from lib.orchestrator.worker import Orchestrator  # noqa: E402
from lib.policy import ASSIGNEE_EVENT  # noqa: E402

AGAMEMNON = Path(__file__).resolve().parent.parent / "agents" / "agamemnon"
OWNER, WORKER_ID = "7", "50"
RETRY = registry.AUTO_RETRY_REASONS
PROJECTS = ["A Gaia"]


def issue(number, **fields):
    base = {"number": number, "project": "A Gaia", "milestone": "aga-v1", "status": "new", "assignee": "",
            "modifiedUser": OWNER, "priority": "medium", "type": "story", "dependsOn": []}
    return {**base, **fields}


class PolicyTest(unittest.TestCase):
    def test_new_candidate(self):
        ok = issue(1, milestone="AGA-v2")       # the prefix is matched in any case
        self.assertTrue(policy.is_new_candidate(ok, "aga-", PROJECTS, [OWNER], {}))
        self.assertTrue(policy.is_new_candidate(issue(1, assignee=OWNER), "aga-", PROJECTS, [OWNER], {}))
        for bad, why in ((issue(1, milestone="Claude-v1"), "aga-"), (issue(1, project="B"), "aga-"),
                         (issue(1, status="in_progress"), "not new"), (issue(1, assignee="99"), "someone"),
                         (issue(1, type="Epic"), "epic"), (issue(1, dependsOn=[9]), "#9")):
            self.assertIn(why, policy.new_skip_reason(bad, "aga-", PROJECTS, [OWNER], {9: "in_progress"}))
        self.assertTrue(policy.is_new_candidate(issue(1, dependsOn=[9]), "aga-", PROJECTS, [OWNER], {9: "Done"}))

    def restart(self, i, state, holder=None):
        return policy.restart_skip_reason(i, state, "aga-", PROJECTS, [OWNER], WORKER_ID, RETRY, 2, holder)

    def test_restart_candidate(self):
        back = issue(1, status="feedback", assignee=OWNER, modifiedUser=WORKER_ID)
        state = {"handoff_reason": "turn_budget", "auto_retries": 1}
        self.assertIsNone(self.restart(back, state))
        self.assertIsNone(self.restart({**back, "status": "Pending"}, state))
        self.assertIn("changed by someone", self.restart({**back, "modifiedUser": OWNER}, state))
        self.assertIn("not restarted", self.restart(back, {"handoff_reason": "agent_needs_input"}))
        self.assertIn("2 time", self.restart(back, {**state, "auto_retries": 2}))
        self.assertIn("owner", self.restart({**back, "assignee": WORKER_ID}, state))
        self.assertIn("in_progress", self.restart({**back, "status": "in_progress"}, state))
        self.assertIn("aga-", self.restart({**back, "milestone": "Claude-v1"}, state))
        branch = {"handoff_reason": "branch_checked_out"}
        self.assertIn("D:/x", self.restart(back, branch, holder="D:/x"))
        self.assertIsNone(self.restart(back, branch, holder=None))

    def test_rank_priority_then_milestone_then_number(self):
        issues = [issue(5, priority="low"), issue(4, priority="high", milestone="aga-v2"),
                  issue(3, priority="high", milestone="aga-v1"), issue(2, priority="?"), issue(1, priority="Blocker")]
        self.assertEqual([i["number"] for i in policy.rank(issues)], [1, 3, 4, 5, 2])

    def test_free_slots(self):
        self.assertEqual(policy.free_slots(2, 0), 2)
        self.assertEqual(policy.free_slots(2, 3), 0)

    def test_fail_streak_and_breaker(self):
        fail = {"kind": policy.HANDBACK, "reason": "run_error"}
        events = [fail, {"kind": policy.HANDBACK, "reason": "turn_budget", "auto_retries": 0},   # restartable
                  {"kind": policy.HANDBACK, "reason": "agent_needs_input"}, fail]
        self.assertEqual(policy.fail_streak(0, events, RETRY, 2), 2)
        exhausted = {"kind": policy.HANDBACK, "reason": "turn_budget", "auto_retries": 2}
        self.assertEqual(policy.fail_streak(1, [exhausted], RETRY, 2), 2)
        self.assertEqual(policy.fail_streak(2, [{"kind": policy.SUCCESS}, fail], RETRY, 2), 1)
        self.assertTrue(policy.breaker_tripped(3, 3))
        self.assertFalse(policy.breaker_tripped(2, 3))


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp()) / "agamemnon"
        self.folder.mkdir()
        shutil.copy(AGAMEMNON / "agent.json", self.folder / "agent.json")
        self.local_dir = Path(tempfile.mkdtemp())
        (self.local_dir / "gaia-mcp").mkdir()
        (self.local_dir / "gaia-mcp" / "agamemnon.env").write_text("GAIA_EMAIL=x\n", encoding="utf-8")
        patcher = mock.patch.object(registry, "LOCAL_DIR", self.local_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, **changes):
        raw = json.loads((AGAMEMNON / "agent.json").read_text(encoding="utf-8"))
        raw.update(changes)
        (self.folder / "agent.json").write_text(json.dumps(raw), encoding="utf-8")

    def test_shipped_agent_loads_disabled_with_defaults(self):
        agent = registry.load_agent(self.folder)
        o = agent.orchestration
        self.assertEqual((agent.kind, agent.enabled, o.worker, o.milestone_prefix), ("orchestrator", False,
                                                                                     "odysseus", "aga-"))
        self.assertEqual((o.max_in_flight, o.sweep_minutes, o.max_auto_retries, o.fail_streak_limit), (0, 5, 2, 3))
        self.assertEqual(o.auto_retry_reasons, RETRY)

    def test_validation(self):
        raw = json.loads((AGAMEMNON / "agent.json").read_text(encoding="utf-8"))
        del raw["worker"]
        (self.folder / "agent.json").write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaisesRegex(TalosError, "missing worker"):
            registry.load_agent(self.folder)
        for changes, message in (({"failStreakLimit": 0}, "failStreakLimit"), ({"maxInFlight": "x"}, "maxInFlight"),
                                 ({"milestonePrefix": " "}, "milestonePrefix"), ({"sweepMinutes": 0}, "sweepMinutes"),
                                 ({"autoRetryReasons": "turn_budget"}, "autoRetryReasons"),
                                 ({"gaiaEnv": "gaia-mcp/none.env"}, "gaiaEnv")):
            self.write(**changes)
            with self.assertRaisesRegex(TalosError, message):
                registry.load_agent(self.folder)


class FakeTracker:
    def __init__(self, issues, dependencies=None):
        self.issues = {i["number"]: dict(i) for i in issues}
        self.dependencies = dependencies or {}      # number -> status of issues outside the sweep
        self.assigned, self.comments = [], []

    def project_ids(self):
        return {"A Gaia": "1"}

    def assigned_issue_numbers(self, project_id, user_id):
        return [n for n, i in self.issues.items() if i["assignee"] == user_id]

    def issues_in(self, project, prefix):
        return [dict(i) for i in self.issues.values() if i["project"] == project]   # the sweep filters milestones

    def issue(self, number):
        return dict(self.issues[number])

    def statuses_of(self, numbers):
        return {n: self.dependencies.get(n) or (self.issues.get(n) or {}).get("status") for n in numbers}

    def assign(self, number, assignee):
        self.issues[number].update(assignee=assignee, modifiedUser="60")
        self.assigned.append(number)

    def comment(self, number, title, body):
        self.comments.append((number, title, body))


class SweepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        patcher = mock.patch.object(db, "DB_FILE", self.tmp / "talos.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        log = mock.patch("lib.orchestrator.worker.log")
        log.start()
        self.addCleanup(log.stop)
        self.clock = ["2026-10-31T10:00:00+05:00"]
        self.holder = None

    def orchestrator(self, tracker, dry_run=False, **settings):
        o = registry.Orchestration(worker="odysseus", **{"max_in_flight": 2, **settings})
        agent = SimpleNamespace(name="agamemnon", display_name="Agent Agamemnon", orchestration=o)
        worker = SimpleNamespace(name="odysseus", display_name="Agent Odysseus", handoff_to=OWNER, max_parallel=1,
                                 projects={"A Gaia": "gaia"}, dependency_done_statuses=("done", "closed"),
                                 repo_for=lambda project: "gaia")
        return Orchestrator(agent, worker, tracker, WORKER_ID, dry_run=dry_run,
                            branch_holder=lambda repo, number: self.holder, now=lambda: self.clock[0])

    def handed_back(self, number, reason, retries=0, **fields):
        sessions.update("odysseus", number, handoff_reason=reason, auto_retries=retries,
                        handoff_at=self.clock[0])
        return issue(number, status="feedback", assignee=OWNER, modifiedUser=WORKER_ID, **fields)

    def test_capacity(self):
        tracker = FakeTracker([issue(1), issue(2), issue(3), issue(4, status="in_progress", assignee=WORKER_ID)])
        self.assertEqual(self.orchestrator(tracker).sweep(), [(1, "new")])
        self.assertEqual(self.orchestrator(tracker).sweep(), [])          # now full
        self.assertIn("no free slot", store.get("agamemnon")["last_sweep"])

    def test_max_in_flight_defaults_to_worker_max_parallel(self):
        tracker = FakeTracker([issue(1), issue(2)])
        self.assertEqual(self.orchestrator(tracker, max_in_flight=0).sweep(), [(1, "new")])

    def test_restarts_before_new_work_and_comment(self):
        tracker = FakeTracker([issue(1, priority="blocker"), self.handed_back(2, "turn_budget", priority="low")])
        self.assertEqual(self.orchestrator(tracker, max_in_flight=1).sweep(), [(2, "restart")])
        self.assertEqual(tracker.issues[2]["status"], "feedback")       # only the assignee changes
        self.assertEqual(sessions.get("odysseus", 2)["auto_retries"], 1)
        number, _, body = tracker.comments[0]
        self.assertEqual(number, 2)
        self.assertIn("restart 1 of 2", body)
        self.assertIn("nothing new from the reviewer", body)

    def test_retry_cap(self):
        tracker = FakeTracker([self.handed_back(2, "talos_error", retries=2)])
        self.assertEqual(self.orchestrator(tracker).sweep(), [])

    def test_skips_when_the_reviewer_modified_the_issue(self):
        back = self.handed_back(2, "interrupted")
        tracker = FakeTracker([{**back, "modifiedUser": OWNER}])
        self.assertEqual(self.orchestrator(tracker).sweep(), [])

    def test_waits_while_the_branch_is_checked_out_elsewhere(self):
        tracker = FakeTracker([self.handed_back(2, "branch_checked_out")])
        sessions.update("odysseus", 2, handoff_detail=json.dumps({"repo": "gaia", "branch": "claude-issue-2"}))
        self.holder = "D:/Pr4M/gaia"
        self.assertEqual(self.orchestrator(tracker).sweep(), [])
        self.holder = None
        self.assertEqual(self.orchestrator(tracker).sweep(), [(2, "restart")])

    def test_dependency_epic_and_milestone_skips(self):
        tracker = FakeTracker([issue(1, dependsOn=[9]), issue(2, type="epic"), issue(3, milestone="Claude-v1"),
                               issue(4, dependsOn=[8])], dependencies={9: "in_progress", 8: "done"})
        self.assertEqual(self.orchestrator(tracker).sweep(), [(4, "new")])

    def test_rechecks_before_assigning(self):
        tracker = FakeTracker([issue(1)])
        o = self.orchestrator(tracker)
        real = tracker.issue
        tracker.issue = lambda n: {**real(n), "status": "in_progress"}     # changed since the list was read
        self.assertEqual(o.sweep(), [])
        self.assertEqual(tracker.assigned, [])

    def test_breaker_pauses_after_failed_handbacks(self):
        tracker = FakeTracker([])
        o = self.orchestrator(tracker)
        o.sweep()                                   # starts counting from now
        self.clock[0] = "2026-10-31T10:05:00+05:00"
        for n in (1, 2, 3):
            store.record_assignment("agamemnon", n, "odysseus", self.clock[0])
        self.clock[0] = "2026-10-31T10:30:00+05:00"
        self.handed_back(1, "run_error")
        self.handed_back(2, "turn_budget")          # still restartable: not a failure
        self.handed_back(3, "agent_blocked")
        self.handed_back(4, "run_error")            # not fed by the orchestrator
        tracker.issues = {5: issue(5)}
        self.assertEqual(o.sweep(), [(5, "new")])
        self.assertEqual(store.get("agamemnon")["fail_streak"], 2)
        self.clock[0] = "2026-10-31T10:40:00+05:00"
        self.handed_back(2, "iteration_limit")
        tracker.issues[6] = issue(6)
        tracker.issues[5]["assignee"] = OWNER
        self.assertEqual(o.sweep(), [])
        state = store.get("agamemnon")
        self.assertTrue(state["paused"])
        self.assertIn("circuit breaker", state["pause_reason"])

    def test_paused_assigns_nothing(self):
        store.update("agamemnon", paused=True, pause_reason="talos pause")
        tracker = FakeTracker([issue(1)])
        self.assertEqual(self.orchestrator(tracker).sweep(), [])
        store.update("agamemnon", paused=False)
        self.assertEqual(self.orchestrator(tracker).sweep(), [(1, "new")])

    def test_dry_run_only_logs(self):
        tracker = FakeTracker([issue(1), self.handed_back(2, "cost_budget")])
        self.assertEqual(self.orchestrator(tracker, dry_run=True).sweep(), [(2, "restart"), (1, "new")])
        self.assertEqual((tracker.assigned, tracker.comments), ([], []))
        self.assertEqual(sessions.get("odysseus", 2)["auto_retries"], 0)
        self.assertEqual(store.get("agamemnon"), {})

    def test_events_wake_the_sweep(self):
        o = self.orchestrator(FakeTracker([]))
        o.on_event({"eventName": "issue.comment.created"})
        self.assertFalse(o._wake.is_set())
        o.on_event({"eventName": ASSIGNEE_EVENT})
        self.assertTrue(o._wake.is_set())


class GithubIdentityTest(unittest.TestCase):
    WORKER = SimpleNamespace(projects={"A Gaia": "gaia", "A Prometheus": "prometheus", "A Other": "not-a-repo"})

    def gh(self, push):
        def run(*args):
            if args[:2] == ("api", "user"):
                return "agent-agamemnon\n"
            repo = args[1].split("/")[-1]
            return json.dumps({"pull": True, "push": push.get(repo, False)})
        return run

    def test_reports_login_and_repos_it_can_merge_in(self):
        result = github.check_identity(self.WORKER, self.gh({"gaia": True, "prometheus": True}))
        self.assertEqual(result, "agent-agamemnon, write access to gaia, prometheus")

    def test_names_repos_without_write_access(self):
        with self.assertRaises(TalosError) as e:
            github.check_identity(self.WORKER, self.gh({"gaia": True}))
        self.assertIn("no write access to prometheus", str(e.exception))


if __name__ == "__main__":
    unittest.main()
