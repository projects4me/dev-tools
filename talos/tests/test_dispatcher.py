"""Dispatcher with a stub tracker and runner: iteration limit, model choice, escalation, hand-offs."""

import sys
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import db, dispatcher as dispatcher_mod, policy, runs as run_log, sessions  # noqa: E402
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

        def fake_build(claude, agent, model, sid=None, new_session_id=None, limits=None, mcp=None, memory=None):
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

    def test_run_leases_a_slot_for_its_environment_and_releases_it(self):
        outcome, _ = self.dispatch(make_issue(), [result("needs_input")], test_slots=2)
        self.assertEqual(self.envs[0]["P4M_TEST_SLOT"], "1")
        self.assertEqual(outcome, "needs_input")

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

    def test_branch_checked_out_elsewhere_hands_back_without_a_run(self):
        tracker = StubTracker(make_issue())

        def refuse(*a):
            raise dispatcher_mod.runner.workspaces.BranchCheckedOut("claude-issue-42", "D:/ws/gaia")

        with mock.patch.object(dispatcher_mod.runner.workspaces, "ensure", refuse):
            outcome = dispatcher_mod.Dispatcher(make_agent(), tracker, "claude").handle(42)
        self.assertEqual(outcome, "needs_input")
        self.assertEqual(tracker.handoffs, [("feedback", "1")])
        self.assertIn("checked out elsewhere", tracker.comments[0])
        self.assertEqual(sessions.get("odysseus", 42), {})   # no run, no iteration

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


    def test_issue_waits_on_an_unfinished_dependency_then_is_released(self):
        tracker = StubTracker(make_issue(dependsOn=[7]))
        tracker.dependencies = {7: "in_review"}
        queued = []
        with mock.patch.object(dispatcher_mod.runner, "gaia_pack", lambda n, folder=None: ""):
            d = dispatcher_mod.Dispatcher(make_agent(), tracker, "claude")
        d.submit = lambda number, source, reconcile=False: queued.append(number)
        self.assertEqual(d.handle(42), "held")
        self.assertEqual(d.handle(42), "held")
        self.assertEqual(len(tracker.comments), 1)          # "waiting on #7" is not repeated
        self.assertIn("#7", tracker.comments[0])
        self.assertEqual(sessions.get("odysseus", 42), {})   # no run, no iteration
        d.on_event({"eventName": "issue.status.changed"})
        self.assertEqual(queued, [])                         # still in review: stays held
        tracker.dependencies = {7: "done"}
        d.on_event({"eventName": "issue.status.changed"})
        self.assertEqual(queued, [42])
        d.release_held("reconnect")
        self.assertEqual(queued, [42])                       # released once

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
