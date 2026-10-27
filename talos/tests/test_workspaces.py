"""Per-issue workspaces with real git repos in a temp dir: worktrees, links, stickiness, removal."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import workspaces  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                           "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
                          cwd=cwd, capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL).stdout.strip()


class WorkspacesTest(unittest.TestCase):
    """A workspace holding gaia (base dev) and dev-tools (base main), clones of bare origins, plus a runtime copy."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        base = Path(self.dir.name)
        self.root = base / "ws"
        self.root.mkdir()
        for name, branch in (("gaia", "dev"), ("dev-tools", "main")):
            origin = base / f"{name}.git"
            git(base, "init", "--bare", str(origin))
            clone = self.root / name
            git(base, "clone", str(origin), str(clone))
            git(clone, "checkout", "-b", branch)
            (clone / "README.md").write_text(name, encoding="utf-8")
            git(clone, "add", ".")
            git(clone, "commit", "-m", "first")
            git(clone, "push", "origin", branch)
        self.tools = self.root / ".dev-tools-runtime"
        (self.tools / "ai").mkdir(parents=True)
        (self.tools / "ai" / "ai.py").write_text("", encoding="utf-8")
        (self.root / ".claude" / "skills").mkdir(parents=True)
        (self.root / "CLAUDE.md").write_text("# workspace", encoding="utf-8")

    def tearDown(self):
        for issue in (1, 2, 3):          # links first, so cleanup never walks into their targets
            folder = workspaces.path(self.root, issue)
            if folder.exists():
                for child in folder.iterdir():
                    if workspaces.is_link(child):
                        workspaces.unlink(child)
        self.dir.cleanup()

    def ensure(self, issue, repo="gaia"):
        return workspaces.ensure(self.root, self.tools, issue, repo)

    def test_creates_worktree_on_issue_branch_with_links_and_claude_md(self):
        folder = self.ensure(1)
        self.assertEqual(folder, self.root / ".runs" / "issue-1")
        self.assertEqual(git(folder / "gaia", "branch", "--show-current"), "claude-issue-1")
        self.assertEqual(git(self.root / "gaia", "branch", "--show-current"), "dev")   # main checkout untouched
        for name in ("dev-tools", ".dev-tools-runtime", ".claude"):
            self.assertTrue(workspaces.is_link(folder / name), name)
        self.assertTrue((folder / "dev-tools" / "ai" / "ai.py").exists())
        self.assertTrue((folder / ".claude" / "skills").is_dir())
        self.assertEqual((folder / "CLAUDE.md").read_text(encoding="utf-8"), "# workspace")
        self.assertEqual((self.root / ".runs" / ".gitignore").read_text(encoding="utf-8"), "*\n")
        self.assertEqual(workspaces.env(folder), {"P4M_WORKSPACE": str(folder)})

    def test_two_issues_in_one_repo_keep_their_own_branch_and_changes(self):
        one, two = self.ensure(1), self.ensure(2)
        (one / "gaia" / "README.md").write_text("one", encoding="utf-8")
        (two / "gaia" / "new.txt").write_text("two", encoding="utf-8")
        self.assertEqual(git(one / "gaia", "branch", "--show-current"), "claude-issue-1")
        self.assertEqual(git(two / "gaia", "branch", "--show-current"), "claude-issue-2")
        self.assertEqual(git(one / "gaia", "status", "--porcelain"), "M README.md")
        self.assertEqual(git(two / "gaia", "status", "--porcelain"), "?? new.txt")

    def test_workspace_is_sticky(self):
        folder = self.ensure(1)
        (folder / "gaia" / "README.md").write_text("work in progress", encoding="utf-8")
        self.assertEqual(self.ensure(1), folder)
        self.assertEqual((folder / "gaia" / "README.md").read_text(encoding="utf-8"), "work in progress")

    def test_existing_branch_is_reused(self):
        git(self.root / "gaia", "branch", "claude-issue-1", "dev")
        git(self.root / "gaia", "commit", "--allow-empty", "-m", "on dev only")
        git(self.root / "gaia", "checkout", "-q", "claude-issue-1")
        git(self.root / "gaia", "commit", "--allow-empty", "-m", "earlier work")
        git(self.root / "gaia", "checkout", "-q", "dev")
        folder = self.ensure(1)
        self.assertEqual(git(folder / "gaia", "log", "-1", "--format=%s"), "earlier work")

    def test_branch_checked_out_elsewhere_is_refused(self):
        git(self.root / "gaia", "checkout", "-q", "-b", "claude-issue-1")
        with self.assertRaises(workspaces.WorkspaceError):
            self.ensure(1)

    def test_dev_tools_issue_gets_a_worktree_and_the_runtime_link(self):
        folder = self.ensure(3, "dev-tools")
        self.assertFalse(workspaces.is_link(folder / "dev-tools"))
        self.assertEqual(git(folder / "dev-tools", "branch", "--show-current"), "claude-issue-3")
        self.assertEqual(git(folder / "dev-tools", "log", "-1", "--format=%s"), "first")
        self.assertTrue(workspaces.is_link(folder / ".dev-tools-runtime"))

    def test_remove_refuses_uncommitted_work_unless_forced_and_keeps_link_targets(self):
        folder = self.ensure(1)
        (folder / "gaia" / "README.md").write_text("unsaved", encoding="utf-8")
        with self.assertRaises(workspaces.WorkspaceError):
            workspaces.remove(self.root, 1)
        self.assertTrue(workspaces.remove(self.root, 1, force=True))
        self.assertFalse(folder.exists())
        self.assertTrue((self.tools / "ai" / "ai.py").exists())
        self.assertTrue((self.root / ".claude" / "skills").is_dir())
        self.assertIn("claude-issue-1", git(self.root / "gaia", "branch", "--list", "claude-issue-1"))
        self.assertFalse(workspaces.remove(self.root, 1))

    def test_remove_clean_workspace(self):
        folder = self.ensure(3, "dev-tools")
        self.assertTrue(workspaces.remove(self.root, 3))
        self.assertFalse(folder.exists())
        self.assertTrue((self.tools / "ai" / "ai.py").exists())

    def test_link_is_a_junction_on_windows_and_a_symlink_elsewhere(self):
        folder = self.ensure(1)
        self.assertEqual((folder / "dev-tools").is_symlink(), os.name != "nt")


if __name__ == "__main__":
    unittest.main()
