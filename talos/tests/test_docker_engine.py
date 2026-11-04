"""Docker engine start/restart with stubbed processes: configured commands, each default method, the timeout, the
refused rootful case, the Windows tree kill and the p4m stack report."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import docker_engine as de  # noqa: E402
from lib.config import TalosError  # noqa: E402
from lib.registry import docker_settings  # noqa: E402

UP = {"daemon": True, "error": ""}
DOWN = {"daemon": False, "error": "cannot connect"}


class Clock:
    """A fake monotonic clock; sleeping advances it."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.calls = []
        self.spawned = []
        self.answers = {}           # tuple(argv) -> (code, out, err); unknown commands exit 1
        self.states = [DOWN, UP]    # docker_state.read() results in turn; the last one repeats
        for name, fn in (("_clock", self.clock), ("_sleep", self.clock.sleep), ("_run", self.fake_run), ("_spawn", self.spawned.append), ("_is_windows", lambda: False),
                         ("_which", lambda name: None), ("_is_file", lambda path: False)):
            p = mock.patch.object(de, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(de.docker_state, "read", side_effect=self.fake_read)
        p.start()
        self.addCleanup(p.stop)
        self.answers[("docker", "ps", "--format", "{{.Names}}")] = (0, "gaia-dev\npostgres-dev\n", "")

    def fake_run(self, argv, timeout=de.CHECK_TIMEOUT):
        self.calls.append(list(argv))
        return self.answers.get(tuple(argv), (1, "", "unknown"))

    def fake_read(self):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def ok(self, *argv):
        self.answers[argv] = (0, "", "")

    def test_configured_command_runs_first(self):
        self.ok("docker", "desktop", "--help")
        self.ok("sudo", "-n", "systemctl", "restart", "docker")
        r = de.restart(["sudo", "-n", "systemctl", "restart", "docker"], timeout=60)
        self.assertEqual((r["ok"], r["method"], r["error"]), (True, "command", ""))
        self.assertIn(["sudo", "-n", "systemctl", "restart", "docker"], self.calls)
        self.assertNotIn(["docker", "desktop", "--help"], self.calls)
        self.assertEqual(r["stackMissing"], ["p4m-proxy"])

    def test_configured_command_failure_is_reported(self):
        self.answers[("sudo", "-n", "systemctl", "start", "docker")] = (1, "", "sudo: a password is required")
        r = de.start(["sudo", "-n", "systemctl", "start", "docker"])
        self.assertFalse(r["ok"])
        self.assertIn("a password is required", r["error"])
        self.assertIsNone(r["stackMissing"])

    def test_start_when_already_running_does_nothing(self):
        self.states = [UP]
        r = de.start(["never", "run"])
        self.assertEqual((r["ok"], r["method"]), (True, "already-running"))
        self.assertNotIn(["never", "run"], self.calls)

    def test_docker_desktop_cli(self):
        self.ok("docker", "desktop", "--help")
        self.ok("docker", "desktop", "restart")
        r = de.restart()
        self.assertEqual((r["ok"], r["method"]), (True, "docker-desktop-cli"))
        self.assertIn(["docker", "desktop", "restart"], self.calls)

    def test_linux_systemd_user_unit(self):
        self.ok("systemctl", "--user", "cat", "docker-desktop")
        self.ok("systemctl", "--user", "start", "docker-desktop")
        r = de.start()
        self.assertEqual((r["ok"], r["method"]), (True, "systemd-user"))
        self.assertIn(["systemctl", "--user", "start", "docker-desktop"], self.calls)

    def test_rootful_docker_service_is_refused(self):
        self.ok("systemctl", "cat", "docker.service")
        r = de.restart()
        self.assertFalse(r["ok"])
        self.assertEqual(r["method"], "refused")
        self.assertIn("docker.restartCommand", r["error"])
        self.assertIn("sudoers", r["error"])
        self.assertFalse(any(c[:2] == ["systemctl", "restart"] for c in self.calls))

    def test_nothing_found_is_refused(self):
        r = de.start()
        self.assertEqual((r["ok"], r["method"]), (False, "refused"))
        self.assertIn("docker.startCommand", r["error"])

    def test_windows_start_launches_desktop_exe(self):
        exe = str(Path("D:/Apps/Docker/Docker") / de.DESKTOP_EXE)
        with mock.patch.object(de, "_is_windows", return_value=True), \
                mock.patch.object(de, "find_desktop_exe", return_value=exe):
            r = de.start()
        self.assertEqual((r["ok"], r["method"]), (True, "desktop-exe"))
        self.assertEqual(self.spawned, [[exe]])
        self.assertFalse(any(c[0] == "taskkill" for c in self.calls))

    def test_windows_restart_kills_the_process_tree_first(self):
        exe = str(Path("C:/Program Files/Docker/Docker") / de.DESKTOP_EXE)
        self.ok("taskkill", "/F", "/T", "/IM", de.DESKTOP_EXE)
        self.answers[("taskkill", "/F", "/T", "/IM", "com.docker.backend.exe")] = (de.TASKKILL_NOT_FOUND, "", "")
        with mock.patch.object(de, "_is_windows", return_value=True), \
                mock.patch.object(de, "find_desktop_exe", return_value=exe):
            r = de.restart()
        self.assertTrue(r["ok"], r["error"])
        kills = [c for c in self.calls if c[0] == "taskkill"]
        self.assertEqual(kills, [["taskkill", "/F", "/T", "/IM", image] for image in de.DESKTOP_PROCESSES])
        self.assertEqual(self.spawned, [[exe]])

    def test_windows_without_desktop_exe_is_refused(self):
        with mock.patch.object(de, "_is_windows", return_value=True):
            r = de.restart()
        self.assertEqual((r["ok"], r["method"]), (False, "refused"))
        self.assertIn("docker.restartCommand", r["error"])

    def test_find_desktop_exe_next_to_the_cli_or_under_program_files(self):
        cli = Path("E:/Tools/Docker/Docker/resources/bin/docker.exe")
        beside = cli.parents[2] / de.DESKTOP_EXE
        with mock.patch.object(de, "_which", return_value=str(cli)), \
                mock.patch.object(de, "_is_file", side_effect=lambda p: Path(p) == beside):
            self.assertEqual(de.find_desktop_exe(env={}), str(beside))
        program_files = Path("F:/Program Files")
        under = program_files / "Docker" / "Docker" / de.DESKTOP_EXE
        with mock.patch.object(de, "_is_file", side_effect=lambda p: Path(p) == under):
            self.assertEqual(de.find_desktop_exe(env={"ProgramFiles": str(program_files)}), str(under))
        self.assertIsNone(de.find_desktop_exe(env={}))

    def test_timeout(self):
        self.ok("systemctl", "--user", "cat", "docker-desktop")
        self.ok("systemctl", "--user", "restart", "docker-desktop")
        self.states = [DOWN]
        r = de.restart(timeout=10)
        self.assertFalse(r["ok"])
        self.assertIn("did not answer within 10s", r["error"])
        self.assertIn("cannot connect", r["error"])
        self.assertEqual(r["seconds"], 10.0)

    def test_describe(self):
        self.assertEqual(de.describe({"action": "start", "ok": True, "method": "command", "seconds": 4.0,
                                      "error": "", "stackMissing": ["p4m-proxy"]}),
                         "DOCKER  start ok (command, 4.0s); p4m stack not running: p4m-proxy")
        self.assertIn("failed", de.describe({"action": "restart", "ok": False, "method": "refused", "seconds": 0.0,
                                             "error": "set it", "stackMissing": None}))


class SettingsTest(unittest.TestCase):
    def test_defaults(self):
        s = docker_settings({})
        self.assertIsNone(s["docker_start_command"])
        self.assertIsNone(s["docker_restart_command"])
        self.assertEqual(s["docker_start_timeout_seconds"], 180)

    def test_commands_and_timeout(self):
        s = docker_settings({"docker": {"startCommand": ["sudo", "-n", "systemctl", "start", "docker"],
                                        "startTimeoutSeconds": 60}})
        self.assertEqual(s["docker_start_command"], ["sudo", "-n", "systemctl", "start", "docker"])
        self.assertEqual(s["docker_start_timeout_seconds"], 60)

    def test_a_command_string_is_rejected(self):
        with self.assertRaises(TalosError):
            docker_settings({"docker": {"restartCommand": "sudo systemctl restart docker"}})


if __name__ == "__main__":
    unittest.main()
