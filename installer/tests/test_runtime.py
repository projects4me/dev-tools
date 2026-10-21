"""Unit tests for the runtime copy of dev-tools. Run: python -m unittest discover -s tests"""

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import runtime  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                           "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
                          cwd=cwd, capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL).stdout.strip()


class RuntimeTest(unittest.TestCase):
    """A workspace (a git repo) holding a dev-tools clone of a bare 'origin' with a main branch."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        base = Path(self.dir.name)
        self.origin = base / "origin.git"
        git(base, "init", "--bare", str(self.origin))
        self.root = base / "ws"
        self.root.mkdir()
        git(self.root, "init")
        self.tools = self.root / "dev-tools"
        git(base, "clone", str(self.origin), str(self.tools))
        git(self.tools, "checkout", "-b", "main")
        (self.tools / "installer").mkdir()
        (self.tools / runtime.MARKER).write_text("", encoding="utf-8")
        self.commit("first")
        git(self.tools, "push", "origin", "main")
        self.says = []

    def tearDown(self):
        self.dir.cleanup()

    def say(self, msg):
        self.says.append(msg)

    def commit(self, name):
        (self.tools / f"{name}.txt").write_text(name, encoding="utf-8")
        git(self.tools, "add", ".")
        git(self.tools, "commit", "-m", name)

    def push_new_main(self, name):
        self.commit(name)
        git(self.tools, "push", "origin", "main")

    def run_db(self, ended_at):
        db = self.tools / "talos" / "state" / "talos.db"
        db.parent.mkdir(parents=True)
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, agent TEXT, issue INTEGER, ended_at TEXT)")
        con.execute("INSERT INTO runs (agent, issue, ended_at) VALUES ('odysseus', 4084, ?)", (ended_at,))
        con.commit()
        con.close()

    def test_setup_creates_a_detached_worktree_of_origin_main(self):
        rt = runtime.setup(self.tools, self.root, say=self.say)
        self.assertEqual(rt, self.root / ".dev-tools-runtime")
        self.assertTrue((rt / "first.txt").exists())
        self.assertEqual(git(rt, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")      # detached
        self.assertIn("/.dev-tools-runtime/", (self.root / ".git" / "info" / "exclude").read_text(encoding="utf-8"))
        self.assertEqual(git(self.root, "status", "--porcelain", "--untracked-files=all"), "?? dev-tools/")

    def test_setup_twice_keeps_the_copy(self):
        runtime.setup(self.tools, self.root, say=self.say)
        runtime.setup(self.tools, self.root, say=self.say)
        exclude = (self.root / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertEqual(exclude.count("/.dev-tools-runtime/"), 1)

    def test_a_branch_in_the_checkout_does_not_change_the_copy(self):
        rt = runtime.setup(self.tools, self.root, say=self.say)
        git(self.tools, "checkout", "-b", "claude-issue-1")
        self.commit("half-done")
        self.assertFalse((rt / "half-done.txt").exists())

    def test_update_fast_forwards_to_origin_main(self):
        rt = runtime.setup(self.tools, self.root, say=self.say)
        self.push_new_main("second")
        self.assertTrue(runtime.update(self.root, self.tools, say=self.say))
        self.assertTrue((rt / "second.txt").exists())
        self.assertFalse(runtime.update(self.root, self.tools, say=self.say))

    def test_update_refuses_while_a_talos_run_is_active(self):
        rt = runtime.setup(self.tools, self.root, say=self.say)
        self.push_new_main("second")
        self.run_db(ended_at=None)
        with self.assertRaisesRegex(runtime.RuntimeCopyError, "odysseus #4084"):
            runtime.update(self.root, self.tools, say=self.say)
        self.assertFalse((rt / "second.txt").exists())
        self.assertTrue(runtime.update(self.root, self.tools, force=True, say=self.say))

    def test_ended_runs_do_not_block_the_update(self):
        runtime.setup(self.tools, self.root, say=self.say)
        self.run_db(ended_at="2026-10-22T10:00:00")
        self.assertEqual(runtime.active_runs(self.tools), [])

    def test_update_refuses_local_changes(self):
        rt = runtime.setup(self.tools, self.root, say=self.say)
        (rt / "first.txt").write_text("edited", encoding="utf-8")
        with self.assertRaisesRegex(runtime.RuntimeCopyError, "local changes"):
            runtime.update(self.root, self.tools, say=self.say)

    def test_update_without_a_copy(self):
        with self.assertRaisesRegex(runtime.RuntimeCopyError, "p4m runtime setup"):
            runtime.update(self.root, self.tools, say=self.say)

    def test_setup_refuses_a_main_without_the_runtime_change(self):
        git(self.tools, "rm", "-q", runtime.MARKER)
        git(self.tools, "commit", "-m", "old main")
        git(self.tools, "push", "origin", "main")
        with self.assertRaisesRegex(runtime.RuntimeCopyError, "isn't merged"):
            runtime.setup(self.tools, self.root, say=self.say)
        self.assertFalse(runtime.runtime_dir(self.root).exists())

    def test_unreadable_runs_table_blocks_the_update(self):
        db = self.tools / "talos" / "state" / "talos.db"
        db.parent.mkdir(parents=True)
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, agent TEXT)")
        con.commit()
        con.close()
        with self.assertRaisesRegex(runtime.RuntimeCopyError, "cannot read Talos runs"):
            runtime.active_runs(self.tools)


if __name__ == "__main__":
    unittest.main()
