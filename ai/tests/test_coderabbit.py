"""CodeRabbit state classification from recorded GitHub API shapes. Run: python -m unittest discover -s tests"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import coderabbit  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
