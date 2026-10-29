"""CodeRabbit state classification from recorded GitHub API shapes. Run: python -m unittest discover -s tests"""

import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import coderabbit  # noqa: E402
from lib.common import AiError  # noqa: E402

BOT = {"login": "coderabbitai[bot]", "type": "Bot"}
AGENT = {"login": "agent-odysseus", "type": "User"}
OK = {"state": "success", "description": "Review completed", "context": "CodeRabbit"}
WALKTHROUGH = {"user": BOT, "body": "summarize by coderabbit.ai\nNo actionable comments were generated"}


def thread(i, user=BOT, reply_to=None):
    return {"id": i, "user": user, "in_reply_to_id": reply_to, "path": "a.py", "line": 1, "body": "x"}


def classify(status, issue=(), review=(), resolved=(), waited_out=False):
    return coderabbit.classify(status, list(issue), list(review), set(resolved), "agent-odysseus", waited_out)


class RabbitStateTest(unittest.TestCase):
    def test_pending_without_status_or_with_pending_status(self):
        self.assertEqual(classify(None)["state"], "pending")
        self.assertEqual(classify({"state": "pending", "description": "Review in progress"})["state"], "pending")

    def test_clean(self):
        self.assertEqual(classify(OK, [WALKTHROUGH])["state"], "clean")

    def test_findings_lists_open_bot_threads(self):
        r = classify(OK, review=[thread(1), thread(2), thread(3)], resolved=[3])
        self.assertEqual((r["state"], r["open"], r["comment_ids"]), ("findings", 2, [1, 2]))

    def test_declined_thread_counts_as_answered(self):
        r = classify(OK, review=[thread(1), thread(10, AGENT, reply_to=1)])
        self.assertEqual(r["state"], "clean")

    def test_bot_followup_reopens_answered_thread(self):
        r = classify(OK, review=[thread(1), thread(10, AGENT, 1), thread(11, BOT, 1)])
        self.assertEqual((r["state"], r["comment_ids"]), ("findings", [1]))

    def test_human_threads_are_not_bot_findings(self):
        human = {"login": "someone", "type": "User"}
        self.assertEqual(classify(OK, review=[thread(1, human)])["state"], "clean")

    def test_paused_and_rate_limited(self):
        for text in ("Auto reviews are paused for this PR", "Rate limit exceeded, try again"):
            r = classify(None, [{"user": BOT, "body": text}])
            self.assertEqual(r["state"], "paused", text)

    def test_completed_review_ignores_stale_paused_note(self):
        self.assertEqual(classify(OK, [{"user": BOT, "body": "Reviews paused"}])["state"], "clean")

    def test_absent_only_after_waiting(self):
        self.assertEqual(classify(None, waited_out=True)["state"], "absent")
        self.assertEqual(classify({"state": "failure", "description": "boom"})["state"], "absent")


def run(status, conclusion=None, slug="coderabbitai", started="2026-10-29T10:00:00Z"):
    return {"name": "CodeRabbit", "app": {"slug": slug}, "status": status, "conclusion": conclusion,
            "started_at": started}


class CheckRunTest(unittest.TestCase):
    def review(self, runs, statuses=()):
        return coderabbit.review_status(list(runs), list(statuses))

    def test_in_progress_and_queued_are_pending(self):
        for st in ("queued", "in_progress"):
            self.assertEqual(classify(self.review([run(st)]))["state"], "pending")

    def test_completed_with_findings(self):
        status = self.review([run("completed", "success")])
        self.assertEqual(classify(status, review=[thread(1)])["state"], "findings")

    def test_completed_clean(self):
        status = self.review([run("completed", "success")])
        self.assertEqual(classify(status, [WALKTHROUGH])["state"], "clean")

    def test_failure_is_absent_with_conclusion(self):
        r = classify(self.review([run("completed", "failure")]))
        self.assertEqual(r["state"], "absent")
        self.assertIn("failure", r["detail"])

    def test_other_apps_check_runs_are_ignored(self):
        self.assertIsNone(self.review([run("completed", "success", slug="github-actions")]))

    def test_newest_check_run_wins(self):
        runs = [run("completed", "success", started="2026-10-29T09:00:00Z"), run("in_progress")]
        self.assertEqual(self.review(runs)["state"], "pending")

    def test_check_run_beats_legacy_status(self):
        self.assertEqual(self.review([run("in_progress")], [OK])["state"], "pending")

    def test_legacy_status_without_check_run(self):
        self.assertEqual(self.review([], [{"state": "pending", "context": "Other"}, OK]), OK)
        self.assertEqual(classify(self.review([], [OK]), [WALKTHROUGH])["state"], "clean")


class EnabledReposTest(unittest.TestCase):
    def cfg(self, repos):
        return mock.patch.object(coderabbit, "config", return_value={"codeRabbitRepos": repos, "agentLogin": "a"})

    def test_enabled_only_for_listed_repos(self):
        with self.cfg(["gaia", "hermes"]):
            self.assertTrue(coderabbit.enabled("gaia"))
            self.assertFalse(coderabbit.enabled("ares"))

    def test_unknown_name_is_an_error(self):
        with self.cfg(["gaia", "nope"]), self.assertRaises(AiError):
            coderabbit.enabled("gaia")

    def test_rabbit_on_a_disabled_repo_is_absent_without_api_calls(self):
        args = mock.Mock(repo="ares", pr=1, json=True, wait=True, timeout=20)
        with self.cfg(["gaia"]), mock.patch.object(coderabbit, "head_sha") as head:
            self.assertEqual(coderabbit.cmd_rabbit(args), coderabbit.EXIT_CODES["absent"])
            head.assert_not_called()


if __name__ == "__main__":
    unittest.main()
