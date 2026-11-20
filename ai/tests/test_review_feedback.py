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


class CliWiringTest(unittest.TestCase):
    def test_comment_parses_text_without_a_file(self):
        import ai
        args = ai.build_parser().parse_args(["comment", "7", "--text", "<p>hi</p>"])
        self.assertEqual((args.issue, args.text, args.file), (7, "<p>hi</p>", None))

    def test_comment_still_parses_a_file(self):
        import ai
        args = ai.build_parser().parse_args(["comment", "7", "note.html"])
        self.assertEqual((args.file, args.text), ("note.html", None))

    def test_review_parses_issue(self):
        import ai
        args = ai.build_parser().parse_args(["review", "dev-tools", "5", "--open", "--issue", "9"])
        self.assertEqual(args.issue, 9)

    def test_review_issue_prints_gaia_comments_ci_and_verdict(self):
        out = []
        gaia = [c("Hammad", "2026-11-10 12:00", "please fix")]
        with mock.patch.object(review, "_api", return_value=[]),                 mock.patch.object(review, "resolved_threads", return_value=set()),                 mock.patch.object(review, "_issue_comments", return_value=gaia),                 mock.patch.object(review, "ci_state", return_value=("fail", "failed: build")),                 mock.patch.object(review, "say", out.append):
            review.cmd_review(Namespace(repo="dev-tools", pr=5, open=True, raw=False, issue=9))
        text = "\n".join(out)
        self.assertIn("please fix", text)
        self.assertIn("NEEDS ADDRESSING", text)
        self.assertIn("failing CI (failed: build)", text)

    def test_review_without_issue_adds_no_verdict(self):
        out = []
        with mock.patch.object(review, "_api", return_value=[]),                 mock.patch.object(review, "resolved_threads", return_value=set()),                 mock.patch.object(review, "say", out.append):
            review.cmd_review(Namespace(repo="dev-tools", pr=5, open=True, raw=False, issue=None))
        self.assertNotIn("ADDRESS", "\n".join(out))


class CommitDeletionsTest(unittest.TestCase):
    def test_paths_are_staged_with_add_all_so_deletions_commit(self):
        calls = []
        with mock.patch.object(workflow, "check_commit", return_value="claude-issue-1"),                 mock.patch.object(workflow, "ody", lambda repo, *a, **k: calls.append(a)),                 mock.patch.object(workflow, "git", return_value=""),                 mock.patch.object(workflow, "git_dir", return_value=Path(__import__("tempfile").mkdtemp())),                 mock.patch.object(workflow, "say"):
            workflow.commit_files("dev-tools", 1, "msg", ["gone.txt"], push=False)
        self.assertEqual(calls[0], ("git", "add", "-A", "--", "gone.txt"))

    def test_already_staged_deletion_is_not_added_again(self):
        calls = []
        with mock.patch.object(workflow, "check_commit", return_value="claude-issue-1"), \
                mock.patch.object(workflow, "ody", lambda repo, *a, **k: calls.append(a)), \
                mock.patch.object(workflow, "git", return_value="gone.txt"), \
                mock.patch.object(workflow, "git_dir", return_value=Path(__import__("tempfile").mkdtemp())), \
                mock.patch.object(workflow, "say"):
            workflow.commit_files("dev-tools", 1, "msg", ["gone.txt", "new.txt"], push=False)
        self.assertEqual(calls[0], ("git", "add", "-A", "--", "new.txt"))
        self.assertNotIn("gone.txt", calls[0])


if __name__ == "__main__":
    unittest.main()
