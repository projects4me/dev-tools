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
from lib.orchestrator.keeper import BranchKeeper  # noqa: E402
from lib.orchestrator.merging import MergeSweeper  # noqa: E402
from lib.orchestrator.setup import MergeRoute  # noqa: E402
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

    def set_status(self, number, status):
        self.issues[number]["status"] = status

    def hand_off(self, number, status, assignee):
        self.issues[number].update(status=status, assignee=assignee)


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

    def test_finished_and_pending_handback_issues_are_not_in_flight(self):
        tracker = FakeTracker([issue(1), issue(2), issue(3, status="done", assignee=WORKER_ID),
                               issue(4, status="wont_fix", assignee=WORKER_ID),
                               issue(5, status="in_progress", assignee=WORKER_ID),
                               issue(6, status="in_progress", assignee=WORKER_ID)])
        sessions.update("odysseus", 6, pending_status="feedback", pending_assignee=OWNER)
        self.assertEqual(self.orchestrator(tracker).sweep(), [(1, "new")])     # only #5 holds a slot

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


MERGER_ID = "60"


def gate(state, *failing):
    checks = [{"name": name, "state": state, "detail": detail} for name, detail in failing]
    return {"state": state, "sha": "abc1234def", "checks": checks}


class FakeMerger:
    """Scripted `ai mergecheck` results (the last repeats) and an `ai merge` that merges when the gate is ok."""

    def __init__(self, *gates, pr=77, already_merged=False):
        self.gates, self.pr, self.merged = list(gates), pr, []
        self.already_merged = already_merged

    def find_pr(self, repo, issue):
        return {"number": self.pr, "merged": self.already_merged,
                "merge_sha": "beadfeed" if self.already_merged else None}

    def check(self, repo, pr):
        return self.gates.pop(0) if len(self.gates) > 1 else self.gates[0]

    def merge(self, repo, pr):
        result = self.check(repo, pr)
        if result["state"] == "ok":
            self.merged.append((repo, pr))
            result = {**result, "merged": "f00dfeedcafe"}
        return result


