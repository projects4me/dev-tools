"""Unit tests for Agamemnon's triage sessions: the decision (premise, repeats, daily cost), the session's end (resolved,
to the owner), recovery after a restart, and where the orchestrator offers incidents (handbacks, merges, conflicts).
Run: python -m unittest discover -s tests"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import db, registry, sessions  # noqa: E402
from lib.agamemnon import journal, premise, triage  # noqa: E402
from lib.orchestrator import policy  # noqa: E402
from lib.orchestrator.keeper import BranchKeeper  # noqa: E402
from lib.orchestrator.merging import MergeSweeper  # noqa: E402
from lib.orchestrator.worker import Orchestrator  # noqa: E402

OWNER, WORKER_ID, MY_ID = "7", "50", "60"
NOW = "2026-10-31T10:00:00+05:00"
RETRY = registry.AUTO_RETRY_REASONS


def an_issue(number=5, **fields):
    base = {"number": number, "project": "A Gaia", "milestone": "aga-v1", "status": "feedback", "assignee": OWNER,
            "modifiedUser": WORKER_ID, "priority": "medium", "type": "story", "dependsOn": []}
    return {**base, **fields}


class FakeTracker:
    def __init__(self, issues):
        self.issues = {i["number"]: dict(i) for i in issues}
        self.comments, self.handed = [], []

    def project_ids(self):
        return {"A Gaia": "1"}

    def assigned_issue_numbers(self, project_id, user_id):
        return [n for n, i in self.issues.items() if i["assignee"] == user_id]

    def issues_in(self, project, prefix):
        return [dict(i) for i in self.issues.values()]

    def issue(self, number):
        return dict(self.issues[number])

    def statuses_of(self, numbers):
        return {n: self.issues[n]["status"] for n in numbers}

    def assign(self, number, assignee):
        self.issues[number]["assignee"] = assignee

    def hand_off(self, number, status, assignee):
        self.issues[number].update(status=status, assignee=assignee)
        self.handed.append((number, status, assignee))

    def comment(self, number, title, body):
        self.comments.append((number, title, body))


class TriageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        patcher = mock.patch.object(db, "DB_FILE", self.tmp / "talos.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        for where in ("lib.agamemnon.triage.log", "lib.orchestrator.worker.log", "lib.orchestrator.merging.log",
                      "lib.orchestrator.keeper.log"):
            patcher = mock.patch(where)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tracker = FakeTracker([an_issue(), an_issue(6, status="in_review", assignee=MY_ID)])
        self.launched = []
        self.answer = {"session_id": "s", "total_cost_usd": 0.5,
                       "structured_output": {"outcome": "resolved", "summary": "re-ran CI"}}
        self.log_file = None

    def settings(self, **changes):
        return registry.Orchestration(worker="odysseus", **{"aga_triage": True, **changes})

    def triage(self, **changes):
        agent = SimpleNamespace(name="agamemnon", display_name="Agent Agamemnon", orchestration=self.settings(**changes))
        return triage.Triage(agent, self.tracker, MY_ID, [OWNER], premise.load(), self.launch,
                             run_log=lambda n: self.log_file, in_flight=lambda: [8], now=lambda: NOW,
                             background=False)

    def launch(self, text, model, incident, issue):
        self.launched.append((text, model, incident, issue))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer, None

    # -- deciding --------------------------------------------------------------------------

    def test_an_incident_in_the_premise_runs_a_session_and_is_resolved(self):
        decision = self.triage().offer(5, triage.MERGE, None, "CI failed again after one re-run: build", pr=77)
        self.assertTrue(decision.taken)
        text, model, incident, issue = self.launched[0]
        self.assertEqual((model, issue), ("sonnet", 5))
        self.assertIn("type `ci_failed`", text)
        self.assertIn('"pr": 77', text)
        self.assertIn('"inFlight"', text)
        row = journal.incident(incident)
        self.assertEqual((row["type"], row["source"], row["model"], row["outcome"], row["cost_usd"]),
                         ("ci_failed", "merge", "sonnet", "resolved", 0.5))
        self.assertTrue(row["closed_at"])
        self.assertEqual([c[1] for c in self.tracker.comments],
                         ["Agent Agamemnon: triaging a ci_failed incident",
                          f"Agent Agamemnon: incident {incident} resolved"])
        self.assertEqual(journal.cost_on(NOW[:10]), 0.5)

    def test_a_merge_conflict_runs_on_opus(self):
        self.triage().offer(6, triage.CONFLICT, None, "merge conflict: PR gaia#77 conflicts with its base")
        self.assertEqual(self.launched[0][1], "opus")

    def test_outside_the_premise_is_declined_and_recorded(self):
        decision = self.triage().offer(5, triage.HANDBACK, "failed", "the run failed", since=NOW)
        self.assertFalse(decision.taken)
        self.assertIn("'other' is outside", decision.note)
        self.assertEqual(self.launched, [])
        [row] = journal.incidents(issue=5)
        self.assertEqual((row["type"], row["outcome"], row["model"]), ("other", "declined", None))
        # the same handback is decided once: nothing more to say on the next sweep
        self.assertIsNone(self.triage().offer(5, triage.HANDBACK, "failed", "the run failed", since=NOW).note)

    def test_the_run_log_feeds_the_classifier(self):
        self.log_file = self.tmp / "run.json"
        self.log_file.write_text(json.dumps({"session_id": "x", "result": "error: Cannot connect to the Docker "
                                                                          "daemon at unix:///var/run/docker.sock",
                                             "structured_output": {"summary": "tests could not start"}}),
                                 encoding="utf-8")
        self.triage().offer(5, triage.HANDBACK, "blocked", "", since=NOW)
        self.assertEqual(journal.incidents(issue=5)[0]["type"], "docker_down")

    def test_the_same_type_on_the_same_issue_again_goes_to_the_owner(self):
        self.triage().offer(6, triage.MERGE, None, "CI failed again after one re-run: build")
        decision = self.triage().offer(6, triage.MERGE, None, "CI failed again after one re-run: build")
        self.assertFalse(decision.taken)
        self.assertIn("again", decision.note)
        self.assertEqual(len(self.launched), 1)

    def test_the_daily_cost_cap(self):
        self.triage().offer(6, triage.MERGE, None, "CI failed again after one re-run: build")
        decision = self.triage(aga_daily_cost_usd=0.5).offer(5, triage.MERGE, None, "CI failed again: build")
        self.assertFalse(decision.taken)
        self.assertIn("daily cap", decision.note)

    def test_an_open_incident_keeps_the_sweeps_away(self):
        incident = journal.open_incident(5, "ci_failed", NOW, triage.MERGE, "sonnet")
        decision = self.triage().offer(5, triage.MERGE, None, "CI failed again: build")
        self.assertEqual((decision.taken, decision.incident), (True, incident))
        self.assertEqual(self.launched, [])

    def test_off_and_dry_run_start_nothing(self):
        self.assertFalse(self.triage(aga_triage=False).offer(5, triage.MERGE, None, "CI failed: build").taken)
        t = self.triage()
        t.dry_run = True
        self.assertIn("dry run", t.offer(5, triage.MERGE, None, "CI failed: build").note)
        self.assertEqual((self.launched, journal.incidents()), ([], []))

    # -- ending ----------------------------------------------------------------------------

    def test_unresolved_goes_to_the_owner_with_the_comment(self):
        self.answer = {"session_id": "s", "total_cost_usd": 1.0,
                       "structured_output": {"outcome": "owner", "summary": "unclear",
                                             "comment": "<p>the job fails on a missing secret</p>"}}
        self.triage().offer(6, triage.MERGE, None, "CI failed again after one re-run: build", pr=77)
        self.assertEqual(self.tracker.handed, [(6, "in_review", OWNER)])
        self.assertIn("missing secret", self.tracker.comments[-1][2])
        self.assertEqual(journal.incidents(issue=6)[0]["outcome"], "owner")

    def test_a_handback_already_with_the_owner_only_gets_the_comment(self):
        self.answer = {"is_error": True, "subtype": "error_max_turns", "session_id": "s"}
        self.triage().offer(5, triage.HANDBACK, "ci_failed", "CI failed", since=NOW)
        self.assertEqual(self.tracker.handed, [])
        self.assertEqual(self.tracker.issues[5]["assignee"], OWNER)
        self.assertIn("without an answer", self.tracker.comments[-1][2])

    def test_a_failed_launch_goes_to_the_owner(self):
        self.answer = OSError("claude not found")
        self.triage().offer(6, triage.MERGE, None, "CI failed again: build")
        self.assertEqual(self.tracker.handed, [(6, "in_review", OWNER)])

    def test_recover_closes_sessions_a_restart_cut_off(self):
        journal.open_incident(6, "ci_failed", NOW, triage.MERGE, "sonnet")
        declined = journal.open_incident(5, "other", NOW, triage.HANDBACK)
        self.triage().recover()
        self.assertEqual(journal.incidents(open_only=True)[0]["id"], declined)
        self.assertEqual(self.tracker.handed, [(6, "in_review", OWNER)])

    def test_the_session_command_allows_aga_only(self):
        cmd = triage.command("claude", "sonnet", 40, 2.0, "role", {"type": "object"})
        self.assertEqual(cmd[cmd.index("--allowedTools") + 1], "Bash(dev-tools/agamemnon/aga:*)")
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Bash")
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual((cmd[cmd.index("--max-turns") + 1], cmd[cmd.index("--max-budget-usd") + 1]), ("40", "2.0"))
        self.assertTrue(triage.OUTCOME_SCHEMA.is_file() and triage.ROLE_PROMPT.is_file())

    # -- where the orchestrator offers incidents -------------------------------------------

    def agent(self, **changes):
        return (SimpleNamespace(name="agamemnon", display_name="Agent Agamemnon",
                                orchestration=self.settings(max_in_flight=1, **changes)),
                SimpleNamespace(name="odysseus", display_name="Agent Odysseus", handoff_to=OWNER, max_parallel=1,
                                projects={"A Gaia": "gaia"}, dependency_done_statuses=("done", "closed"),
                                repo_for=lambda project: "gaia"))

    def test_policy(self):
        def skip(state, **fields):
            return policy.triage_skip_reason(an_issue(**fields), state, "aga-", ["A Gaia"], [OWNER], WORKER_ID,
                                             RETRY, 2)
        self.assertIsNone(skip({"handoff_reason": "failed"}))
        self.assertIsNone(skip({"handoff_reason": "turn_budget", "auto_retries": 2}))   # retries used up
        self.assertEqual(skip({"handoff_reason": "turn_budget"}), "restarted automatically")
        self.assertEqual(skip({"handoff_reason": "agent_needs_input"}), "a question for the reviewer")
        self.assertEqual(skip({"handoff_reason": "failed"}, modifiedUser=OWNER), "changed by someone since the handback")

    def test_the_sweep_offers_handbacks_and_comments_when_not_taken(self):
        sessions.update("odysseus", 5, handoff_reason="failed", handoff_at=NOW)
        sessions.update("odysseus", 7, handoff_reason="agent_needs_input", handoff_at=NOW)
        self.tracker.issues[7] = an_issue(7)
        agent, worker = self.agent()
        t = self.triage()
        o = Orchestrator(agent, worker, self.tracker, WORKER_ID, now=lambda: NOW, triage=t)
        o.sweep()
        self.assertEqual([i["issue"] for i in journal.incidents()], [5])      # the question is not an incident
        self.assertEqual(self.tracker.comments[-1][1], "Agent Agamemnon: left with the owner, no triage")
        o.sweep()
        self.assertEqual(len(self.tracker.comments), 1)                       # decided once per handback

    def test_the_merge_sweeper_lets_triage_look_first(self):
        agent, worker = self.agent(auto_merge_milestones=("aga-v1",))
        merger = mock.Mock()
        merger.find_pr.return_value = None
        sweeper = MergeSweeper(agent, worker, self.tracker, merger, MY_ID, [OWNER], now=lambda: NOW,
                               triage=mock.Mock(**{"enabled.return_value": True,
                                                   "offer.return_value": triage.Decision(True, "triage: x")}))
        self.assertEqual(sweeper._hand_back(an_issue(6, status="in_review", assignee=MY_ID), 77, "CI failed"),
                         "triage: x")
        self.assertEqual(self.tracker.handed, [])
        sweeper.triage.offer.return_value = triage.Decision(False, "outside the premise")
        sweeper._hand_back(an_issue(6, status="in_review", assignee=MY_ID), 77, "CI failed")
        self.assertEqual(self.tracker.handed, [(6, "in_review", OWNER)])
        self.assertIn("no triage: outside the premise", self.tracker.comments[-1][2])

    def test_the_keeper_lets_triage_look_at_a_conflict_first(self):
        agent, worker = self.agent()
        offered = mock.Mock(**{"enabled.return_value": True,
                               "offer.return_value": triage.Decision(True, "triage: merge_conflict incident 1")})
        keeper = BranchKeeper(agent, worker, self.tracker, None, WORKER_ID, MY_ID, [OWNER], lambda r, n: None,
                              now=lambda: NOW, triage=offered)
        pr = {"number": 77, "headRefOid": "abc", "headRefName": "claude-issue-6"}
        self.assertEqual(keeper._tell(an_issue(6, status="in_review", assignee=MY_ID), "gaia", pr, 1),
                         "conflict: triage: merge_conflict incident 1")
        self.assertEqual(self.tracker.handed, [])
        self.assertEqual(sessions.get("odysseus", 6)["conflict_noted"], "abc")

    def test_a_first_conflict_goes_to_triage_before_the_worker(self):
        agent, worker = self.agent()
        offered = mock.Mock(**{"enabled.return_value": True,
                               "offer.return_value": triage.Decision(True, "triage: merge_conflict incident 1")})
        keeper = BranchKeeper(agent, worker, self.tracker, None, WORKER_ID, MY_ID, [OWNER], lambda r, n: None,
                              now=lambda: NOW, triage=offered)
        pr = {"number": 77, "headRefOid": "abc", "headRefName": "claude-issue-6"}
        issue = an_issue(6, status="in_review", assignee=OWNER)
        self.assertEqual(keeper._conflict(issue, "gaia", pr, {}), "conflict: triage: merge_conflict incident 1")
        self.assertEqual(offered.offer.call_args.args[:2], (6, triage.CONFLICT))
        self.assertIn("merge conflict", offered.offer.call_args.args[3])
        self.assertEqual((self.tracker.issues[6]["assignee"], sessions.get("odysseus", 6)), (MY_ID, {}))
        offered.offer.return_value = triage.Decision(False, "already triaged")    # declined: the worker gets it
        self.assertIn("handed to Agent Odysseus", keeper._conflict(issue, "gaia", pr, {}))
        self.assertEqual(self.tracker.issues[6]["assignee"], WORKER_ID)
        self.assertEqual(sessions.get("odysseus", 6)["conflict_attempts"], 1)
        offered.offer.reset_mock()
        keeper._conflict(issue, "gaia", pr, {"conflict_attempts": 1, "conflict_noted": "abc"})
        offered.offer.assert_not_called()                                       # no second look before the owner


if __name__ == "__main__":
    unittest.main()
