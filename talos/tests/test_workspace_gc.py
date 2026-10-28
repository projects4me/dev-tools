"""Workspace gc with real git repos in a temp dir: finished removed, active or dirty kept, dry run lists both."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import workspace_gc, workspaces  # noqa: E402
from test_workspaces import WorkspacesTest  # noqa: E402


class GcTest(unittest.TestCase):
    def setUp(self):                     # the workspaces fixture (temp git repos), without re-running its tests
        self.fixture = WorkspacesTest("setUp")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        for issue in (1, 2, 3):
            self.fixture.ensure(issue)

    def run_gc(self, statuses, running=(), merged=(), dry_run=False):
        res = workspace_gc.collect(self.root, statuses, set(running), lambda i: i in merged, dry_run)
        return {i: a for i, a, _ in res}

    def test_removes_finished_keeps_active_and_running(self):
        res = self.run_gc({1: "done", 2: "in_progress", 3: "done"}, running=[3])
        self.assertEqual(res, {1: "removed", 2: "kept", 3: "kept"})
        self.assertFalse(workspaces.path(self.root, 1).exists())
        self.assertTrue(workspaces.path(self.root, 3).exists())

    def test_dry_run_lists_and_removes_nothing(self):
        res = self.run_gc({1: "wont_fix", 2: "in_progress"}, dry_run=True)
        self.assertEqual(res[1], "would-remove")
        self.assertEqual(res[2], "kept")
        self.assertTrue(workspaces.path(self.root, 1).exists())

    def test_dirty_kept_unless_closed(self):
        for i in (1, 2):
            (workspaces.path(self.root, i) / "gaia" / "new.txt").write_text("x", encoding="utf-8")
        res = self.run_gc({1: "done", 2: "closed"})
        self.assertEqual(res, {1: "kept", 2: "removed", 3: "kept"})

    def test_merged_pr_counts_as_finished(self):
        self.assertEqual(self.run_gc({3: "in_review"}, merged=[3])[3], "removed")

    def test_prune_runs(self):
        workspaces.prune(self.root)


if __name__ == "__main__":
    unittest.main()
