"""The merge gate (ai mergecheck / ai merge) from recorded GitHub API shapes. Run: python -m unittest discover -s tests"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import mergegate  # noqa: E402

BOT = {"login": "coderabbitai[bot]", "type": "Bot"}
AUTHOR = {"login": "agent-odysseus", "type": "User"}
MERGER = {"login": "agent-agamemnon", "type": "User"}
HUMAN = {"login": "hammad", "type": "User"}
AGENTS = ("agent-odysseus", "agent-agamemnon")
PUSHED = "2026-10-31T10:00:00Z"


def run(name, status="completed", conclusion="success"):
    return {"name": name, "status": status, "conclusion": conclusion, "app": {"slug": name}}


def pr(**fields):
    base = {"state": "open", "draft": False, "mergeable": True, "mergeable_state": "blocked", "labels": [],
            "body": "## How tested\n```\nRESULT: PASS\n```", "head": {"sha": "abc1234def"}}
    return {**base, **fields}


def humans(reviews=(), issue=(), review=(), resolved=()):
    return mergegate.check_humans(list(reviews), list(issue), list(review), set(resolved), PUSHED, AGENTS)["state"]


class ChecksTest(unittest.TestCase):
    def test_open_draft_and_closed(self):
        self.assertEqual(mergegate.check_open(pr())["state"], "ok")
        self.assertEqual(mergegate.check_open(pr(draft=True))["state"], "fail")
        self.assertEqual(mergegate.check_open(pr(state="closed"))["state"], "fail")

    def test_coderabbit(self):
        self.assertEqual(mergegate.check_coderabbit(True, {"state": "clean"})["state"], "ok")
        self.assertEqual(mergegate.check_coderabbit(True, {"state": "pending"})["state"], "pending")
        findings = mergegate.check_coderabbit(True, {"state": "findings", "open": 2, "comment_ids": [1, 2]})
        self.assertEqual((findings["state"], findings["detail"]), ("fail", "2 open CodeRabbit thread(s)"))
        self.assertEqual(mergegate.check_coderabbit(True, {"state": "paused"})["state"], "fail")
        self.assertEqual(mergegate.check_coderabbit(False, None)["state"], "fail")   # no CodeRabbit, no auto-merge

    def test_ci(self):
        self.assertEqual(mergegate.check_ci([run("build"), run("lint", conclusion="skipped")], [])["state"], "ok")
        self.assertEqual(mergegate.check_ci([run("build", status="in_progress", conclusion=None)], [])["state"],
                         "pending")
        failed = mergegate.check_ci([run("build", conclusion="failure"), run("lint", status="queued")], [])
        self.assertEqual((failed["state"], failed["detail"]), ("fail", "failed: build (failure)"))
        self.assertEqual(mergegate.check_ci([], [])["detail"], "no CI checks on this commit")

    def test_ci_reads_the_newest_status_per_context(self):
        statuses = [{"context": "ci/tests", "state": "success"}, {"context": "ci/tests", "state": "failure"}]
        self.assertEqual(mergegate.check_ci([], statuses)["state"], "ok")
        self.assertEqual(mergegate.check_ci([], [{"context": "ci/tests", "state": "pending"}])["state"], "pending")

    def test_tests_recorded_by_ai_ship(self):
        self.assertEqual(mergegate.check_tests(pr()["body"])["state"], "ok")
        self.assertEqual(mergegate.check_tests("## How tested\nRESULT: FAIL")["state"], "fail")
        self.assertEqual(mergegate.check_tests(None)["state"], "fail")

    def test_mergeable(self):
        self.assertEqual(mergegate.check_mergeable(pr())["state"], "ok")          # blocked = waiting for approval
        self.assertEqual(mergegate.check_mergeable(pr(mergeable=None))["state"], "pending")
        self.assertEqual(mergegate.check_mergeable(pr(mergeable=False, mergeable_state="dirty"))["state"], "fail")
        self.assertEqual(mergegate.check_mergeable(pr(mergeable_state="behind"))["state"], "fail")

    def test_people(self):
        self.assertEqual(humans(), "ok")
        self.assertEqual(humans(reviews=[{"user": HUMAN, "state": "CHANGES_REQUESTED"}]), "fail")
        self.assertEqual(humans(reviews=[{"user": HUMAN, "state": "CHANGES_REQUESTED"},
                                         {"user": HUMAN, "state": "APPROVED"}]), "ok")    # the latest review counts
        thread = {"id": 1, "user": HUMAN, "in_reply_to_id": None, "body": "why?"}
        self.assertEqual(humans(review=[thread]), "fail")
        self.assertEqual(humans(review=[thread], resolved=[1]), "ok")
        self.assertEqual(humans(review=[thread, {"id": 2, "user": AUTHOR, "in_reply_to_id": 1}]), "ok")  # answered
        self.assertEqual(humans(issue=[{"user": HUMAN, "created_at": "2026-10-31T11:00:00Z"}]), "fail")
        self.assertEqual(humans(issue=[{"user": HUMAN, "created_at": "2026-10-31T09:00:00Z"}]), "ok")   # before push
        self.assertEqual(humans(issue=[{"user": BOT, "created_at": "2026-10-31T11:00:00Z"},
                                       {"user": MERGER, "created_at": "2026-10-31T11:00:00Z"}]), "ok")

    def test_do_not_merge_label(self):
        self.assertEqual(mergegate.check_labels(pr(labels=[{"name": "Do-Not-Merge"}]))["state"], "fail")
        self.assertEqual(mergegate.check_labels(pr(labels=[{"name": "enhancement"}]))["state"], "ok")

    def test_self_blocks_changes_to_agamemnons_own_powers(self):
        pats = ["talos/agents/agamemnon/**", "talos/lib/orchestrator/**", "ai/lib/mergegate.py"]
        ok = mergegate.check_self(["ai/lib/review.py", "talos/lib/runner.py"], pats)
        self.assertEqual(ok["state"], "ok")
        for path in ("talos/agents/agamemnon/agent.json", "talos/lib/orchestrator/merge.py", "ai/lib/mergegate.py"):
            self.assertEqual(mergegate.check_self(["README.md", path], pats)["state"], "fail", path)

    def test_verdict(self):
        ok, pending, fail = ({"state": s} for s in ("ok", "pending", "fail"))
        self.assertEqual(mergegate.verdict([ok, ok]), "ok")
        self.assertEqual(mergegate.verdict([ok, pending]), "pending")
        self.assertEqual(mergegate.verdict([pending, fail]), "fail")


class MergeTest(unittest.TestCase):
    ARGS = mock.Mock(repo="gaia", pr=12, json=False)

    def test_refuses_without_approving_when_the_gate_does_not_pass(self):
        result = {"state": "fail", "sha": "abc1234def", "checks": [{"name": "ci", "state": "fail", "detail": "x"}]}
        with mock.patch.object(mergegate, "gate", return_value=result), \
                mock.patch.object(mergegate, "agamemnon") as gh, mock.patch.object(mergegate, "say"):
            self.assertEqual(mergegate.cmd_merge(self.ARGS), 1)
        gh.assert_not_called()

    def test_approves_then_merges_the_commit_the_gate_read(self):
        result = {"state": "ok", "sha": "abc1234def", "checks": []}
        with mock.patch.object(mergegate, "gate", return_value=result), \
                mock.patch.object(mergegate, "agamemnon", return_value="f00dfeed\n") as gh, \
                mock.patch.object(mergegate, "say") as say:
            self.assertEqual(mergegate.cmd_merge(self.ARGS), 0)
        calls = [c.args for c in gh.call_args_list]
        self.assertEqual(calls[0][:5], ("pr", "review", "12", "--repo", "Projects4Me/gaia"))
        self.assertIn("--approve", calls[0])
        self.assertEqual(calls[1], ("pr", "merge", "12", "--repo", "Projects4Me/gaia", "--merge",
                                    "--match-head-commit", "abc1234def"))
        say.assert_called_with("merged #12 @ abc1234 as f00dfee")


if __name__ == "__main__":
    unittest.main()