class AutoMergeTest(SweepTest):
    def setUp(self):
        super().setUp()
        log = mock.patch("lib.orchestrator.merging.log")
        log.start()
        self.addCleanup(log.stop)

    def merging(self, tracker, merger, dry_run=False, **settings):
        o = self.orchestrator(tracker, dry_run, auto_merge_milestones=("aga-v1",), **settings)
        o.merges = MergeSweeper(o.agent, o.worker, tracker, merger, MERGER_ID, [OWNER], now=lambda: self.clock[0],
                                dry_run=dry_run)
        return o

    def clean(self, number=5, **fields):
        return issue(number, **{"status": "in_review", "assignee": MERGER_ID, **fields})

    def test_policy(self):
        self.assertEqual(policy.merge_target(issue(1), ["AGA-v1"], MERGER_ID, paused=False), MERGER_ID)
        self.assertIsNone(policy.merge_target(issue(1, milestone="aga-v2"), ["aga-v1"], MERGER_ID, paused=False))
        self.assertIsNone(policy.merge_target(issue(1), ["aga-v1"], MERGER_ID, paused=True))
        self.assertTrue(policy.is_merge_candidate(self.clean(), ["aga-v1"], PROJECTS, MERGER_ID))
        self.assertFalse(policy.is_merge_candidate(self.clean(assignee=OWNER), ["aga-v1"], PROJECTS, MERGER_ID))
        self.assertEqual(policy.merge_step("ok", 0, 60, 0, 5)[0], policy.MERGE)
        self.assertEqual(policy.merge_step("pending", 10, 60, 0, 5)[0], policy.WAIT)
        self.assertEqual(policy.merge_step("pending", 61, 60, 0, 5)[0], policy.HAND_BACK)
        self.assertEqual(policy.merge_step("fail", 0, 60, 0, 5)[0], policy.HAND_BACK)
        self.assertEqual(policy.merge_step("ok", 0, 60, 5, 5)[0], policy.HAND_BACK)

    def test_merges_a_clean_pr_and_sets_the_issue_done(self):
        tracker, merger = FakeTracker([self.clean()]), FakeMerger(gate("ok"))
        self.merging(tracker, merger).sweep()
        self.assertEqual(merger.merged, [("gaia", 77)])
        self.assertEqual(tracker.issues[5]["status"], "done")
        self.assertIn("merged automatically", tracker.comments[-1][1])
        self.assertEqual([m["issue"] for m in store.merges_on("agamemnon", "2026-10-31")], [5])
        self.assertIn("merges: #5 merged PR gaia#77", store.get("agamemnon")["last_sweep"])

    def test_a_failed_gate_goes_to_the_reviewer_with_the_reason(self):
        tracker = FakeTracker([self.clean()])
        merger = FakeMerger(gate("fail", ("people", "hammad requested changes")))
        self.merging(tracker, merger).sweep()
        self.assertEqual(merger.merged, [])
        self.assertEqual((tracker.issues[5]["status"], tracker.issues[5]["assignee"]), ("in_review", OWNER))
        self.assertIn("hammad requested changes", tracker.comments[-1][2])

    def test_pending_waits_then_goes_to_the_reviewer(self):
        tracker = FakeTracker([self.clean()])
        merger = FakeMerger(gate("pending", ("ci", "still running: build")))
        o = self.merging(tracker, merger, auto_merge_wait_minutes=30)
        o.sweep()
        self.assertEqual(tracker.issues[5]["assignee"], MERGER_ID)            # still waiting
        self.clock[0] = "2026-10-31T10:31:00+05:00"
        o.sweep()
        self.assertEqual(tracker.issues[5]["assignee"], OWNER)
        self.assertIn("still pending after 31 min", tracker.comments[-1][2])

    def test_daily_limit(self):
        tracker = FakeTracker([self.clean(5), self.clean(6)])
        self.merging(tracker, FakeMerger(gate("ok")), max_auto_merges_per_day=1).sweep()
        self.assertEqual(tracker.issues[5]["status"], "done")
        self.assertEqual((tracker.issues[6]["status"], tracker.issues[6]["assignee"]), ("in_review", OWNER))
        self.assertIn("daily limit", tracker.comments[-1][2])

    def test_only_its_own_auto_merge_milestones(self):
        tracker = FakeTracker([self.clean(5, milestone="aga-v2"), self.clean(6, assignee=OWNER)])
        merger = FakeMerger(gate("ok"))
        self.merging(tracker, merger).sweep()
        self.assertEqual(merger.merged, [])

    def test_a_paused_orchestrator_hands_its_merges_to_the_owner(self):
        tracker, merger = FakeTracker([self.clean()]), FakeMerger(gate("ok"))
        store.update("agamemnon", paused=True)
        self.merging(tracker, merger).sweep()
        self.assertEqual(merger.merged, [])
        self.assertEqual(tracker.issues[5]["assignee"], OWNER)
        self.assertIn("is paused", tracker.comments[-1][2])

    def test_dry_run_merges_nothing_and_starts_no_wait(self):
        tracker, merger = FakeTracker([self.clean()]), FakeMerger(gate("ok"))
        self.merging(tracker, merger, dry_run=True).sweep()
        self.assertEqual((merger.merged, tracker.issues[5]["status"], tracker.comments), ([], "in_review", []))
        self.assertIsNone(store.find_open_merge("agamemnon", 5, 77))

    def test_a_pr_already_merged_is_finished_not_handed_back(self):
        tracker = FakeTracker([self.clean()])
        o = self.merging(tracker, FakeMerger(gate("pending")))
        o.sweep()                                   # starts the wait record
        o.merges.merger = FakeMerger(gate("ok"), already_merged=True)   # merged, but setting done failed last time
        o.sweep()
        self.assertEqual(tracker.issues[5]["status"], "done")
        self.assertEqual([m["merge_sha"] for m in store.merges_on("agamemnon", "2026-10-31")], ["beadfeed"])
        self.assertIn("merged automatically", tracker.comments[-1][1])

    def test_route_reads_the_merger_lazily_and_retries(self):
        calls = []

        def user_id():
            calls.append(1)
            if len(calls) == 1:
                raise TalosError("Gaia down")
            return MERGER_ID
        agent = SimpleNamespace(name="agamemnon", display_name="Agent Agamemnon",
                                orchestration=registry.Orchestration(worker="odysseus",
                                                                     auto_merge_milestones=("aga-v1",)))
        paused = [False]
        route = MergeRoute(agent, user_id, lambda: paused[0])
        with mock.patch("lib.orchestrator.setup.log"):
            self.assertIsNone(route(issue(1)))                   # Gaia down: the reviewer, retried next time
        self.assertEqual(route(issue(1)), (MERGER_ID, "Agent Agamemnon"))
        self.assertIsNone(route(issue(1, milestone="aga-v2")))
        paused[0] = True
        self.assertIsNone(route(issue(1)))
        self.assertEqual(len(calls), 2)


