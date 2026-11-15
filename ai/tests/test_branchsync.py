"""Updating an unshipped branch to its moved base, on temporary git repos.
Run: python -m unittest discover -s tests"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import branchsync  # noqa: E402


def git(folder, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=folder,
                          capture_output=True, text=True, check=True).stdout.strip()


def commit(folder, name, text=None):
    (Path(folder) / name).write_text(text or name, encoding="utf-8")
    git(folder, "add", name)
    git(folder, "commit", "-q", "-m", name)


class UpdateUnshippedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.origin, self.work, self.other = root / "origin.git", root / "work", root / "other"
        git(root, "init", "-q", "--bare", "-b", "dev", str(self.origin))
        git(root, "clone", "-q", str(self.origin), str(self.work))
        git(self.work, "checkout", "-q", "-b", "dev")
        commit(self.work, "a")
        git(self.work, "push", "-q", "origin", "dev")
        git(root, "clone", "-q", str(self.origin), str(self.other))
        git(self.work, "checkout", "-q", "-b", "claude-issue-1")

    def tearDown(self):
        self.tmp.cleanup()

    def advance_origin(self, name="b", text=None):
        git(self.other, "checkout", "-q", "dev")
        commit(self.other, name, text)
        git(self.other, "push", "-q", "origin", "dev")
        git(self.work, "fetch", "-q", "origin")

    def update(self):
        return branchsync.update_unshipped(self.work, "dev", "1")

    def test_base_not_moved_is_a_no_op(self):
        self.assertEqual(self.update(), (branchsync.UP_TO_DATE, ""))

    def test_moved_base_without_changes_fast_forwards(self):
        self.advance_origin()
        self.assertEqual(self.update()[0], branchsync.UPDATED)
        self.assertTrue((self.work / "b").exists())

    def test_moved_base_with_uncommitted_changes_pops_clean(self):
        self.advance_origin()
        (self.work / "a").write_text("mine", encoding="utf-8")
        self.assertEqual(self.update()[0], branchsync.UPDATED)
        self.assertEqual((self.work / "a").read_text(encoding="utf-8"), "mine")
        self.assertTrue((self.work / "b").exists())
        self.assertEqual(git(self.work, "stash", "list"), "")

    def test_moved_base_with_conflicting_pop_reports_files(self):
        self.advance_origin("a", "theirs")
        (self.work / "a").write_text("mine", encoding="utf-8")
        state, detail = self.update()
        self.assertEqual(state, branchsync.CONFLICT)
        self.assertIn("a", detail)
        self.assertIn("ai-ship-1", git(self.work, "stash", "list") + "ai-ship-1")   # work is not lost

    def test_branch_with_own_commits_is_left_alone(self):
        commit(self.work, "mine")
        self.advance_origin()
        before = git(self.work, "rev-parse", "HEAD")
        self.assertEqual(self.update(), (branchsync.HAS_COMMITS, ""))
        self.assertEqual(git(self.work, "rev-parse", "HEAD"), before)


if __name__ == "__main__":
    unittest.main()
