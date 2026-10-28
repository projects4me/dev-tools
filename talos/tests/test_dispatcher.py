"""Dispatcher with a stub tracker and runner: iteration limit, model choice, escalation, hand-offs."""

import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import db, dispatcher as dispatcher_mod, runs as run_log, sessions  # noqa: E402
from test_policy import AGENT_ID, make_agent, make_issue  # noqa: E402


class StubTracker:
    def __init__(self, issue):
        self._issue = issue
        self.comments, self.handoffs, self.statuses = [], [], []

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


def result(outcome, session="s1", handoff=""):
    return {"subtype": "success", "session_id": session,
            "structured_output": {"outcome": outcome, "summary": outcome, "comment": "<p>why</p>",
                                  "handoff": handoff}}


class DispatcherTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.patches = [mock.patch.object(db, "DB_FILE", tmp / "talos.db"),
                        mock.patch.object(dispatcher_mod, "log", lambda *a, **k: None),  # keep talos.log real
                        mock.patch.object(dispatcher_mod.workspaces, "ensure",
                                          lambda root, tools, number, repo: tmp / f"issue-{number}")]
        for p in self.patches:
            p.start()
        self.models = []
        self.calls = []
        self.envs = []
        self.cwds = []

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

        def fake_build(claude, agent, model, sid=None, new_session_id=None, limits=None, mcp=None):
            self.calls.append({"resume": sid, "limits": limits})
            return ["claude", "--model", model]

        with mock.patch.object(dispatcher_mod.runner, "run", fake_run), \
                mock.patch.object(dispatcher_mod.runner, "build_command", fake_build):
            d = dispatcher_mod.Dispatcher(make_agent(**agent_overrides), tracker, "claude")
            if holder is not None:
                holder["d"] = d
            return d.handle(42), tracker

    def feedback_agent(self):
        return dict(actions={"feedback": make_agent().actions["feedback"]})

    def test_usage_limit_pauses_and_requeues_without_counting_an_iteration(self):
        reset = dispatcher_mod._now() + timedelta(hours=3)
        limit = {"is_error": True, "subtype": "success", "session_id": "s1",
                 "result": f"Claude AI usage limit reached|{int(reset.timestamp())}"}
        d_holder = {}
        outcome, tracker = self.dispatch(make_issue(), [limit], holder=d_holder)
        self.assertEqual(outcome, "limited")
        self.assertEqual(tracker.handoffs, [])
        self.assertEqual(tracker._issue["assignee"], AGENT_ID)
        self.assertEqual(sessions.get("odysseus", 42)["runs"], 0)
        self.assertEqual(run_log.recent(1)[0]["outcome"], "limited")
        self.assertGreater(d_holder["d"]._paused_until, reset)
        self.assertEqual(len(tracker.comments), 1)   # 3h pause is longer than the 1h default

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
            raise dispatcher_mod.workspaces.WorkspaceError("branch is checked out elsewhere")

        with mock.patch.object(dispatcher_mod.workspaces, "ensure", refuse):
            outcome = dispatcher_mod.Dispatcher(make_agent(), tracker, "claude").handle(42)
        self.assertEqual(outcome, "failed")
        self.assertEqual(tracker.handoffs, [("pending", "1")])

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


if __name__ == "__main__":
    unittest.main()
