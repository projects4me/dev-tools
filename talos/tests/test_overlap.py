"""The orchestrator's overlap hold: a ready issue whose predicted files overlap an issue in flight waits for it."""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import db, registry  # noqa: E402
from lib.config import TalosError  # noqa: E402
from lib.orchestrator import overlap, policy  # noqa: E402
from lib.orchestrator import state as store  # noqa: E402
from lib.orchestrator.overlap import IssueFiles, OverlapGate  # noqa: E402
from lib.orchestrator.worker import Orchestrator  # noqa: E402
from test_orchestrator import AGAMEMNON, OWNER, WORKER_ID, FakeTracker, issue  # noqa: E402


class PolicyTest(unittest.TestCase):
    def test_overlapping(self):
        others = [(1, {"a.py"}), (2, None), (3, {"b.py", "docs/api.html", "c.py"})]
        self.assertEqual(policy.overlapping({"c.py", "b.py"}, others), (3, ["b.py", "c.py"]))
        self.assertEqual(policy.overlapping({"a.py", "b.py"}, others), (1, ["a.py"]))     # the first in flight
        self.assertIsNone(policy.overlapping({"docs/api.html"}, others, ignore=("docs/*",)))
        self.assertIsNone(policy.overlapping(None, others))
        self.assertIsNone(policy.overlapping(set(), others))
        self.assertIsNone(policy.overlapping({"z.py"}, others))

    def test_ignored_globs_cross_folders(self):
        self.assertTrue(policy.ignored("docs/v1/index.html", ("docs/*",)))
        self.assertFalse(policy.ignored("src/docs/x.md", ("docs/*",)))
        self.assertTrue(policy.ignored("config/acl.json", ("*/acl.json",)))

    def test_is_active(self):
        done = ("done", "closed")
        self.assertTrue(policy.is_active({"status": "in_progress"}, done))
        self.assertTrue(policy.is_active({"status": "in_review"}, done))
        for status in ("new", "done", "Closed", "wont_fix"):
            self.assertFalse(policy.is_active({"status": status}, done), status)


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp()) / "agamemnon"
        self.folder.mkdir()
        self.local_dir = Path(tempfile.mkdtemp())
        (self.local_dir / "gaia-mcp").mkdir()
        (self.local_dir / "gaia-mcp" / "agamemnon.env").write_text("GAIA_EMAIL=x\n", encoding="utf-8")
        patcher = mock.patch.object(registry, "LOCAL_DIR", self.local_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def load(self, **changes):
        raw = {**json.loads((AGAMEMNON / "agent.json").read_text(encoding="utf-8")), **changes}
        (self.folder / "agent.json").write_text(json.dumps(raw), encoding="utf-8")
        return registry.load_agent(self.folder).orchestration

    def test_overlap_ignore(self):
        shutil.copy(AGAMEMNON / "agent.json", self.folder / "agent.json")
        self.assertEqual(registry.load_agent(self.folder).orchestration.overlap_ignore, ("docs/*",))
        self.assertEqual(self.load(overlapIgnore=["docs/*", " config/acl.json "]).overlap_ignore,
                         ("docs/*", "config/acl.json"))
        for bad in ("docs/*", [""], [3]):
            with self.assertRaisesRegex(TalosError, "overlapIgnore"):
                self.load(overlapIgnore=bad)


class FakeFiles:
    """files_of: in-flight issues answer from `branches` (their diff) first, everything from `predicted`."""

    def __init__(self, predicted, branches=None):
        self.predicted, self.branches = predicted, branches or {}
        self.calls = []

    def clear(self):
        pass

    def __call__(self, repo, number, started):
        self.calls.append((number, started))
        return (self.branches.get(number) if started else None) or self.predicted.get(number)


class HoldTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for patcher in (mock.patch.object(db, "DB_FILE", self.tmp / "talos.db"),
                        mock.patch("lib.orchestrator.worker.log"), mock.patch("lib.orchestrator.overlap.log")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.clock = ["2026-10-31T10:00:00+05:00"]

    def orchestrator(self, tracker, files, dry_run=False, max_in_flight=3):
        o = registry.Orchestration(worker="odysseus", max_in_flight=max_in_flight)
        agent = SimpleNamespace(name="agamemnon", display_name="Agent Agamemnon", orchestration=o)
        worker = SimpleNamespace(name="odysseus", display_name="Agent Odysseus", handoff_to=OWNER, max_parallel=1,
                                 projects={"A Gaia": "gaia"}, dependency_done_statuses=("done", "closed"),
                                 repo_for=lambda project: "gaia")
        gate = OverlapGate(agent, worker, tracker, files, dry_run=dry_run, now=lambda: self.clock[0])
        return Orchestrator(agent, worker, tracker, WORKER_ID, dry_run=dry_run, now=lambda: self.clock[0],
                            branch_holder=lambda repo, number: None, overlap=gate)

    def held_comments(self, tracker):
        return [(n, title) for n, title, _ in tracker.comments if "held" in title]

    def test_holds_an_overlapping_issue_and_comments_once(self):
        tracker = FakeTracker([issue(1), issue(2), issue(9, status="in_review", assignee=OWNER)])
        files = FakeFiles({1: {"src/A.php", "src/B.php"}, 2: {"src/C.php"}}, branches={9: {"src/B.php", "x.php"}})
        self.assertEqual(self.orchestrator(tracker, files).sweep(), [(2, "new")])
        self.assertEqual(self.held_comments(tracker), [(1, "Agent Agamemnon: held until #9 is done")])
        body = next(b for n, t, b in tracker.comments if n == 1)
        self.assertIn("<code>src/B.php</code>", body)
        self.assertNotIn("src/A.php", body)
        hold = store.holds("agamemnon")[1]
        self.assertEqual((hold["blocker"], hold["files"], hold["repo"]), (9, ["src/B.php"], "gaia"))
        self.assertIn("held for overlapping files: #1", store.get("agamemnon")["last_sweep"])

        self.orchestrator(tracker, files).sweep()       # still held: no second comment
        self.assertEqual(len(self.held_comments(tracker)), 1)
        self.assertEqual(tracker.assigned, [2])

    def test_a_failed_comment_is_retried_on_the_next_sweep(self):
        tracker = FakeTracker([issue(1), issue(9, status="in_progress", assignee=WORKER_ID)])
        files = FakeFiles({1: {"a.py"}, 9: {"a.py"}})
        with mock.patch.object(tracker, "comment", side_effect=TalosError("gaia down")):
            self.orchestrator(tracker, files).sweep()
        self.assertNotIn(1, store.holds("agamemnon"))
        self.orchestrator(tracker, files).sweep()
        self.assertEqual(self.held_comments(tracker), [(1, "Agent Agamemnon: held until #9 is done")])
        self.assertIn(1, store.holds("agamemnon"))

    def test_released_on_the_sweep_after_the_blocker_is_done(self):
        tracker = FakeTracker([issue(1), issue(9, status="in_progress", assignee=WORKER_ID)])
        files = FakeFiles({1: {"a.py"}, 9: {"a.py"}})       # #9 has no branch yet: its prediction counts
        self.assertEqual(self.orchestrator(tracker, files).sweep(), [])
        self.assertIn(1, store.holds("agamemnon"))
        tracker.issues[9]["status"] = "done"
        self.assertEqual(self.orchestrator(tracker, files).sweep(), [(1, "new")])
        self.assertEqual(store.holds("agamemnon"), {})

    def test_an_issue_assigned_in_the_sweep_holds_the_next_one(self):
        tracker = FakeTracker([issue(1, priority="high"), issue(2)])
        files = FakeFiles({1: {"a.py"}, 2: {"a.py", "b.py"}})
        self.assertEqual(self.orchestrator(tracker, files).sweep(), [(1, "new")])
        self.assertEqual(store.holds("agamemnon")[2]["blocker"], 1)

    def test_no_prediction_or_ignored_files_never_hold(self):
        tracker = FakeTracker([issue(1), issue(2), issue(9, status="in_progress", assignee=WORKER_ID)])
        files = FakeFiles({2: {"docs/api/index.html"}, 9: {"docs/api/index.html", "a.py"}})
        self.assertEqual(self.orchestrator(tracker, files).sweep(), [(1, "new"), (2, "new")])
        self.assertEqual(store.holds("agamemnon"), {})

    def test_other_repos_and_finished_issues_are_not_in_flight(self):
        tracker = FakeTracker([issue(1), issue(8, status="done", assignee=WORKER_ID),
                               issue(9, status="wont_fix", assignee=OWNER)])
        files = FakeFiles({1: {"a.py"}, 8: {"a.py"}, 9: {"a.py"}})
        self.assertEqual(self.orchestrator(tracker, files).sweep(), [(1, "new")])
        self.assertEqual(files.calls, [])       # nothing in flight: no prediction needed

    def test_a_hold_the_sweep_did_not_reach_stays(self):
        tracker = FakeTracker([issue(1), issue(2), issue(9, status="in_progress", assignee=WORKER_ID)])
        files = FakeFiles({2: {"a.py"}, 9: {"a.py"}})
        self.orchestrator(tracker, files).sweep()
        self.assertEqual(list(store.holds("agamemnon")), [2])
        tracker.issues[3] = issue(3, priority="blocker")
        self.assertEqual(self.orchestrator(tracker, files).sweep(), [(3, "new")])    # the only slot, before #2
        self.assertEqual(list(store.holds("agamemnon")), [2])
        self.assertEqual(len(self.held_comments(tracker)), 1)

    def test_released_when_assigned_by_hand(self):
        tracker = FakeTracker([issue(1), issue(9, status="in_progress", assignee=WORKER_ID)])
        files = FakeFiles({1: {"a.py"}, 9: {"a.py"}})
        self.orchestrator(tracker, files).sweep()
        tracker.issues[1].update(status="in_progress", assignee=OWNER)
        self.orchestrator(tracker, files).sweep()
        self.assertEqual(store.holds("agamemnon"), {})

    def test_dry_run_records_and_comments_nothing(self):
        tracker = FakeTracker([issue(1), issue(9, status="in_progress", assignee=WORKER_ID)])
        files = FakeFiles({1: {"a.py"}, 9: {"a.py"}})
        self.assertEqual(self.orchestrator(tracker, files, dry_run=True).sweep(), [])
        self.assertEqual((tracker.comments, store.holds("agamemnon")), ([], {}))


class IssueFilesTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("lib.orchestrator.overlap.log")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_prediction_from_ai_context_cached_for_the_sweep(self):
        files = IssueFiles(lambda: {}, root=tempfile.mkdtemp())
        with mock.patch.object(overlap, "ai_json", return_value={"files": ["a.py"]}) as ai:
            self.assertEqual(files("gaia", 5, True), {"a.py"})     # no checkout: the prediction
            self.assertEqual(files("gaia", 5, False), {"a.py"})
            self.assertEqual(ai.call_count, 1)
            self.assertEqual(ai.call_args.args[1:], ("context", "5", "--repo", "gaia"))
            files.clear()
            files("gaia", 5, False)
            self.assertEqual(ai.call_count, 2)

    def test_unknown_prediction_is_none(self):
        files = IssueFiles(lambda: {}, root=tempfile.mkdtemp())
        with mock.patch.object(overlap, "ai_json", return_value={"files": []}):
            self.assertIsNone(files("gaia", 5, False))
        files.clear()
        with mock.patch.object(overlap, "ai_json", side_effect=TalosError("gaia down")):
            self.assertIsNone(files("gaia", 6, False))

    def test_branch_diff_against_the_base(self):
        files = IssueFiles(lambda: {}, root=tempfile.mkdtemp())
        (files.root / "gaia" / ".git").mkdir(parents=True)
        done = SimpleNamespace(returncode=0, stdout="src/A.php\nsrc/B.php\n")
        with mock.patch.object(overlap.subprocess, "run", return_value=done) as run, \
                mock.patch.object(overlap.workspaces, "base_branch", return_value="dev"):
            self.assertEqual(files.branch("gaia", 7), {"src/A.php", "src/B.php"})
        self.assertIn("origin/dev...claude-issue-7", run.call_args.args[0])
        with mock.patch.object(overlap.subprocess, "run", return_value=SimpleNamespace(returncode=128, stdout="", stderr="unknown revision")), \
                mock.patch.object(overlap.workspaces, "base_branch", return_value="dev"), \
                mock.patch.object(overlap, "ai_json", return_value={"files": ["p.php"]}):
            self.assertEqual(files("gaia", 7, True), {"p.php"})    # no branch yet: the prediction


if __name__ == "__main__":
    unittest.main()
