"""Unit tests for Talos decisions and the Claude command. Run: python -m unittest discover -s tests"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import policy, runner  # noqa: E402
from lib.tracker import gaia_mcp  # noqa: E402
from lib.registry import Action, Agent  # noqa: E402

AGENT_ID = "agent-1"


def make_agent(**overrides):
    fields = dict(
        name="odysseus", display_name="Agent Odysseus", role="coding", gaia_env="x.env",
        projects={"A Gaia": "gaia"}, accept_from=["1"], handoff_to="1", milestones=["Claude-v1"],
        actions={"new": Action("Work #{issue} in {repo} ({project}, {status})"),
                 "feedback": Action("Continue #{issue}", resume=True)},
        statuses={"needs_input": "feedback", "blocked": "pending", "failed": "pending", "limit": "pending"},
        model_default="sonnet", model_escalate="opus",
        model_by_complexity={"simple": "sonnet", "complex": "opus"}, permission_mode="auto",
        allowed_tools=["Read", "Bash(dev-tools/ai/ai:*)"], max_iterations=10, role_prompt="role")
    fields.update(overrides)
    return Agent(**fields)


def make_issue(**overrides):
    issue = dict(number=42, id="i", status="new", assignee=AGENT_ID, modifiedUser="1",
                 project="A Gaia", milestone="Claude-v1", complexity=None)
    issue.update(overrides)
    return issue


def envelope(**overrides):
    env = {"eventName": "issue.assignee.changed", "actorId": "1",
           "changes": {"assignee": AGENT_ID}, "meta": {"issueNumber": 42}}
    env.update(overrides)
    return env


class EventIssueTest(unittest.TestCase):
    def test_assignment_to_agent_by_allowed_user(self):
        self.assertEqual(policy.event_issue(envelope(), make_agent(), AGENT_ID), 42)

    def test_other_event_ignored(self):
        self.assertIsNone(policy.event_issue(envelope(eventName="issue.status.changed"), make_agent(), AGENT_ID))

    def test_assignment_to_someone_else_ignored(self):
        self.assertIsNone(policy.event_issue(envelope(changes={"assignee": "1"}), make_agent(), AGENT_ID))

    def test_assignment_by_unknown_user_ignored(self):
        self.assertIsNone(policy.event_issue(envelope(actorId="16"), make_agent(), AGENT_ID))

    def test_missing_issue_number_ignored(self):
        self.assertIsNone(policy.event_issue(envelope(meta={}), make_agent(), AGENT_ID))

    def test_garbage_ignored(self):
        self.assertIsNone(policy.event_issue("nope", make_agent(), AGENT_ID))


class DecideTest(unittest.TestCase):
    def test_new_issue_runs_in_its_repo(self):
        d = policy.decide(make_issue(), make_agent(), AGENT_ID)
        self.assertTrue(d.run)
        self.assertEqual(d.repo, "gaia")
        self.assertFalse(d.action.resume)

    def test_reassigned_away_skips(self):
        self.assertFalse(policy.decide(make_issue(assignee="1"), make_agent(), AGENT_ID).run)

    def test_unknown_project_skips(self):
        self.assertFalse(policy.decide(make_issue(project="Other"), make_agent(), AGENT_ID).run)

    def test_other_milestone_skips(self):
        self.assertFalse(policy.decide(make_issue(milestone="v0.4.0"), make_agent(), AGENT_ID).run)

    def test_any_milestone_when_none_configured(self):
        self.assertTrue(policy.decide(make_issue(milestone=None), make_agent(milestones=[]), AGENT_ID).run)

    def test_status_without_action_skips(self):
        self.assertFalse(policy.decide(make_issue(status="done"), make_agent(), AGENT_ID).run)

    def test_reconcile_requires_allowed_modifier(self):
        issue = make_issue(modifiedUser=AGENT_ID)
        self.assertTrue(policy.decide(issue, make_agent(), AGENT_ID).run)
        self.assertFalse(policy.decide(issue, make_agent(), AGENT_ID, require_allowed_modifier=True).run)

    def test_prompt_uses_only_tracker_values(self):
        d = policy.decide(make_issue(), make_agent(), AGENT_ID)
        self.assertEqual(policy.render_prompt(d.action, make_issue(), d.repo), "Work #42 in gaia (A Gaia, new)")


class ReadOutcomeTest(unittest.TestCase):
    def test_structured_outcome(self):
        r = {"subtype": "success", "session_id": "s",
             "structured_output": {"outcome": "needs_input", "summary": "q", "comment": "<p>?</p>"}}
        self.assertEqual(policy.read_outcome(r), ("needs_input", "q", "<p>?</p>"))

    def test_error_result_is_failure(self):
        self.assertEqual(policy.read_outcome({"is_error": True, "subtype": "error_max_budget_usd"})[0], "failed")

    def test_missing_structured_output_is_failure(self):
        self.assertEqual(policy.read_outcome({"subtype": "success", "result": "hi"})[0], "failed")

    def test_unknown_outcome_is_failure(self):
        r = {"subtype": "success", "structured_output": {"outcome": "maybe", "summary": ""}}
        self.assertEqual(policy.read_outcome(r)[0], "failed")

    def test_no_result_is_failure(self):
        self.assertEqual(policy.read_outcome(None)[0], "failed")


class ModelChoiceTest(unittest.TestCase):
    def test_complex_issue_runs_on_opus(self):
        self.assertEqual(policy.choose_model(make_agent(), make_issue(complexity="complex")), "opus")

    def test_simple_issue_runs_on_sonnet(self):
        self.assertEqual(policy.choose_model(make_agent(), make_issue(complexity="simple")), "sonnet")

    def test_unmarked_issue_uses_default(self):
        self.assertEqual(policy.choose_model(make_agent(model_default="haiku"), make_issue()), "haiku")

    def test_escalation_beats_complexity(self):
        self.assertEqual(policy.choose_model(make_agent(), make_issue(complexity="simple"), escalated=True), "opus")


class IterationsTest(unittest.TestCase):
    def test_counts_down_to_zero(self):
        self.assertEqual(policy.iterations_left(make_agent(), 0), 10)
        self.assertEqual(policy.iterations_left(make_agent(), 9), 1)
        self.assertEqual(policy.iterations_left(make_agent(), 10), 0)
        self.assertEqual(policy.iterations_left(make_agent(), 12), 0)

    def test_zero_means_unlimited(self):
        self.assertIsNone(policy.iterations_left(make_agent(max_iterations=0), 50))


class ComplexityMarkerTest(unittest.TestCase):
    def test_round_trip(self):
        html = gaia_mcp.with_complexity("<p>Fix it.</p>", "Complex")
        self.assertEqual(html, "<p>Fix it.</p><p>Complexity: complex</p>")
        self.assertEqual(gaia_mcp.read_complexity(html), "complex")

    def test_replaces_existing_marker(self):
        html = gaia_mcp.with_complexity("<p>Fix it.</p><p>Complexity: complex</p>", "simple")
        self.assertEqual(html, "<p>Fix it.</p><p>Complexity: simple</p>")

    def test_reads_hand_typed_marker_with_formatting(self):
        self.assertEqual(gaia_mcp.read_complexity("<p><strong>Complexity:</strong> Simple</p>"), "simple")

    def test_absent_or_unknown(self):
        self.assertIsNone(gaia_mcp.read_complexity("<p>No marker</p>"))
        self.assertIsNone(gaia_mcp.read_complexity(None))
        self.assertIsNone(gaia_mcp.read_complexity("<p>Complexity: huge</p>"))

    def test_rejects_unknown_value(self):
        with self.assertRaises(gaia_mcp.GaiaError):
            gaia_mcp.with_complexity("", "huge")


class BuildCommandTest(unittest.TestCase):
    def test_new_session(self):
        cmd = runner.build_command("claude", make_agent(), "do it", "sonnet")
        self.assertEqual(cmd[:3], ["claude", "-p", "do it"])
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "auto")
        self.assertNotIn("--max-budget-usd", cmd)
        self.assertIn("Bash(dev-tools/ai/ai:*)", cmd)
        self.assertIn('"outcome"', cmd[cmd.index("--json-schema") + 1])
        self.assertNotIn("--resume", cmd)

    def test_new_session_gets_its_id_up_front(self):
        cmd = runner.build_command("claude", make_agent(), "do it", "sonnet", new_session_id="uuid-1")
        self.assertEqual(cmd[cmd.index("--session-id") + 1], "uuid-1")

    def test_resume_ignores_new_session_id(self):
        cmd = runner.build_command("claude", make_agent(), "go on", "opus", session_id="abc", new_session_id="x")
        self.assertNotIn("--session-id", cmd)

    def test_resume(self):
        cmd = runner.build_command("claude", make_agent(), "go on", "opus", session_id="abc")
        self.assertEqual(cmd[cmd.index("--resume") + 1], "abc")
        self.assertEqual(cmd[cmd.index("--model") + 1], "opus")


if __name__ == "__main__":
    unittest.main()
