"""Runner choice and the container runner's `docker run`: what crosses into the container and how."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import container, runner  # noqa: E402
from lib.config import TalosError  # noqa: E402
from test_policy import make_agent  # noqa: E402

SECRET = "sk-ant-oat-secret"


def container_agent(gaia_env="x.env", **settings):
    return make_agent(env={"EXTRA": "1"}, gaia_env=str(gaia_env),
                      runner={"type": "container", "image": "p4m-talos-runner", **settings})


def job(env=None):
    base = {"PATH": "/bin", "HOME": "/home/me", "CLAUDE_CODE_OAUTH_TOKEN": SECRET, "GAIA_EMAIL": "engineer@b",
            "EXTRA": "1", "P4M_TEST_SLOT": "2"}
    return runner.Job("odysseus", 42, "gaia", ["claude", "-p", "--model", "sonnet"], {**base, **(env or {})}, "go")


class ContainerRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [mock.patch.object(container, "LOCAL_DIR", self.tmp),
                        mock.patch.object(container, "CONTAINER_DIR", self.tmp / "containers"),
                        mock.patch.object(container, "FILES", {"ODYSSEUS_SSH_KEY": None, "GAIA_CA_FILE": None})]
        for p in self.patches:
            p.start()
        (self.tmp / "odysseus").mkdir()
        (self.tmp / "odysseus" / "odysseus.env").write_text("GH_TOKEN=odysseus\n", encoding="utf-8")
        (self.tmp / "gaia.env").write_text("GAIA_EMAIL=agent@b\n", encoding="utf-8")
        self.runs = []
        self.removed = []

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def execute(self, the_job=None, **settings):
        def fake_run(cmd, env, agent, number, prompt, cwd=None):
            self.runs.append((cmd, env, prompt))
            return {"session_id": "s1"}, Path("log.json")

        def fake_subprocess_run(cmd, **kwargs):
            self.removed.append(cmd)

        with mock.patch.object(container.runner, "run", fake_run), \
                mock.patch.object(container.subprocess, "run", fake_subprocess_run):
            agent = container_agent(gaia_env=self.tmp / "gaia.env", **settings)
            return container.ContainerRunner(agent).execute(the_job or job())

    def test_agent_runner_defaults_to_local(self):
        self.assertIsInstance(runner.for_agent(make_agent()), runner.LocalRunner)
        self.assertIsInstance(runner.for_agent(container_agent()), container.ContainerRunner)

    def test_secrets_go_by_name_not_on_the_command_line(self):
        self.execute()
        cmd, env, prompt = self.runs[0]
        self.assertNotIn(SECRET, " ".join(cmd))
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", cmd)
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], SECRET)       # the docker CLI copies it in
        self.assertEqual(prompt, "go")

    def test_only_the_runs_variables_cross_over(self):
        self.execute()
        cmd = self.runs[0][0]
        names = {cmd[i + 1] for i, a in enumerate(cmd) if a == "-e"}
        self.assertTrue({"GAIA_EMAIL", "EXTRA", "P4M_TEST_SLOT", "TALOS_REPO", "TALOS_ISSUE"} <= names)
        self.assertNotIn("PATH", names)
        self.assertNotIn("HOME", names)

    def test_claude_argv_follows_the_image(self):
        self.execute()
        cmd = self.runs[0][0]
        self.assertEqual(cmd[:4], ["docker", "run", "--rm", "-i"])
        at = cmd.index("p4m-talos-runner")
        self.assertEqual(cmd[at + 1:], ["--", "claude", "-p", "--model", "sonnet"])

    def test_claude_home_is_a_per_issue_bind_by_default(self):
        self.execute()
        cmd = self.runs[0][0]
        home = container.claude_home("odysseus", 42)
        self.assertTrue(home.is_dir())
        self.assertIn(f"{home}:{container.CLAUDE_HOME}", cmd)
        self.assertIn(f"talos-odysseus-42-work:{container.WORK}", cmd)

    def test_claude_home_volume_and_none(self):
        self.execute(claudeHome="volume")
        self.assertIn(f"talos-odysseus-42-claude:{container.CLAUDE_HOME}", self.runs[0][0])
        none = container.ContainerRunner(container_agent(claudeHome="none"))
        self.assertFalse(none.resumable)
        self.assertTrue(container.ContainerRunner(container_agent()).resumable)

    def test_unknown_claude_home_is_refused(self):
        with self.assertRaises(TalosError):
            container.ContainerRunner(container_agent(claudeHome="tmpfs"))

    def test_no_claude_credential_fails_the_run(self):
        with self.assertRaises(TalosError):
            self.execute(job({"CLAUDE_CODE_OAUTH_TOKEN": ""}))

    def test_workspace_repo_issues_are_refused(self):
        the_job = job()
        the_job.repo = "pr4m"
        with self.assertRaises(TalosError):
            self.execute(the_job)

    def test_credential_files_fill_the_environment(self):
        (self.tmp / "talos").mkdir()
        (self.tmp / "talos" / "container.env").write_text("# token\nANTHROPIC_API_KEY='sk-file'\n", encoding="utf-8")
        self.execute(job({"CLAUDE_CODE_OAUTH_TOKEN": ""}))
        self.assertEqual(self.runs[0][1]["ANTHROPIC_API_KEY"], "sk-file")

    def test_github_token_comes_from_odysseus_env_not_the_engineers(self):
        self.execute(job({"GH_TOKEN": "engineer"}))
        self.assertEqual(self.runs[0][1]["GH_TOKEN"], "odysseus")
        with self.assertRaises(TalosError):                           # never falls back to the engineer's
            self.execute(job({"GH_TOKEN": "engineer"}), envFiles=[])

    def test_gaia_sign_in_comes_from_the_agents_gaia_env_not_this_process(self):
        self.execute(job({"GAIA_PASSWORD": "engineer"}))
        cmd, env, _ = self.runs[0]
        self.assertEqual(env["GAIA_EMAIL"], "agent@b")
        self.assertNotIn("GAIA_PASSWORD", cmd)

    def test_a_missing_key_file_fails_the_run(self):
        with self.assertRaises(TalosError):
            self.execute(job({"ODYSSEUS_SSH_KEY": str(self.tmp / "missing")}))

    def test_a_left_over_container_is_removed_first(self):
        self.execute()
        self.assertEqual(self.removed, [["docker", "rm", "-f", "talos-odysseus-42"]])

    def test_key_files_are_mounted_read_only(self):
        key = self.tmp / "agent_odysseus"
        key.write_text("private", encoding="utf-8")
        key.with_name("agent_odysseus.pub").write_text("public", encoding="utf-8")
        self.execute(job({"ODYSSEUS_SSH_KEY": str(key)}))
        cmd, env, _ = self.runs[0]
        inside = f"{container.SECRETS}/odysseus_ssh_key"
        self.assertIn(f"{key}:{inside}:ro", cmd)
        self.assertIn(f"{key}.pub:{inside}.pub:ro", cmd)
        self.assertEqual(env["ODYSSEUS_SSH_KEY"], inside)

    def test_docker_host_and_test_docker_host(self):
        self.execute(dockerHost="tcp://builder:2376", testDockerHost="tcp://tests:2375",
                     addHosts=["api.local-projects4.me:host-gateway"])
        cmd, env, _ = self.runs[0]
        self.assertEqual(env["DOCKER_HOST"], "tcp://builder:2376")
        self.assertIn("DOCKER_HOST=tcp://tests:2375", cmd)
        self.assertNotIn("/var/run/docker.sock:/var/run/docker.sock", cmd)
        self.assertIn("api.local-projects4.me:host-gateway", cmd)

    def test_argv_names_the_containers_claude_and_mcp_config(self):
        r = container.ContainerRunner(container_agent())
        self.assertEqual(r.claude_bin("C:/bin/claude.exe"), "claude")
        self.assertEqual(r.mcp_config(Path("host/mcp.json")), container.MCP)
        self.assertIsNone(r.mcp_config(None))

    def test_read_env_file(self):
        path = self.tmp / "x.env"
        path.write_text('export A="1"\n\n# c\nB=two=2\n', encoding="utf-8")
        self.assertEqual(container.read_env_file(path), {"A": "1", "B": "two=2"})


if __name__ == "__main__":
    unittest.main()
