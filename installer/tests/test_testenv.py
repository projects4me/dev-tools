"""Unit tests for the test-container path logic. Run: python -m unittest discover -s tests"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import testenv  # noqa: E402


class PathTest(unittest.TestCase):
    def test_docker_desktop_host_paths(self):
        self.assertEqual(testenv.host_path("/run/desktop/mnt/host/d/Claude/x"), "D:/Claude/x")
        self.assertEqual(testenv.host_path("D:\\Claude\\x"), "D:/Claude/x")
        self.assertEqual(testenv.host_path("/home/u/x"), "/home/u/x")

    def test_mount_source(self):
        info = {"Mounts": [{"Type": "volume", "Destination": "/var/www/html/vendor", "Name": "v"},
                           {"Type": "bind", "Destination": "/var/www/html",
                            "Source": "/run/desktop/mnt/host/d/Old/gaia"}]}
        self.assertEqual(testenv.mount_source(info, "/var/www/html"), "D:/Old/gaia")
        self.assertIsNone(testenv.mount_source(info, "/app"))

    def test_same_checkout_compares_equal_across_spellings(self):
        a, b = testenv.norm("D:\\Claude\\Pr4M\\gaia\\"), testenv.norm("/run/desktop/mnt/host/d/Claude/Pr4M/gaia")
        if testenv.IS_WINDOWS:
            self.assertEqual(a, b)
        self.assertNotEqual(testenv.norm("D:/Claude/Projects4Me/gaia"), testenv.norm("D:/Claude/Pr4M/gaia"))

    def test_container_names_are_stable_per_checkout(self):
        self.assertEqual(testenv._key("D:/x/gaia"), testenv._key("D:/x/gaia/"))
        self.assertNotEqual(testenv._key("D:/x/gaia"), testenv._key("D:/y/gaia"))



class RunTestsTest(unittest.TestCase):
    """run_tests stops a run that testem would never finish, and always cleans the container up."""

    def _box(self, script):
        box = testenv.PrometheusTestContainer(".", Path("."))
        box.kills = 0
        box.kill_tests = lambda: setattr(box, "kills", box.kills + 1)
        box.test_command = lambda flt=None: [sys.executable, "-c", script]
        return box

    def test_unmatched_filter_is_stopped_and_cleaned_up(self):
        script = "import time;print('No tests matched the filter',flush=True);time.sleep(60)"
        box = self._box(script)
        old = testenv.STUCK_GRACE
        testenv.STUCK_GRACE = 1
        try:
            self.assertEqual(box.run_tests("x"), 1)
        finally:
            testenv.STUCK_GRACE = old
        self.assertEqual(box.kills, 2)

    def test_timeout_is_stopped(self):
        box = self._box("import time;time.sleep(60)")
        self.assertEqual(box.run_tests(timeout=1), 1)

    def test_normal_run_returns_exit_code(self):
        box = self._box("print('# pass 1');raise SystemExit(0)")
        self.assertEqual(box.run_tests(), 0)


class BuildWaitTest(unittest.TestCase):
    """build() sees a rebuild that finished before it started, and kills stale watchers."""

    def _box(self, log, stale=()):
        box = testenv.PrometheusTestContainer(".", Path("."))
        box.log, box.calls = log, []
        box._build_log = lambda: box.log
        box._builder_running = lambda: True
        box.stale_builders = lambda: list(stale)
        box.run_docker = None
        return box

    def test_rebuild_finished_before_build_starts(self):
        # before=0 was taken ahead of the sync; the log already shows the rebuild when build() runs
        box = self._box("Build successful (6000ms)\n")
        ok, message = box.build(["app/a.js"], before=0, timeout=10, progress=0)
        self.assertTrue(ok, message)

    def test_no_new_build_times_out(self):
        box = self._box("Build successful (6000ms)\n")
        ok, message = box.build(["app/a.js"], before=1, timeout=1, progress=0)
        self.assertFalse(ok)
        self.assertIn("did not finish", message)

    def test_stale_watcher_is_killed(self):
        box = self._box("Build successful\n", stale=[2214])
        old = testenv._docker
        testenv._docker = lambda *a, **k: box.calls.append(a)
        try:
            self.assertEqual(box.kill_stale_builders(), [2214])
        finally:
            testenv._docker = old
        self.assertIn(("exec", box.name, "kill", "-9", "2214"), box.calls)

    def test_build_cleans_stale_watchers_first(self):
        box = self._box("Build successful\n", stale=[7])
        killed = []
        box.kill_stale_builders = lambda: killed.append(1) or []
        box.build([], before=1, progress=0)
        self.assertEqual(killed, [1])

    def test_running_check_uses_the_recorded_pid(self):
        # the watch build execs ember, which renames itself to plain `ember`: pgrep -f 'ember build' misses it
        box = testenv.PrometheusTestContainer(".", Path("."))
        sent = []
        old = testenv._docker
        testenv._docker = lambda *a, **k: sent.append(a) or subprocess.CompletedProcess(a, 0)
        try:
            self.assertTrue(box._builder_running())
        finally:
            testenv._docker = old
        script = sent[0][-1]
        self.assertIn(testenv.BUILD_PID, script)
        self.assertIn("kill -0", script)
        self.assertIn("/proc/$p/cmdline", script)

    def test_restart_stops_the_recorded_build(self):
        box = self._box("Build successful\nBuild successful\n")
        stopped = []
        box._stop_builder = lambda: stopped.append(1)
        box.kill_stale_builders = lambda: []
        box.build(["package.json"], before=1, timeout=5, progress=0)
        self.assertEqual(stopped, [1])


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("bash"), "needs /proc and bash")
class BuilderScriptTest(unittest.TestCase):
    """The shell checks against real processes: a live `ember`, a dead pid, a live non-ember pid."""

    def _run(self, script, pid_file):
        return subprocess.run(["sh", "-c", script.replace(testenv.BUILD_PID, pid_file)
                               .replace("pgrep -f '[e]mber build'", "false")]).returncode

    def test_recorded_builder(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = os.path.join(tmp, "build.pid")
            ember = subprocess.Popen(["bash", "-c", "exec -a ember sleep 30"])
            other = subprocess.Popen(["sleep", "30"])
            try:
                Path(pid_file).write_text(str(ember.pid))
                self.assertEqual(self._run(testenv.BUILDER_RUNNING_SCRIPT, pid_file), 0)
                Path(pid_file).write_text(str(other.pid))                  # pid reused by something else
                self.assertNotEqual(self._run(testenv.BUILDER_RUNNING_SCRIPT, pid_file), 0)
                Path(pid_file).write_text(str(ember.pid))
                self._run(testenv.STOP_BUILDER_SCRIPT.replace("sleep 1", "true"), pid_file)
                self.assertIsNotNone(ember.wait(timeout=5))
                self.assertFalse(os.path.exists(pid_file))
                self.assertNotEqual(self._run(testenv.BUILDER_RUNNING_SCRIPT, pid_file), 0)  # no pid file
            finally:
                for p in (ember, other):
                    p.kill()


if __name__ == "__main__":
    unittest.main()