def gh_pr(number, issue_number, mergeable="MERGEABLE", merge_state="BLOCKED", head="h1"):
    return {"number": number, "headRefName": f"claude-issue-{issue_number}", "headRefOid": head,
            "mergeable": mergeable, "mergeStateStatus": merge_state}


class FakeGh:
    """`gh` as Agamemnon: lists the scripted open PRs and records the update-branch calls."""

    def __init__(self, prs):
        self.prs, self.updates = prs, []

    def __call__(self, *args):
        if args[:2] == ("pr", "list"):
            return json.dumps(self.prs)
        if args[:3] == ("api", "-X", "PUT"):
            self.updates.append(args[3])
            return "{}"
        raise AssertionError(f"unexpected gh call {args}")


class KeeperTest(SweepTest):
    def setUp(self):
        super().setUp()
        log = mock.patch("lib.orchestrator.keeper.log")
        log.start()
        self.addCleanup(log.stop)

    def keeper(self, tracker, gh, dry_run=False, max_conflict_attempts=1):
        o = self.orchestrator(tracker, dry_run, max_conflict_attempts=max_conflict_attempts)
        return BranchKeeper(o.agent, o.worker, tracker, gh, WORKER_ID, MERGER_ID, [OWNER],
                            lambda repo, number: self.holder, now=lambda: self.clock[0], dry_run=dry_run)

    def waiting(self, number=5, **fields):
        return issue(number, **{"status": "in_review", "assignee": OWNER, **fields})

    def test_policy(self):
        self.assertEqual(policy.issue_of_branch("claude-issue-4214"), 4214)
        self.assertIsNone(policy.issue_of_branch("feature/x"))
        self.assertEqual(policy.mergeability(gh_pr(1, 5, "CONFLICTING", "DIRTY")), policy.CONFLICT)
        self.assertEqual(policy.mergeability(gh_pr(1, 5, merge_state="BEHIND")), policy.BEHIND)
        self.assertEqual(policy.mergeability(gh_pr(1, 5, "UNKNOWN", "UNKNOWN")), policy.UNKNOWN)
        self.assertEqual(policy.mergeability(gh_pr(1, 5)), policy.UP_TO_DATE)
        self.assertEqual(policy.conflict_step(0, 1, None, "h1"), policy.RESOLVE)
        self.assertEqual(policy.conflict_step(1, 1, None, "h1"), policy.TELL)
        self.assertEqual(policy.conflict_step(1, 1, "h1", "h1"), policy.NOTHING)
        mergeable_only = {"state": "fail", "checks": [{"name": "mergeable", "state": "fail"},
                                                      {"name": "ci", "state": "ok"}]}
        self.assertTrue(policy.only_mergeability_fails(mergeable_only))
        self.assertFalse(policy.only_mergeability_fails(
            {"state": "fail", "checks": [{"name": "mergeable", "state": "fail"}, {"name": "ci", "state": "fail"}]}))

    def test_a_branch_behind_its_base_is_updated_on_github(self):
        gh = FakeGh([gh_pr(77, 5, merge_state="BEHIND", head="abc")])
        self.keeper(FakeTracker([self.waiting()]), gh).sweep()
        self.assertEqual(gh.updates, ["repos/Projects4Me/gaia/pulls/77/update-branch"])

    def test_a_conflict_goes_to_the_worker_with_the_conflict_action(self):
        tracker = FakeTracker([self.waiting()])
        self.keeper(tracker, FakeGh([gh_pr(77, 5, "CONFLICTING", "DIRTY")])).sweep()
        self.assertEqual(tracker.issues[5]["assignee"], WORKER_ID)
        state = sessions.get("odysseus", 5)
        self.assertEqual((state["next_action"], state["conflict_attempts"]), ("merge_conflict", 1))
        self.assertIn("merge conflict", tracker.comments[-1][1])

    def test_used_up_attempts_tell_the_owner_once_per_head(self):
        sessions.update("odysseus", 5, conflict_attempts=1)
        tracker = FakeTracker([self.waiting(assignee=MERGER_ID)])       # it was waiting to be merged automatically
        keeper = self.keeper(tracker, FakeGh([gh_pr(77, 5, "CONFLICTING", "DIRTY", head="h1")]))
        keeper.sweep()
        keeper.sweep()
        self.assertEqual(tracker.issues[5]["assignee"], OWNER)
        self.assertEqual(len(tracker.comments), 1)
        self.assertIn("needs a person", tracker.comments[0][1])

    def test_a_failed_assignment_gives_the_attempt_back(self):
        tracker = FakeTracker([self.waiting()])

        def down(number, assignee):
            raise TalosError("Gaia down")
        tracker.assign = down
        self.keeper(tracker, FakeGh([gh_pr(77, 5, "CONFLICTING", "DIRTY")])).sweep()
        state = sessions.get("odysseus", 5)
        self.assertEqual((state["next_action"], state["conflict_attempts"]), ("", 0))

    def test_leaves_issues_the_worker_or_a_person_has(self):
        prs = [gh_pr(77, 5, "CONFLICTING", "DIRTY"), gh_pr(78, 6, merge_state="BEHIND")]
        running = FakeTracker([self.waiting(5, assignee=WORKER_ID, status="in_progress"), self.waiting(6)])
        gh = FakeGh(prs)
        self.holder = "D:/work/gaia"                     # claude-issue-6 checked out by hand
        self.keeper(running, gh).sweep()
        self.assertEqual((gh.updates, running.assigned), ([], []))

    def test_dry_run_changes_nothing(self):
        tracker = FakeTracker([self.waiting(5), self.waiting(6)])
        gh = FakeGh([gh_pr(77, 5, "CONFLICTING", "DIRTY"), gh_pr(78, 6, merge_state="BEHIND")])
        self.keeper(tracker, gh, dry_run=True).sweep()
        self.assertEqual((gh.updates, tracker.assigned, tracker.comments), ([], [], []))
        self.assertEqual(sessions.get("odysseus", 5), {})

    def test_a_merge_waiting_on_mergeability_waits_for_the_keeper(self):
        tracker = FakeTracker([issue(5, status="in_review", assignee=MERGER_ID)])
        conflicted = gate("fail", ("mergeable", "conflicts with the base branch"))
        o = AutoMergeTest.merging(self, tracker, FakeMerger(conflicted))
        o.sweep()
        self.assertEqual(tracker.issues[5]["assignee"], MERGER_ID)   # not handed back: the keeper takes it
        sessions.update("odysseus", 5, conflict_noted="abc1234def")  # the keeper gave up at this head
        o.sweep()
        self.assertEqual(tracker.issues[5]["assignee"], OWNER)
        self.assertIn("conflicts with the base branch", tracker.comments[-1][2])


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
