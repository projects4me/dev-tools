"""Dispatcher with a stub tracker and runner: iteration limit, model choice, escalation, hand-offs."""

import sys
import json
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.config import TalosError  # noqa: E402
from lib import db, dispatcher as dispatcher_mod, policy, runs as run_log, sessions  # noqa: E402
from lib.registry import Action  # noqa: E402
from test_policy import AGENT_ID, make_agent, make_issue  # noqa: E402


class StubTracker:
    def __init__(self, issue):
        self._issue = issue
        self.comments, self.handoffs, self.statuses = [], [], []
        self.dependencies = {}   # dependency issue number -> status

    def user_id(self):
        return AGENT_ID

    def issue(self, number):
        return dict(self._issue)

    def comment(self, number, title, body):
        self.comments.append(title)

    def set_status(self, number, status):
        self.statuses.append(status)
        self._issue["status"] = status

    def hand_off(self, number, status, assignee):
        self.handoffs.append((status, assignee))
        self._issue["assignee"] = assignee

    def child_env(self):
        return {}

    def statuses_of(self, numbers):
        if getattr(self, "unreadable", False):
            raise TalosError("Gaia is down")
        self.read = list(getattr(self, "read", [])) + list(numbers)
        return {n: self.dependencies.get(n) for n in numbers}


def result(outcome, session="s1", handoff=""):
    return {"subtype": "success", "session_id": session,
            "structured_output": {"outcome": outcome, "summary": outcome, "comment": "<p>why</p>",
                                  "handoff": handoff}}


class DispatcherTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.patches = [mock.patch.object(db, "DB_FILE", tmp / "talos.db"),
                        mock.patch.object(dispatcher_mod, "log", lambda *a, **k: None),  # keep talos.log real
                        mock.patch.object(dispatcher_mod, "lease_file", lambda name: tmp / f"{name}-leases.json"),
                        mock.patch.object(dispatcher_mod.runner.workspaces, "ensure",
                                          lambda root, tools, number, repo: tmp / f"issue-{number}")]
        for p in self.patches:
            p.start()
        self.models = []
        self.calls = []
        self.envs = []
        self.cwds = []
        self.memories = []

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def dispatch(self, issue, outcomes, holder=None, **agent_overrides):
        tracker = StubTracker(issue)
        replies = iter(outcomes)

        def fake_run(cmd, env, agent, number, prompt, cwd=None):
            self.calls[-1]["prompt"] = prompt
            self.cwds.append(cwd)
            self.models.append(cmd[cmd.index("--model") + 1])
            self.envs.append(env)
            return next(replies), Path("log.json")

        def fake_build(claude, agent, model, sid=None, new_session_id=None, limits=None, mcp=None, memory=None,
                       guard=None):
            self.calls.append({"resume": sid, "limits": limits})
            self.memories.append(memory)
            return ["claude", "--model", model]

        with mock.patch.object(dispatcher_mod.runner, "run", fake_run), \
                mock.patch.object(dispatcher_mod.runner, "build_command", fake_build):
            d = dispatcher_mod.Dispatcher(make_agent(**agent_overrides), tracker, "claude")
            if holder is not None:
                holder["d"] = d
            return d.handle(42), tracker

    def test_run_shares_the_workspaces_memory(self):
        with mock.patch.dict(dispatcher_mod.memory.os.environ, {"CLAUDE_CONFIG_DIR": "cfg"}),                 mock.patch.object(dispatcher_mod, "WORKSPACE", Path("/srv/Pr4M")):
            self.dispatch(make_issue(), [result("needs_input")])
        self.assertEqual(self.memories[0], Path("cfg") / "projects" / "-srv-Pr4M" / "memory")

    def runtime_dispatch(self, after_entries, outcome="done"):
        """A run from a runtime copy whose `git status` reads after_entries once the run ends."""
        check = dispatcher_mod.runtime_check
        before = check.Snapshot("a" * 40, "", frozenset())
        after = check.Snapshot("a" * 40, "", frozenset(after_entries))
        snapshots = iter([before, after])
        log_file = Path(tempfile.mkdtemp()) / "run.json"
        log_file.write_text("{}", encoding="utf-8")

        def fake_run(cmd, env, agent, number, prompt, cwd=None):
            self.calls[-1]["prompt"] = prompt
            return result(outcome), log_file

        def fake_build(*args, **kwargs):
            self.calls.append(kwargs)
            return ["claude", "--model", args[2]]

        tracker = StubTracker(make_issue())
        with mock.patch.object(dispatcher_mod, "_runtime_copy", lambda: Path("rt")), \
                mock.patch.object(check, "snapshot", lambda copy: next(snapshots)), \
                mock.patch.object(check, "report", lambda copy, b, found: "the diff\n"), \
                mock.patch.object(check, "restore", mock.Mock(return_value=[])) as restore, \
                mock.patch.object(dispatcher_mod.runner, "run", fake_run), \
                mock.patch.object(dispatcher_mod.runner, "build_command", fake_build):
            outcome = dispatcher_mod.Dispatcher(make_agent(), tracker, "claude").handle(42)
        return outcome, tracker, restore, log_file.read_text(encoding="utf-8")

    def test_run_that_changed_the_runtime_copy_is_restored_and_blocked(self):
        outcome, tracker, restore, log_text = self.runtime_dispatch({(" M", "ai/lib/tests.py")})
        self.assertEqual(outcome, "blocked")
        self.assertEqual(tracker.comments, ["Agent Odysseus: changed the tooling in place"])
        self.assertEqual(tracker.handoffs[0][0], make_agent().statuses["blocked"])
        self.assertTrue(restore.called)
        self.assertIn("the diff", log_text)
        self.assertEqual(self.calls[0]["guard"], Path("rt"))     # the hook guards the same copy

    def test_run_that_left_the_runtime_copy_alone_ends_as_reported(self):
        outcome, tracker, restore, _ = self.runtime_dispatch(set(), outcome="needs_input")
        self.assertEqual(outcome, "needs_input")
        self.assertFalse(restore.called)

    def test_run_leases_a_slot_for_its_environment_and_releases_it(self):
        outcome, _ = self.dispatch(make_issue(), [result("needs_input")], max_parallel=2, test_slots=2)
        self.assertEqual(self.envs[0]["P4M_TEST_SLOT"], "1")
        self.assertEqual(outcome, "needs_input")

    def test_run_outside_gaia_leases_no_slot(self):
        self.dispatch(make_issue(project="A Prometheus"), [result("needs_input")],
                      projects={"A Gaia": "gaia", "A Prometheus": "prometheus"}, max_parallel=2, test_slots=2)
        self.assertNotIn("P4M_TEST_SLOT", self.envs[0])

    def test_single_worker_leases_no_slot(self):
        self.dispatch(make_issue(), [result("needs_input")], max_parallel=1, test_slots=2)
        self.assertNotIn("P4M_TEST_SLOT", self.envs[0])

    def feedback_agent(self):
        return dict(actions={"feedback": make_agent().actions["feedback"]})

    def test_usage_limit_pauses_and_requeues_without_counting_an_iteration(self):
        reset = dispatcher_mod._now() + timedelta(hours=3)
        limit = {"is_error": True, "subtype": "success", "session_id": "s1",
                 "result": f"Claude AI usage limit reached|{int(reset.timestamp())}"}
        d_holder = {}
        outcome, tracker = self.dispatch(make_issue(), [limit], holder=d_holder)
        self.assertEqual(outcome, policy.LIMITED)
        self.assertEqual(tracker.handoffs, [])
        self.assertEqual(tracker._issue["assignee"], AGENT_ID)
        self.assertEqual(sessions.get("odysseus", 42)["runs"], 0)
        self.assertEqual(run_log.recent(1)[0]["outcome"], policy.LIMITED)
        self.assertGreater(d_holder["d"]._pause.until, reset)
        self.assertEqual(len(tracker.comments), 1)   # 3h pause is longer than the 1h default

    def new_dispatcher(self, **agent_overrides):
        return dispatcher_mod.Dispatcher(make_agent(**agent_overrides), StubTracker(make_issue()), "claude")

    def test_workers_hitting_a_limit_together_make_one_pause_and_one_comment(self):
        reset = dispatcher_mod._now() + timedelta(hours=3)
        limit = {"is_error": True, "subtype": "success", "session_id": "s1",
                 "result": f"Claude AI usage limit reached|{int(reset.timestamp())}"}
        d = self.new_dispatcher()
        outcomes = []
        with mock.patch.object(dispatcher_mod.runner, "run", lambda *a, **k: (limit, Path("log.json"))),                 mock.patch.object(dispatcher_mod.runner, "build_command", lambda *a, **k: ["claude", "--model", "m"]):
            threads = [threading.Thread(target=lambda n=n: outcomes.append(d.handle(n))) for n in (41, 42, 43)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        self.assertEqual(outcomes, [policy.LIMITED] * 3)
        self.assertEqual(len(d.tracker.comments), 1)
        self.assertEqual(d._pause._streak, 1)

    def test_a_limited_issue_goes_back_to_the_front(self):
        d = self.new_dispatcher()
        d.submit(1, "a")
        d.submit(2, "b")
        first = d._queue.get(d._rank)
        d._queue.put(None, first, front=True)
        d.submit(3, "c")
        self.assertEqual([d._queue.get(d._rank)[0] for _ in range(3)], [first[0], 2, 3])

    def test_queue_order_uses_the_priority_at_pop_time(self):
        d = self.new_dispatcher()
        calls = []
        d.tracker.issue = lambda n: calls.append(n) or make_issue(priority="low" if n == 1 else "medium")
        d.submit(1, "a")
        d.submit(2, "b")
        self.assertEqual(calls, [])                      # nothing is read when queueing
        d.tracker.issue = lambda n: make_issue(priority="blocker" if n == 1 else "low")
        self.assertEqual(d._queue.get(d._rank)[0], 1)    # raised while it waited

    def test_warm_session_is_resumed_and_run_cost_is_incremental(self):
        recent = dispatcher_mod._iso(dispatcher_mod._now() - timedelta(minutes=10))
        sessions.update("odysseus", 42, session_id="s1", session_cost_usd=1.46, last_ended_at=recent)
        reply = result("needs_input", session="s1")
        reply["total_cost_usd"] = 1.67
        self.dispatch(make_issue(status="feedback"), [reply], **self.feedback_agent())
        self.assertEqual(self.calls[0]["resume"], "s1")
        run = run_log.recent(1)[0]
        self.assertAlmostEqual(run["cost_usd"], 0.21)
        self.assertAlmostEqual(run["session_cost_usd"], 1.67)
        self.assertEqual(run["resumed"], 1)
        self.assertEqual(sessions.get("odysseus", 42)["session_cost_usd"], 1.67)

    def test_cold_session_starts_fresh_with_catch_up(self):
        old = dispatcher_mod._iso(dispatcher_mod._now() - timedelta(hours=3))
        sessions.update("odysseus", 42, session_id="s1", session_cost_usd=1.46, last_ended_at=old)
        reply = result("needs_input", session="s2")
        reply["total_cost_usd"] = 0.30
        self.dispatch(make_issue(status="feedback"), [reply], **self.feedback_agent())
        self.assertIsNone(self.calls[0]["resume"])
        self.assertIn("not resumed", self.calls[0]["prompt"])
        self.assertAlmostEqual(run_log.recent(1)[0]["cost_usd"], 0.30)
        self.assertEqual(sessions.get("odysseus", 42)["session_id"], "s2")

    def test_runner_without_sessions_starts_warm_issues_cold_with_catch_up(self):
        recent = dispatcher_mod._iso(dispatcher_mod._now() - timedelta(minutes=10))
        sessions.update("odysseus", 42, session_id="s1", session_cost_usd=1.46, last_ended_at=recent)
        cold = dispatcher_mod.runner.LocalRunner()
        cold.resumable = False
        with mock.patch.object(dispatcher_mod.runner, "for_agent", lambda agent: cold):
            self.dispatch(make_issue(status="feedback"), [result("needs_input", session="s2")],
                          **self.feedback_agent())
        self.assertIsNone(self.calls[0]["resume"])
        self.assertIn("not resumed", self.calls[0]["prompt"])

    def test_an_orchestrators_action_runs_once_instead_of_the_status_action(self):
        sessions.update("odysseus", 42, next_action="merge_conflict")
        actions = {"new": Action("Work #{issue}"), "merge_conflict": Action("Resolve the conflict on #{issue}")}
        self.dispatch(make_issue(status="new"), [result("needs_input")], actions=actions)
        self.assertEqual(self.calls[0]["prompt"], "Resolve the conflict on #42")
        self.assertEqual(sessions.get("odysseus", 42)["next_action"], "")     # used by this run only

    def test_an_orchestrators_action_is_dropped_when_the_issue_is_not_run(self):
        sessions.update("odysseus", 42, next_action="merge_conflict")
        actions = {"new": Action("Work #{issue}"), "merge_conflict": Action("Resolve #{issue}")}
        self.dispatch(make_issue(status="new", assignee="someone-else"), [], actions=actions)
        self.assertEqual((self.calls, sessions.get("odysseus", 42)["next_action"]), ([], ""))

    def test_new_status_never_gets_catch_up_text(self):
        self.dispatch(make_issue(status="new"), [result("needs_input")])
        self.assertNotIn("not resumed", self.calls[0]["prompt"])

    def test_run_is_recorded_and_closed(self):
        self.dispatch(make_issue(), [result("needs_input")])
        run = run_log.recent(1)[0]
        self.assertEqual((run["issue"], run["iteration"], run["model"], run["outcome"]),
                         (42, 1, "sonnet", "needs_input"))
        self.assertIsNotNone(run["ended_at"])
        self.assertEqual(sessions.get("odysseus", 42)["running"], 0)

    def test_run_works_in_the_issue_workspace(self):
        self.dispatch(make_issue(), [result("needs_input")])
        self.assertEqual(self.cwds[0].name, "issue-42")
        self.assertEqual(self.envs[0]["P4M_WORKSPACE"], str(self.cwds[0]))

    def test_workspace_failure_becomes_failed_run(self):
        tracker = StubTracker(make_issue())

        def refuse(*a):
            raise dispatcher_mod.runner.workspaces.WorkspaceError("branch is checked out elsewhere")

        with mock.patch.object(dispatcher_mod.runner.workspaces, "ensure", refuse):
            outcome = dispatcher_mod.Dispatcher(make_agent(), tracker, "claude").handle(42)
        self.assertEqual(outcome, "failed")
        self.assertEqual(tracker.handoffs, [("pending", "1")])
        self.assertEqual(self.reason(), "talos_error")

    def test_branch_checked_out_elsewhere_hands_back_without_a_run(self):
        tracker = StubTracker(make_issue())

        def refuse(*a):
            raise dispatcher_mod.runner.workspaces.BranchCheckedOut("claude-issue-42", "D:/ws/gaia")

        with mock.patch.object(dispatcher_mod.runner.workspaces, "ensure", refuse):
            outcome = dispatcher_mod.Dispatcher(make_agent(), tracker, "claude").handle(42)
        self.assertEqual(outcome, "needs_input")
        self.assertEqual(tracker.handoffs, [("feedback", "1")])
        self.assertIn("checked out elsewhere", tracker.comments[0])
        state = sessions.get("odysseus", 42)
        self.assertEqual((state["runs"], state["handoff_reason"]), (0, "branch_checked_out"))   # no run, no iteration
        self.assertEqual(json.loads(state["handoff_detail"])["branch"], "claude-issue-42")
        self.assertEqual(json.loads(state["handoff_detail"])["repo"], "gaia")

    def test_gaia_pack_is_built_in_the_issue_workspace(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen.update(cwd=kw["cwd"], env=kw["env"])
            return mock.Mock(returncode=0, stdout="PACK")

        with mock.patch.object(dispatcher_mod.runner.subprocess, "run", fake_run):
            self.assertEqual(dispatcher_mod.runner.gaia_pack(42, Path("ws/issue-42")), "PACK")
        self.assertEqual(seen["cwd"], Path("ws/issue-42"))
        self.assertEqual(seen["env"]["P4M_WORKSPACE"], str(Path("ws/issue-42")))

    def test_runner_exception_becomes_failed_run(self):
        tracker = StubTracker(make_issue())

        def boom(*a, **k):
            raise OSError("claude.exe missing")

        with mock.patch.object(dispatcher_mod.runner, "run", boom):
            outcome = dispatcher_mod.Dispatcher(make_agent(), tracker, "claude").handle(42)
        self.assertEqual(outcome, "failed")
        self.assertEqual(run_log.recent(1)[0]["outcome"], "failed")
        self.assertEqual(tracker.handoffs, [("pending", "1")])

    def test_pickup_moves_new_issue_to_in_progress_before_claude(self):
        _, tracker = self.dispatch(make_issue(status="new"), [result("needs_input")], pickup_status="in_progress")
        self.assertEqual(tracker.statuses, ["in_progress"])

    def test_pickup_leaves_in_progress_alone(self):
        _, tracker = self.dispatch(make_issue(status="in_progress"), [result("needs_input")],
                                   pickup_status="in_progress",
                                   actions={"in_progress": make_agent().actions["new"]})
        self.assertEqual(tracker.statuses, [])

    def test_no_pickup_status_configured(self):
        _, tracker = self.dispatch(make_issue(status="new"), [result("needs_input")])
        self.assertEqual(tracker.statuses, [])

    def test_limit_does_not_mark_in_progress(self):
        sessions.update("odysseus", 42, runs=10)
        _, tracker = self.dispatch(make_issue(status="new"), [], pickup_status="in_progress")
        self.assertEqual(tracker.statuses, [])

    def test_complex_issue_runs_on_opus(self):
        self.dispatch(make_issue(complexity="complex"), [result("needs_input")])
        self.assertEqual(self.models, ["opus"])

    def reason(self):
        return sessions.get("odysseus", 42)["handoff_reason"]

    def test_agent_outcomes_record_their_reason(self):
        self.dispatch(make_issue(), [result("needs_input")])
        self.assertEqual(self.reason(), "agent_needs_input")
        self.assertTrue(sessions.get("odysseus", 42)["handoff_at"])
        self.dispatch(make_issue(), [result("blocked")])
        self.assertEqual(self.reason(), "agent_blocked")

    def test_escalation_at_the_top_model_is_agent_needs_input(self):
        self.dispatch(make_issue(complexity="complex"), [result("escalate")])
        self.assertEqual(self.reason(), "agent_needs_input")

    def test_budget_stops_record_their_reason(self):
        self.dispatch(make_issue(), [{"is_error": True, "subtype": "error_max_turns", "num_turns": 5}])
        self.assertEqual(self.reason(), "turn_budget")
        self.dispatch(make_issue(), [{"is_error": True, "subtype": "error_max_budget_usd"}])
        self.assertEqual(self.reason(), "cost_budget")

    def test_claude_error_and_missing_outcome_are_run_error(self):
        self.dispatch(make_issue(), [{"is_error": True, "subtype": "exit 1", "result": "boom"}])
        self.assertEqual(self.reason(), "run_error")
        self.dispatch(make_issue(), [{"subtype": "success", "result": "hi"}])
        self.assertEqual(self.reason(), "run_error")

    def test_exception_around_the_runner_is_talos_error(self):
        tracker = StubTracker(make_issue())

        def boom(*a, **k):
            raise RuntimeError("no claude")

        with mock.patch.object(dispatcher_mod.runner, "run", boom):
            dispatcher_mod.Dispatcher(make_agent(), tracker, "claude").handle(42)
        self.assertEqual(self.reason(), "talos_error")

    def test_iteration_limit_records_its_reason(self):
        sessions.update("odysseus", 42, runs=10)
        self.dispatch(make_issue(), [])
        self.assertEqual(self.reason(), "iteration_limit")

    def test_done_still_assigned_is_not_handed_back(self):
        self.dispatch(make_issue(), [result("done")])
        self.assertEqual(self.reason(), "not_handed_back")

    def test_interrupted_run_records_its_reason(self):
        sessions.update("odysseus", 42, running=True)
        tracker = StubTracker(make_issue())
        dispatcher_mod.Dispatcher(make_agent(), tracker, "claude").recover_interrupted()
        self.assertEqual(self.reason(), "interrupted")

    def test_needs_input_hands_off_as_feedback(self):
        outcome, tracker = self.dispatch(make_issue(), [result("needs_input")])
        self.assertEqual(outcome, "needs_input")
        self.assertEqual(tracker.handoffs, [("feedback", "1")])

    def test_escalation_reruns_on_opus_and_counts_both_runs(self):
        self.dispatch(make_issue(complexity="simple"), [result("escalate"), result("needs_input")])
        self.assertEqual(self.models, ["sonnet", "opus"])
        self.assertEqual(sessions.get("odysseus", 42)["runs"], 2)
        self.assertTrue(sessions.get("odysseus", 42)["escalated"])

    def test_limit_stops_before_running_claude(self):
        sessions.update("odysseus", 42, runs=10)
        outcome, tracker = self.dispatch(make_issue(), [])
        self.assertEqual(outcome, "limit")
        self.assertEqual(self.models, [])
        self.assertEqual(tracker.handoffs, [("pending", "1")])
        self.assertIn("needs manual intervention", tracker.comments[0])

    def test_tenth_run_is_allowed_eleventh_is_not(self):
        sessions.update("odysseus", 42, runs=9)
        self.dispatch(make_issue(), [result("needs_input")])
        self.assertEqual(len(self.models), 1)
        outcome, _ = self.dispatch(make_issue(), [])
        self.assertEqual(outcome, "limit")

    def test_crash_hands_off_as_failed(self):
        outcome, tracker = self.dispatch(make_issue(), [{"is_error": True, "subtype": "exit 1", "result": "boom"}])
        self.assertEqual(outcome, "failed")
        self.assertEqual(tracker.handoffs, [("pending", "1")])

    def ship_agent(self):
        return dict(ship_prompt="Ship #{issue} in {repo}. Note: {handoff}")

    def test_ready_to_ship_starts_a_fresh_ship_session(self):
        outcome, _ = self.dispatch(make_issue(), [result("ready_to_ship", handoff="files a, b"),
                                                  result("needs_input", session="s2")], **self.ship_agent())
        self.assertEqual(outcome, "needs_input")
        self.assertEqual(self.calls[1], {"prompt": "Ship #42 in gaia. Note: files a, b", "resume": None,
                                         "limits": (0, 0.0)})
        self.assertEqual(self.models, ["sonnet", "sonnet"])
        state = sessions.get("odysseus", 42)
        self.assertEqual((state["runs"], state["session_id"]), (2, "s2"))

    def test_ship_session_may_not_ask_to_ship_again(self):
        outcome, tracker = self.dispatch(make_issue(), [result("ready_to_ship"), result("ready_to_ship")],
                                         **self.ship_agent())
        self.assertEqual(outcome, "failed")
        self.assertEqual(len(self.models), 2)
        self.assertEqual(tracker.handoffs, [("pending", "1")])

    def test_ready_to_ship_without_ship_prompt_is_failed(self):
        outcome, _ = self.dispatch(make_issue(), [result("ready_to_ship")])
        self.assertEqual(outcome, "failed")
        self.assertEqual(len(self.models), 1)

    def test_limits_follow_the_model(self):
        self.dispatch(make_issue(complexity="complex"), [result("needs_input")], max_turns=80,
                      limits_by_model={"opus": {"maxTurns": 150, "maxBudgetUsd": 15}})
        self.assertEqual(self.calls[0]["limits"], (150, 15.0))

    def test_agent_env_reaches_claude(self):
        self.dispatch(make_issue(), [result("needs_input")], env={"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"})
        self.assertEqual(self.envs[0]["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"], "1")

    def test_done_still_assigned_is_flagged(self):
        outcome, tracker = self.dispatch(make_issue(), [result("done")])
        self.assertEqual(tracker.handoffs, [("pending", "1")])


    def held_dispatcher(self, tracker, **agent):
        queued = []
        with mock.patch.object(dispatcher_mod.runner, "gaia_pack", lambda n, folder=None: ""):
            d = dispatcher_mod.Dispatcher(make_agent(**agent), tracker, "claude")
        d.submit = lambda number, source, reconcile=False: queued.append(number)
        return d, queued

    def test_issue_waits_on_an_unfinished_dependency_then_is_released(self):
        tracker = StubTracker(make_issue(dependsOn=[7]))
        tracker.dependencies = {7: "in_review"}
        d, queued = self.held_dispatcher(tracker)
        self.assertEqual(d.handle(42), "held")
        self.assertEqual(d.handle(42), "held")
        self.assertEqual(len(tracker.comments), 1)          # "waiting on #7" is not repeated
        self.assertIn("#7", tracker.comments[0])
        self.assertEqual(sessions.get("odysseus", 42), {})   # no run, no iteration
        d.release_held("status changed", 7)
        self.assertEqual(queued, [])                         # still in review: stays held
        tracker.dependencies = {7: "done"}
        d.release_held("status changed", 7)
        self.assertEqual(queued, [42])

    def test_status_event_is_handed_to_the_release_worker(self):
        d, queued = self.held_dispatcher(StubTracker(make_issue(dependsOn=[7])))
        d.on_event({"eventName": "issue.status.changed", "meta": {"issueNumber": 7}})
        self.assertTrue(d._releases.empty())                 # nothing is held: no re-check is queued
        d.handle(42)                                         # held; queues a re-check of its dependency
        d._releases.get_nowait()
        d.on_event({"eventName": "issue.status.changed", "meta": {"issueNumber": 7}})
        self.assertEqual(d._releases.get_nowait(), 7)        # nothing read from Gaia on the listener thread

    def test_unreadable_dependency_is_held_without_a_comment(self):
        tracker = StubTracker(make_issue(dependsOn=[7]))
        tracker.unreadable = True
        d, queued = self.held_dispatcher(tracker)
        self.assertEqual(d.handle(42), "held")
        self.assertEqual(tracker.comments, [])
        self.assertTrue(d._deps.is_held(42))
        tracker.unreadable = False
        tracker.dependencies = {7: "done"}
        d.release_held("status changed", 99)                 # any status change retries an unreadable one
        self.assertEqual(queued, [42])

    def test_release_rechecks_only_issues_that_depend_on_the_changed_one(self):
        tracker = StubTracker(make_issue(dependsOn=[7]))
        tracker.dependencies = {7: "in_review"}
        d, queued = self.held_dispatcher(tracker)
        d.handle(42)
        tracker.dependencies = {7: "done"}
        tracker.read = []
        d.release_held("status changed", 99)
        self.assertEqual((queued, tracker.read), ([], []))   # #99 is not one of #42's dependencies
        d.release_held("reconnect")
        self.assertEqual(queued, [42])

    def test_dependency_done_statuses_come_from_config(self):
        tracker = StubTracker(make_issue(dependsOn=[7]))
        tracker.dependencies = {7: "Merged"}
        d, queued = self.held_dispatcher(tracker, dependency_done_statuses=("merged",))
        self.assertEqual(d._deps.unmet([7], {7: "Merged"}), [])
        self.assertEqual(d._deps.unmet([7], {7: "done"}), [7])   # the default list is replaced

    def test_held_issue_that_is_no_longer_actionable_is_logged_and_dropped(self):
        tracker = StubTracker(make_issue(dependsOn=[7]))
        tracker.dependencies = {7: "in_review"}
        d, queued = self.held_dispatcher(tracker)
        d.handle(42)
        tracker._issue["assignee"] = "someone-else"
        tracker.dependencies = {7: "done"}
        with mock.patch.object(dispatcher_mod, "log") as log:
            self.assertIsNone(d.handle(42))
        self.assertFalse(d._deps.is_held(42))
        self.assertIn("left the held set", log.call_args[0][0])

    def test_dependency_done_at_start_runs_normally(self):
        tracker_issue = make_issue(dependsOn=[7])
        outcome, tracker = self.dispatch_with_dependencies(tracker_issue, {7: "done"})
        self.assertEqual(outcome, "needs_input")

    def dispatch_with_dependencies(self, issue, dependencies):
        original = StubTracker.__init__

        def init(tracker, i):
            original(tracker, i)
            tracker.dependencies = dependencies

        with mock.patch.object(StubTracker, "__init__", init):
            return self.dispatch(issue, [result("needs_input")])


if __name__ == "__main__":
    unittest.main()
