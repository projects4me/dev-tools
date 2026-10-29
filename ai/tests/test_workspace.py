"""ai in a Talos per-issue workspace (P4M_WORKSPACE): repos resolve there, start never checks out the base branch."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import apitester, common, doctor, workflow  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                           "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
                          cwd=cwd, capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL).stdout.strip()


class RepoDirTest(unittest.TestCase):
    def test_api_cases_append_to_the_worktree_catalog(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"P4M_WORKSPACE": tmp}):
            apis = Path(tmp) / "gaia" / "tools" / "api-tester" / "apis"
            apis.mkdir(parents=True)
            (apis / "client.json").write_text('{\n  "cases": [\n    {\n      "id": "a"\n    }\n  ]\n}\n',
                                              encoding="utf-8")
            cases = Path(tmp) / "cases.json"
            cases.write_text('[{"id": "b"}]', encoding="utf-8")
            with mock.patch.object(apitester, "say"):
                apitester.cmd_api_cases(mock.Mock(catalog="client", file=str(cases)))
            self.assertIn('"id": "b"', (apis / "client.json").read_text(encoding="utf-8"))

    def test_without_override_repos_are_in_the_workspace(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("P4M_WORKSPACE", None)
            self.assertEqual(common.repo_dir("gaia"), (common.WORKSPACE / "gaia").resolve())

    def test_override_moves_service_repos_but_not_the_workspace_repo(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"P4M_WORKSPACE": tmp}):
            self.assertEqual(common.repo_dir("gaia"), (Path(tmp) / "gaia").resolve())
            self.assertEqual(common.repo_dir("dev-tools"), (Path(tmp) / "dev-tools").resolve())
            self.assertEqual(common.repo_dir("pr4m"), common.WORKSPACE.resolve())
            self.assertEqual(common.LOCAL_DIR, common.WORKSPACE / "dev-tools")

    def test_a_repo_resolving_to_the_runtime_copy_is_refused(self):
        # dev-tools/ in another issue's workspace links to the runtime copy: `ai start dev-tools N` must not branch it
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"P4M_WORKSPACE": tmp}):
            runtime, link = Path(tmp) / common.RUNTIME_NAME, Path(tmp) / "dev-tools"
            runtime.mkdir()
            if os.name == "nt":
                import _winapi
                _winapi.CreateJunction(str(runtime), str(link))
            else:
                os.symlink(runtime, link, target_is_directory=True)
            try:
                with self.assertRaisesRegex(common.AiError, "runtime copy"):
                    common.repo_dir("dev-tools")
            finally:
                if os.name == "nt":
                    os.rmdir(link)       # removes the junction, not its target
                else:
                    os.unlink(link)


class StartInRunWorkspaceTest(unittest.TestCase):
    """gaia's main checkout sits on dev; the run's worktree must branch from origin/dev without checking out dev."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        base = Path(self.dir.name)
        origin = base / "gaia.git"
        git(base, "init", "--bare", str(origin))
        main = base / "ws" / "gaia"
        git(base, "clone", str(origin), str(main))
        git(main, "checkout", "-b", "dev")
        git(main, "commit", "--allow-empty", "-m", "first")
        git(main, "push", "origin", "dev")
        self.run_ws = base / "ws" / ".runs" / "issue-7"
        git(main, "worktree", "add", "--detach", str(self.run_ws / "gaia"), "origin/dev")
        self.main = main

    def tearDown(self):
        self.dir.cleanup()

    def start(self, issue):
        args = mock.Mock(issue=issue, repo="gaia")
        with mock.patch.dict(os.environ, {"P4M_WORKSPACE": str(self.run_ws)}), \
                mock.patch.object(workflow, "tracker_call"), mock.patch.object(workflow, "start_timer"), \
                mock.patch.object(workflow, "cmd_issue"), mock.patch.object(doctor, "preflight"), \
                mock.patch.object(workflow, "say"):
            workflow.cmd_start(args)

    def test_start_branches_from_origin_base_and_leaves_main_checkout_alone(self):
        self.start(7)
        self.assertEqual(git(self.run_ws / "gaia", "branch", "--show-current"), "claude-issue-7")
        self.assertEqual(git(self.main, "branch", "--show-current"), "dev")

    def test_start_again_keeps_the_branch_and_uncommitted_work(self):
        self.start(7)
        (self.run_ws / "gaia" / "wip.txt").write_text("wip", encoding="utf-8")
        self.start(7)
        self.assertEqual(git(self.run_ws / "gaia", "branch", "--show-current"), "claude-issue-7")
        self.assertTrue((self.run_ws / "gaia" / "wip.txt").exists())


if __name__ == "__main__":
    unittest.main()
