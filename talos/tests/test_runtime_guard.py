"""The runtime copy guard: the PreToolUse hook's verdicts (runtime_hook.py) and the post-run check with a real git
repo in a temp dir (runtime_check.py)."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import runtime_check, runtime_hook, workspaces  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                           "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
                          cwd=cwd, capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL).stdout.strip()


class HookTest(unittest.TestCase):
    """A run workspace whose dev-tools/ and .dev-tools-runtime/ link to the runtime copy, and a gaia worktree."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        base = Path(self.dir.name)
        self.runtime = base / ".dev-tools-runtime"
        (self.runtime / "ai" / "lib").mkdir(parents=True)
        (self.runtime / "ai" / "state").mkdir()
        (self.runtime / "ai" / "lib" / "tests.py").write_text("", encoding="utf-8")
        self.run = base / ".runs" / "issue-1"
        (self.run / "gaia").mkdir(parents=True)
        for name in ("dev-tools", ".dev-tools-runtime"):
            workspaces.link(self.runtime, self.run / name)

    def tearDown(self):
        for name in ("dev-tools", ".dev-tools-runtime"):
            workspaces.unlink(self.run / name)
        self.dir.cleanup()

    def edit(self, path, tool="Edit"):
        return runtime_hook.verdict(tool, {runtime_hook.FILE_TOOLS[tool]: str(path)}, str(self.run), self.runtime)

    def bash(self, command, cwd=None):
        return runtime_hook.verdict("Bash", {"command": command}, str(cwd or self.run), self.runtime)

    def test_editing_tooling_through_the_dev_tools_link_is_denied(self):
        reason = self.edit(self.run / "dev-tools" / "ai" / "lib" / "tests.py")
        self.assertIn("runtime copy", reason)
        self.assertIn("dev-tools issue", reason)

    def test_relative_paths_new_files_and_every_file_tool_are_denied(self):
        self.assertTrue(self.edit("dev-tools/ai/lib/new.py", "Write"))
        self.assertTrue(self.edit(".dev-tools-runtime/ai/lib/tests.py", "MultiEdit"))
        self.assertTrue(self.edit(self.runtime / "talos" / "x.ipynb", "NotebookEdit"))

    def test_ai_state_and_the_issues_own_repo_are_allowed(self):
        self.assertIsNone(self.edit(self.run / "dev-tools" / "ai" / "state" / "ship-1.json", "Write"))
        self.assertIsNone(self.edit(self.run / "gaia" / "src" / "a.php"))
        self.assertIsNone(runtime_hook.verdict("Read", {"file_path": str(self.runtime / "ai")}, str(self.run),
                                               self.runtime))

    def test_git_writes_in_the_copy_are_denied(self):
        self.assertIn("git checkout", self.bash("cd dev-tools && git checkout -b claude-issue-4123"))
        self.assertTrue(self.bash("git -C .dev-tools-runtime rebase origin/main"))
        self.assertTrue(self.bash("git commit -m 'fix; again'", cwd=self.run / "dev-tools"))
        self.assertTrue(self.bash("cd gaia; cd ../dev-tools/ai; git stash"))
        self.assertTrue(self.bash("GIT_EDITOR=true dev-tools/odysseus/odysseus git -C dev-tools reset --hard"))
        self.assertTrue(self.bash("git -C dev-tools update-ref refs/heads/main HEAD"))

    def test_reads_and_git_writes_elsewhere_are_allowed(self):
        self.assertIsNone(self.bash("cd dev-tools && git status && git log --oneline -3 | head"))
        self.assertIsNone(self.bash("cd gaia && git checkout -b x && git commit -m y"))
        self.assertIsNone(self.bash("git -C gaia rebase origin/dev"))
        self.assertIsNone(self.bash("dev-tools/ai/ai start gaia 1"))

    def test_main_denies_with_exit_code_2_and_the_reason_on_stderr(self):
        data = {"tool_name": "Edit", "cwd": str(self.run),
                "tool_input": {"file_path": str(self.run / "dev-tools" / "ai" / "lib" / "tests.py")}}
        err = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(data))), mock.patch.object(sys, "stderr", err):
            self.assertEqual(runtime_hook.main(["hook", str(self.runtime)]), 2)
        self.assertIn("runtime copy", err.getvalue())
        data["tool_input"]["file_path"] = str(self.run / "gaia" / "a.php")
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(data))):
            self.assertEqual(runtime_hook.main(["hook", str(self.runtime)]), 0)

    def test_hook_runs_as_a_script(self):
        # what Claude Code runs: the hook file by path, outside Talos' package
        data = {"tool_name": "Write", "cwd": str(self.run),
                "tool_input": {"file_path": str(self.run / "dev-tools" / "ai" / "lib" / "tests.py")}}
        script = Path(runtime_hook.__file__)
        cp = subprocess.run([sys.executable, str(script), str(self.runtime)], input=json.dumps(data),
                            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(cp.returncode, 2, cp.stderr)
        self.assertIn("runtime copy", cp.stderr)

    def test_settings_name_the_copys_hook(self):
        hooks = runtime_hook.settings(Path("/w/.dev-tools-runtime"), python="/usr/bin/python3")["hooks"]
        entry = hooks["PreToolUse"][0]
        self.assertEqual(entry["matcher"], "Edit|Write|MultiEdit|NotebookEdit|Bash")
        self.assertEqual(entry["hooks"][0]["command"],
                         '"/usr/bin/python3" "/w/.dev-tools-runtime/talos/lib/runtime_hook.py" "/w/.dev-tools-runtime"')


class CheckTest(unittest.TestCase):
    """A detached runtime worktree with a tracked ai/lib/tests.py and ai/state/ ignored but for one tracked file."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        base = Path(self.dir.name)
        main = base / "dev-tools"
        (main / "ai" / "lib").mkdir(parents=True)
        (main / "ai" / "state").mkdir()
        (main / "ai" / "lib" / "tests.py").write_text("one\n", encoding="utf-8")
        (main / "ai" / "state" / "cost-baseline.md").write_text("base\n", encoding="utf-8")
        (main / ".gitignore").write_text("ai/state/*\n!ai/state/cost-baseline.md\n", encoding="utf-8")
        git(base, "init", str(main))
        git(main, "add", ".")
        git(main, "commit", "-m", "first")
        self.runtime = base / ".dev-tools-runtime"
        git(main, "worktree", "add", "--detach", str(self.runtime), "HEAD")
        self.main = main
        self.before = runtime_check.snapshot(self.runtime)

    def tearDown(self):
        self.dir.cleanup()

    def found(self):
        return runtime_check.changes(self.before, runtime_check.snapshot(self.runtime))

    def test_no_change(self):
        self.assertEqual(self.found(), [])
        self.assertEqual(self.before.branch, "")

    def test_only_ai_state_changed_is_allowed(self):
        (self.runtime / "ai" / "state" / "ship-1.json").write_text("{}", encoding="utf-8")
        (self.runtime / "ai" / "state" / "cost-baseline.md").write_text("changed\n", encoding="utf-8")
        self.assertEqual(self.found(), [])

    def test_file_edit_is_found_reported_and_restored(self):
        (self.runtime / "ai" / "lib" / "tests.py").write_text("two\n", encoding="utf-8")
        (self.runtime / "ai" / "lib" / "new.py").write_text("x\n", encoding="utf-8")
        found = self.found()
        self.assertEqual(found, ["?? ai/lib/new.py", "M ai/lib/tests.py"])
        report = runtime_check.report(self.runtime, self.before, found)
        self.assertIn("+two", report)
        self.assertIn("ai/lib/new.py", report)
        self.assertEqual(runtime_check.restore(self.runtime, self.before), [])
        self.assertEqual((self.runtime / "ai" / "lib" / "tests.py").read_text(encoding="utf-8"), "one\n")
        self.assertFalse((self.runtime / "ai" / "lib" / "new.py").exists())

    def test_staged_new_file_and_deletion_are_restored(self):
        (self.runtime / "ai" / "lib" / "added.py").write_text("x\n", encoding="utf-8")
        git(self.runtime, "add", "ai/lib/added.py")
        git(self.runtime, "rm", "-q", "ai/lib/tests.py")
        self.assertTrue(self.found())
        self.assertEqual(runtime_check.restore(self.runtime, self.before), [])
        self.assertTrue((self.runtime / "ai" / "lib" / "tests.py").exists())
        self.assertFalse((self.runtime / "ai" / "lib" / "added.py").exists())

    def test_head_moved_by_a_branch_and_commit_is_found_and_restored(self):
        git(self.runtime, "checkout", "-q", "-b", "claude-issue-4123")
        (self.runtime / "ai" / "lib" / "tests.py").write_text("two\n", encoding="utf-8")
        git(self.runtime, "commit", "-q", "-am", "fix in place")
        found = self.found()
        self.assertEqual(len(found), 2)
        self.assertIn("HEAD moved", found[0])
        self.assertIn("claude-issue-4123", found[1])
        self.assertIn("fix in place", runtime_check.report(self.runtime, self.before, found))
        self.assertEqual(runtime_check.restore(self.runtime, self.before), [])
        self.assertEqual(git(self.runtime, "rev-parse", "HEAD"), self.before.head)
        self.assertEqual(git(self.runtime, "branch", "--show-current"), "")
        self.assertEqual((self.runtime / "ai" / "lib" / "tests.py").read_text(encoding="utf-8"), "one\n")

    def test_changes_from_before_the_run_are_left_alone(self):
        (self.runtime / "ai" / "lib" / "old.py").write_text("before\n", encoding="utf-8")
        before = runtime_check.snapshot(self.runtime)
        (self.runtime / "ai" / "lib" / "tests.py").write_text("two\n", encoding="utf-8")
        self.assertEqual(runtime_check.changes(before, runtime_check.snapshot(self.runtime)), ["M ai/lib/tests.py"])
        self.assertEqual(runtime_check.restore(self.runtime, before), [])
        self.assertTrue((self.runtime / "ai" / "lib" / "old.py").exists())

    def test_a_second_edit_of_a_file_changed_before_the_run_is_found_and_left(self):
        (self.runtime / "ai" / "lib" / "tests.py").write_text("before\n", encoding="utf-8")
        before = runtime_check.snapshot(self.runtime)
        (self.runtime / "ai" / "lib" / "tests.py").write_text("during\n", encoding="utf-8")
        found = runtime_check.changes(before, runtime_check.snapshot(self.runtime))
        self.assertEqual(found, ["changed again ai/lib/tests.py (already changed before the run)"])
        self.assertEqual(runtime_check.restore(self.runtime, before), found)     # not this run's to undo
        self.assertEqual((self.runtime / "ai" / "lib" / "tests.py").read_text(encoding="utf-8"), "during\n")

    def test_moving_head_back_keeps_changes_from_before_the_run(self):
        (self.runtime / "ai" / "lib" / "tests.py").write_text("before\n", encoding="utf-8")
        before = runtime_check.snapshot(self.runtime)
        git(self.runtime, "checkout", "-q", "-b", "claude-issue-4123")
        found, left = runtime_check.after_run(self.runtime, before, Path(self.dir.name) / "run.log")
        self.assertIn("claude-issue-4123", found[0] + found[-1])
        self.assertEqual(left, [])
        self.assertEqual(git(self.runtime, "branch", "--show-current"), "")
        self.assertEqual((self.runtime / "ai" / "lib" / "tests.py").read_text(encoding="utf-8"), "before\n")
        self.assertIn("runtime copy changed", (Path(self.dir.name) / "run.log").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
