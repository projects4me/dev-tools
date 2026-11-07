"""HumanMergeSweeper: an agent PR a person merged sets its in_review issue done. Run: python -m unittest discover -s tests"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import db, registry  # noqa: E402
from lib.config import TalosError  # noqa: E402
from lib.orchestrator import github  # noqa: E402
from lib.orchestrator import state as store  # noqa: E402
from lib.orchestrator.human_merges import HumanMergeSweeper  # noqa: E402

OWNER = "7"


def issue(number, **fields):
    return {"number": number, "project": "A Gaia", "milestone": "aga-v1", "status": "in_review", "assignee": OWNER,
            **fields}


class FakeTracker:
    def __init__(self, issues):
        self.issues = {i["number"]: dict(i) for i in issues}
        self.comments = []

    def issues_in(self, project, prefix):
        return [dict(i) for i in self.issues.values() if i["project"] == project]

    def set_status(self, number, status):
        self.issues[number]["status"] = status

    def comment(self, number, title, body):
        self.comments.append((number, title, body))


class HumanMergesTest(unittest.TestCase):
    def setUp(self):
        db_patch = mock.patch.object(db, "DB_FILE", Path(tempfile.mkdtemp()) / "talos.db")
        repos = mock.patch.object(github, "known_repos", return_value={"gaia": "o/gaia"})
        log = mock.patch("lib.orchestrator.human_merges.log")
        for p in (db_patch, repos, log):
            p.start()
            self.addCleanup(p.stop)
        self.prs, self.calls = [], []

    def sweeper(self, tracker, dry_run=False):
        def gh(*args):
            self.calls.append(args)
            return json.dumps(self.prs)
        agent = SimpleNamespace(name="agamemnon", display_name="Agent Agamemnon",
                                orchestration=registry.Orchestration(worker="odysseus"))
        worker = SimpleNamespace(projects={"A Gaia": "gaia"}, repo_for=lambda project: "gaia")
        return HumanMergeSweeper(agent, worker, tracker, gh, "agent-odysseus", "agent-agamemnon", [OWNER], dry_run=dry_run)

    def test_merged_by_a_person_is_set_done_with_a_comment(self):
        self.prs = [{"number": 9, "state": "MERGED", "mergedBy": {"login": "hammad"}}]
        tracker = FakeTracker([issue(5)])
        note = self.sweeper(tracker).sweep()
        self.assertEqual(tracker.issues[5]["status"], "done")
        self.assertIn("merged by hammad", note)
        self.assertIn("PR gaia#9 merged by hammad; set done", tracker.comments[0][2])
        self.assertIn("--author", self.calls[0])
        self.assertIn("claude-issue-5", self.calls[0])
        self.assertEqual(self.sweeper(tracker).sweep(), "")     # safe to repeat: done issues are skipped

    def test_merged_by_agamemnon_is_left_to_its_own_flow(self):
        self.prs = [{"number": 9, "state": "MERGED", "mergedBy": {"login": "agent-agamemnon"}}]
        record = store.open_merge("agamemnon", 5, "gaia", 9, "2026-10-31T10:00:00+05:00")
        for closing in (False, True):
            if closing:
                store.close_merge(record["id"], "2026-10-31T10:05:00+05:00", "merged", merge_sha="abc")
            tracker = FakeTracker([issue(5)])
            self.assertEqual(self.sweeper(tracker).sweep(), "")
            self.assertEqual(tracker.issues[5]["status"], "in_review")
            self.assertEqual(tracker.comments, [])

    def test_a_merge_with_no_person_on_record_is_not_called_a_person_merge(self):
        for merged_by in (None, {"login": "agent-odysseus"}, {"login": "agent-agamemnon"}):
            self.prs = [{"number": 9, "state": "MERGED", "mergedBy": merged_by}]
            tracker = FakeTracker([issue(5)])
            self.assertEqual(self.sweeper(tracker).sweep(), "")
            self.assertEqual(tracker.issues[5]["status"], "in_review")

    def test_a_handed_back_record_does_not_hide_a_person_merge(self):
        self.prs = [{"number": 9, "state": "MERGED", "mergedBy": {"login": "hammad"}}]
        record = store.open_merge("agamemnon", 5, "gaia", 9, "2026-10-31T10:00:00+05:00")
        store.close_merge(record["id"], "2026-10-31T10:05:00+05:00", "handed_back", detail="CI")
        tracker = FakeTracker([issue(5)])
        self.sweeper(tracker).sweep()
        self.assertEqual(tracker.issues[5]["status"], "done")

    def test_not_merged_closed_open_or_no_pr_is_untouched(self):
        merged = {"number": 9, "state": "MERGED", "mergedBy": {"login": "hammad"}}
        for prs in ([], [{"number": 9, "state": "OPEN"}], [{"number": 9, "state": "CLOSED"}],
                    [{"number": 8, "state": "OPEN"}, {"number": 9, "state": "OPEN"}],
                    [{"number": 8, "state": "OPEN"}, merged]):
            self.prs = prs
            tracker = FakeTracker([issue(5)])
            self.assertEqual(self.sweeper(tracker).sweep(), "")
            self.assertEqual(tracker.issues[5]["status"], "in_review")
            self.assertEqual(tracker.comments, [])

    def test_other_issues_are_not_looked_at(self):
        self.prs = [{"number": 9, "state": "MERGED", "mergedBy": {"login": "hammad"}}]
        tracker = FakeTracker([issue(1, assignee="50"), issue(2, status="in_progress"), issue(3, project="B")])
        self.assertEqual(self.sweeper(tracker).sweep(), "")
        self.assertEqual(self.calls, [])

    def test_dry_run_changes_nothing(self):
        self.prs = [{"number": 9, "state": "MERGED", "mergedBy": {"login": "hammad"}}]
        tracker = FakeTracker([issue(5)])
        self.assertIn("dry run", self.sweeper(tracker, dry_run=True).sweep())
        self.assertEqual(tracker.issues[5]["status"], "in_review")

    def test_a_github_error_is_noted_and_the_next_issue_goes_on(self):
        tracker = FakeTracker([issue(5), issue(6)])
        sweeper = self.sweeper(tracker)
        answers = iter([TalosError("gh down"), json.dumps([{"number": 9, "state": "MERGED",
                                                              "mergedBy": {"login": "hammad"}}])])

        def gh(*args):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer
        sweeper.gh = gh
        note = sweeper.sweep()
        self.assertIn("#5 error", note)
        self.assertEqual(tracker.issues[6]["status"], "done")


if __name__ == "__main__":
    unittest.main()
