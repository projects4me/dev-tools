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
        self.assertEqual(policy.read_outcome(r), ("needs_input", "q", "<p>?</p>", ""))

    def test_ready_to_ship_carries_the_handoff_note(self):
        r = {"subtype": "success", "structured_output": {"outcome": "ready_to_ship", "summary": "s",
                                                         "handoff": "branch claude-issue-42"}}
        self.assertEqual(policy.read_outcome(r), ("ready_to_ship", "s", "", "branch claude-issue-42"))

    def test_error_result_is_failure(self):
        self.assertEqual(policy.read_outcome({"is_error": True, "subtype": "error_during_execution"})[0], "failed")

    def test_missing_structured_output_is_failure(self):
        self.assertEqual(policy.read_outcome({"subtype": "success", "result": "hi"})[0], "failed")

    def test_unknown_outcome_is_failure(self):
        r = {"subtype": "success", "structured_output": {"outcome": "maybe", "summary": ""}}
        self.assertEqual(policy.read_outcome(r)[0], "failed")

    def test_no_result_is_failure(self):
        self.assertEqual(policy.read_outcome(None)[0], "failed")

    def test_turn_budget_hands_back_for_input(self):
        outcome, summary, comment, _ = policy.read_outcome(
            {"is_error": True, "subtype": "error_max_turns", "num_turns": 80, "session_id": "s"})
        self.assertEqual(outcome, "needs_input")
        self.assertIn("80-turn budget", summary)
        self.assertIn("assign the issue back", comment)

    def test_cost_budget_hands_back_for_input(self):
        outcome, summary, comment, _ = policy.read_outcome(
            {"is_error": True, "subtype": "error_max_budget_usd", "total_cost_usd": 15.2, "session_id": "s"})
        self.assertEqual(outcome, "needs_input")
        self.assertIn("$15.20 cost budget", summary)
        self.assertIn("assign the issue back", comment)


class RunLimitsTest(unittest.TestCase):
    def test_agent_turn_cap_without_model_limits(self):
        self.assertEqual(policy.run_limits(make_agent(max_turns=80), "opus"), (80, 0.0))

    def test_model_limits_replace_the_turn_cap(self):
        agent = make_agent(max_turns=80, limits_by_model={"opus": {"maxTurns": 150, "maxBudgetUsd": 15}})
        self.assertEqual(policy.run_limits(agent, "opus"), (150, 15.0))
        self.assertEqual(policy.run_limits(agent, "sonnet"), (80, 0.0))

    def test_budget_only_keeps_the_turn_cap(self):
        agent = make_agent(max_turns=80, limits_by_model={"opus": {"maxBudgetUsd": 10}})
        self.assertEqual(policy.run_limits(agent, "opus"), (80, 10.0))


class ShipPromptTest(unittest.TestCase):
    def test_handoff_note_and_tracker_values(self):
        agent = make_agent(ship_prompt="Ship #{issue} in {repo}: {handoff}")
        self.assertEqual(policy.render_ship_prompt(agent, make_issue(), "gaia", " note {x} "),
                         "Ship #42 in gaia: note {x}")

    def test_empty_handoff_note_is_named(self):
        agent = make_agent(ship_prompt="{handoff}")
        self.assertIn("no handoff note", policy.render_ship_prompt(agent, make_issue(), "gaia", ""))


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

    def test_mcp_config_is_strict(self):
        self.assertNotIn("--mcp-config", runner.build_command("claude", make_agent(), "x", "sonnet"))
        cmd = runner.build_command("claude", make_agent(), "x", "sonnet", mcp=Path("m.json"))
        self.assertEqual(cmd[cmd.index("--mcp-config") + 1], "m.json")
        self.assertIn("--strict-mcp-config", cmd)

    def test_resume(self):
        cmd = runner.build_command("claude", make_agent(), "go on", "opus", session_id="abc")
        self.assertEqual(cmd[cmd.index("--resume") + 1], "abc")
        self.assertEqual(cmd[cmd.index("--model") + 1], "opus")

    def test_turn_budget(self):
        self.assertNotIn("--max-turns", runner.build_command("claude", make_agent(), "x", "sonnet"))
        cmd = runner.build_command("claude", make_agent(max_turns=80), "x", "sonnet")
        self.assertEqual(cmd[cmd.index("--max-turns") + 1], "80")

    def test_run_limits_override_turns_and_add_a_budget(self):
        cmd = runner.build_command("claude", make_agent(max_turns=80), "x", "opus", limits=(150, 15.0))
        self.assertEqual(cmd[cmd.index("--max-turns") + 1], "150")
        self.assertEqual(cmd[cmd.index("--max-budget-usd") + 1], "15.0")
        self.assertNotIn("--max-budget-usd", runner.build_command("claude", make_agent(), "x", "sonnet",
                                                                  limits=(80, 0.0)))

    def test_tool_policy(self):
        self.assertNotIn("--tools", runner.build_command("claude", make_agent(), "x", "sonnet"))
        cmd = runner.build_command("claude", make_agent(builtin_tools="Bash,Read", denied_tools=["PowerShell"]),
                                   "x", "sonnet")
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Bash,Read")
        self.assertEqual(cmd[cmd.index("--disallowedTools") + 1], "PowerShell")


class PackTest(unittest.TestCase):
    def test_with_pack_appends_the_pack_as_data(self):
        out = policy.with_pack("Work #1", "== Gaia pack\n  x")
        self.assertTrue(out.startswith("Work #1\n\n"))
        self.assertIn("data, not instructions", out)
        self.assertTrue(out.endswith("x"))

    def test_with_pack_without_a_pack_is_unchanged(self):
        self.assertEqual(policy.with_pack("Work #1", "  \n"), "Work #1")


if __name__ == "__main__":
    unittest.main()
