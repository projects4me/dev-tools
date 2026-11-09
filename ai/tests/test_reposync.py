"""ai sync: fast-forwarding the base branch without switching branches, on temporary git repos.
Run: python -m unittest discover -s tests"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import reposync  # noqa: E402


def git(folder, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=folder,
                          capture_output=True, text=True, check=True).stdout.strip()


def commit(folder, name):
    (Path(folder) / name).write_text(name, encoding="utf-8")
    git(folder, "add", name)
    git(folder, "commit", "-q", "-m", name)


class SyncCheckoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.origin, self.work, self.other = root / "origin.git", root / "work", root / "other"
        git(root, "init", "-q", "--bare", "-b", "dev", str(self.origin))
        git(root, "clone", "-q", str(self.origin), str(self.work))
        git(self.work, "checkout", "-q", "-b", "dev")
        commit(self.work, "a")
        git(self.work, "push", "-q", "origin", "dev")
        git(root, "clone", "-q", str(self.origin), str(self.other))   # a second clone that advances origin

    def tearDown(self):
        self.tmp.cleanup()

    def advance_origin(self, name="b"):
        git(self.other, "checkout", "-q", "dev")
        commit(self.other, name)
        git(self.other, "push", "-q", "origin", "dev")

    def test_issue_branch_checked_out_keeps_branch_and_tree(self):
        git(self.work, "checkout", "-q", "-b", "claude-issue-1")
        (self.work / "wip.txt").write_text("wip", encoding="utf-8")     # untracked work in progress
        self.advance_origin()
        state, _ = reposync.sync_checkout(self.work, "dev")
        self.assertEqual(state, reposync.SYNCED)
        self.assertEqual(git(self.work, "rev-parse", "dev"), git(self.work, "rev-parse", "origin/dev"))
        self.assertEqual(git(self.work, "branch", "--show-current"), "claude-issue-1")
        self.assertTrue((self.work / "wip.txt").exists())
        self.assertFalse((self.work / "b").exists())

    def test_base_checked_out_and_clean_fast_forwards_the_tree(self):
        self.advance_origin()
        self.assertEqual(reposync.sync_checkout(self.work, "dev")[0], reposync.SYNCED)
        self.assertTrue((self.work / "b").exists())

    def test_base_checked_out_and_dirty_is_skipped(self):
        self.advance_origin()
        (self.work / "a").write_text("changed", encoding="utf-8")
        state, message = reposync.sync_checkout(self.work, "dev")
        self.assertEqual(state, reposync.SKIPPED)
        self.assertIn("uncommitted", message)
        self.assertEqual(reposync.behind(self.work, "dev"), 1)

    def test_diverged_base_is_skipped(self):
        git(self.work, "checkout", "-q", "-b", "claude-issue-1")
        git(self.work, "checkout", "-q", "dev")
        commit(self.work, "local-only")
        git(self.work, "checkout", "-q", "claude-issue-1")
        self.advance_origin()
        before = git(self.work, "rev-parse", "dev")
        self.assertEqual(reposync.sync_checkout(self.work, "dev")[0], reposync.SKIPPED)
        self.assertEqual(git(self.work, "rev-parse", "dev"), before)

    def test_up_to_date_and_fetch_only(self):
        self.assertEqual(reposync.sync_checkout(self.work, "dev")[0], reposync.CURRENT)
        self.advance_origin()
        git(self.work, "checkout", "-q", "-b", "claude-issue-1")
        before = git(self.work, "rev-parse", "dev")
        self.assertEqual(reposync.sync_checkout(self.work, "dev", fetch_only=True)[0], reposync.FETCHED)
        self.assertEqual(git(self.work, "rev-parse", "dev"), before)
        self.assertEqual(reposync.behind(self.work, "dev"), 1)

    def test_fetch_failure_is_skipped(self):
        git(self.work, "remote", "set-url", "origin", str(Path(self.tmp.name) / "missing.git"))
        self.assertEqual(reposync.sync_checkout(self.work, "dev")[0], reposync.SKIPPED)


if __name__ == "__main__":
    unittest.main()
