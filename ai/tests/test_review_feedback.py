"""`ai review --issue`: Gaia comments, CI and the closing verdict. Run: python -m unittest discover -s tests"""

import sys
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import review, workflow  # noqa: E402
from lib.common import AiError  # noqa: E402


def c(by, date, text="x"):
    return {"by": by, "date": date, "comment": text}


class FeedbackTest(unittest.TestCase):
    def test_only_people_after_the_agents_last_comment(self):
        comments = [c("Hammad", "2026-11-10 09:00"), c("Agent Odysseus", "2026-11-10 10:00"),
                    c("Hammad", "2026-11-10 11:00"), c("Agent Achilles", "2026-11-10 11:30")]
        self.assertEqual(review.human_comments_since_agent(comments), [])
        comments.append(c("Hammad", "2026-11-10 12:00", "CI is failing"))
        self.assertEqual([x["comment"] for x in review.human_comments_since_agent(comments)], ["CI is failing"])

    def test_without_agent_comments_all_people_count(self):
        self.assertEqual(len(review.human_comments_since_agent([c("Hammad", "2026-11-10 09:00")])), 1)

    def test_nothing_to_address_only_when_all_clear(self):
        self.assertTrue(review.feedback_summary(0, ("ok", "3 passed"), []).startswith("NOTHING TO ADDRESS"))
        self.assertIn("failing CI (failed: build)", review.feedback_summary(0, ("fail", "failed: build"), []))
        self.assertTrue(review.feedback_summary(1, ("ok", ""), []).startswith("NEEDS ADDRESSING"))
        self.assertTrue(review.feedback_summary(0, ("ok", ""), [c("H", "d")]).startswith("NEEDS ADDRESSING"))
        self.assertTrue(review.feedback_summary(0, ("pending", "still running: x"), []).startswith("NEEDS"))

    def test_ci_state_names_the_failing_checks(self):
        def api(repo, path, *extra):
            if path.endswith("/pulls/5"):
                return {"head": {"sha": "abc"}}
            if path.endswith("check-runs"):
                return {"check_runs": [{"name": "build", "status": "completed", "conclusion": "failure"}]}
            return []
        with mock.patch.object(review, "_api", api):
            state, detail = review.ci_state("dev-tools", 5)
        self.assertEqual(state, "fail")
        self.assertIn("build", detail)


class CommentTextTest(unittest.TestCase):
    def test_inline_text(self):
        with mock.patch.object(workflow, "tracker_call") as call, mock.patch.object(workflow, "say"):
            workflow.cmd_comment(Namespace(issue=7, file=None, text="  <p>hi</p> "))
        call.assert_called_once_with("add_comment", issue_number=7, comment="<p>hi</p>")

    def test_needs_text_or_file(self):
        with self.assertRaises(AiError):
            workflow.cmd_comment(Namespace(issue=7, file=None, text=None))


if __name__ == "__main__":
    unittest.main()
