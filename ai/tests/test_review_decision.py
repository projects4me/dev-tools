"""Who reviews a new agent PR: the budget answer in ai ship / ai pr. Run: python -m unittest discover -s tests"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import rabbitbudget, ship, workflow  # noqa: E402

URL = "https://github.com/projects4me/dev-tools/pull/9"
CFG = {"codeRabbitRepos": ["gaia"], "reviewer": "hammad"}


def fake_request(decision, used=12, limit=25):
    return mock.Mock(return_value={"decision": decision, "used": used, "limit": limit, "reason": "r"})


class DecisionTest(unittest.TestCase):
    def ask(self, request, known=None, pending=None, repo="gaia"):
        with mock.patch("lib.coderabbit.config", return_value=CFG), \
                mock.patch.object(rabbitbudget, "request", request), \
                mock.patch.object(rabbitbudget, "known_decision", return_value=known), \
                mock.patch.object(rabbitbudget, "settings", return_value={"everyPrUntil": 10}), \
                mock.patch.object(workflow, "load_pending_handoff", return_value=pending), \
                mock.patch.object(workflow, "say"):
            return workflow.review_decision(repo, 5, URL, 3)

    def test_coderabbit_answer(self):
        request = fake_request("coderabbit")
        self.assertEqual(self.ask(request)["decision"], "coderabbit")
        request.assert_called_once_with("gaia", 9, "pr-opened", 3, False, 5)

    def test_human_answer_has_the_note(self):
        note = workflow.human_review_note(self.ask(fake_request("human")))
        self.assertIn("12 of 25 reviews used in the last 24 hours", note)
        self.assertIn("small PRs go to you from 10", note)
        self.assertIn("Please review by hand", note)

    def test_no_note_for_coderabbit(self):
        self.assertEqual(workflow.human_review_note({"decision": "coderabbit"}), "")

    def test_resume_does_not_ask_twice(self):
        request = fake_request("coderabbit")
        self.assertEqual(self.ask(request, known="human")["decision"], "human")
        self.assertEqual(self.ask(request, pending={"pr": URL})["decision"], "coderabbit")
        request.assert_not_called()

    def test_repo_without_coderabbit_is_not_asked(self):
        request = fake_request("coderabbit")
        self.assertEqual(self.ask(request, repo="ares")["decision"], "absent")
        request.assert_not_called()

    def test_failed_request_means_human(self):
        self.assertEqual(self.ask(mock.Mock(side_effect=RuntimeError("x")))["decision"], "human")


class ShipTest(unittest.TestCase):
    def run_ship(self, decision, defer=True):
        args = mock.Mock(repo="gaia", issue=5, file="f", no_tests="docs", dry_run=False, defer_handoff=defer)
        spec = {"title": "t", "files": ["a", "b", "c"], "what": "w", "why": "y"}
        hand_off = mock.Mock()
        with mock.patch.object(ship, "load_spec", return_value=spec), \
                mock.patch.object(ship, "size_result", return_value={"count": 3, "line": "l", "warning": None,
                                                                     "reason": None}), \
                mock.patch.object(ship, "config", return_value=CFG), \
                mock.patch("lib.coderabbit.config", return_value=CFG), \
                mock.patch.object(ship, "load_progress", return_value={"commit": "abc", "pr": URL,
                                                                       "testLines": ["ok"]}), \
                mock.patch.object(ship, "save_progress"), mock.patch.object(ship, "issue_ref", return_value="r-5"), \
                mock.patch.object(ship, "git", return_value="abc"), mock.patch.object(ship, "say"), \
                mock.patch.object(ship, "cost_warning", return_value=None), \
                mock.patch.object(ship, "cost_note", return_value=""), \
                mock.patch.object(ship, "base_branch", return_value="dev"), \
                mock.patch.object(ship, "_progress_file"), \
                mock.patch.object(ship, "require_matching_workspace"), \
                mock.patch.object(ship, "announce_checkout"), \
                mock.patch.object(workflow, "review_decision", return_value=decision) as ask, \
                mock.patch.object(workflow, "pr_hand_off", hand_off):
            ship.cmd_ship(args)
        return ask, hand_off

    def test_coderabbit_defers(self):
        ask, hand_off = self.run_ship({"decision": "coderabbit"})
        ask.assert_called_once_with("gaia", 5, URL, 3)
        self.assertEqual(hand_off.call_args.kwargs["defer"], "gaia")

    def test_human_hands_off_with_note(self):
        _, hand_off = self.run_ship({"decision": "human", "used": 12, "limit": 25, "everyPrUntil": 10})
        self.assertIsNone(hand_off.call_args.kwargs["defer"])
        self.assertIn("Not sent to CodeRabbit: 12 of 25", hand_off.call_args.kwargs["notes_html"])


if __name__ == "__main__":
    unittest.main()
