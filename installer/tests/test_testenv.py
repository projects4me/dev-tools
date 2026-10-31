"""Unit tests for the test-container path logic. Run: python -m unittest discover -s tests"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
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


class FakeDocker:
    """Records docker calls in place of testenv._docker; `outputs` maps a call to its stdout."""

    def __init__(self, outputs=None):
        self.calls, self.outputs = [], outputs or (lambda args: b"")
        self._old = testenv._docker

    def __enter__(self):
        testenv._docker = self
        return self

    def __exit__(self, *exc):
        testenv._docker = self._old

    def __call__(self, *args, check=True, input=None, text=True):
        self.calls.append((args, input))
        return subprocess.CompletedProcess(args, 0, self.outputs(args), "")


@unittest.skipUnless(shutil.which("git"), "needs git")
class GaiaSyncTest(unittest.TestCase):
    """The incremental copy into the gaia test container, and the copy of results back."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = Path(self.tmp.name) / "gaia"
        self.src.mkdir()
        subprocess.run(["git", "init", "-q", str(self.src)], check=True)
        (self.src / ".gitignore").write_text("vendor/\noutput/api-tester-*-report.json\n", encoding="utf-8")
        (self.src / "app").mkdir()
        (self.src / "app" / "a.php").write_text("<?php // a\n", encoding="utf-8")
        (self.src / "app" / "b.php").write_text("<?php // b\n", encoding="utf-8")
        (self.src / "vendor").mkdir()
        (self.src / "vendor" / "v.php").write_text("<?php\n", encoding="utf-8")
        self.box = testenv.GaiaTestContainer(self.src, Path(self.tmp.name) / "state")

    def tearDown(self):
        self.tmp.cleanup()

    def _copied(self, docker):
        """{path: content} of the files sent in tar -x calls."""
        import io
        import tarfile
        files = {}
        for args, data in docker.calls:
            if "tar" in args and "-x" in args:
                self.assertIn("www-data", args)              # owned like the image's source
                self.assertEqual(args[args.index("-C") + 1], "/var/www/html")
                with tarfile.open(fileobj=io.BytesIO(data)) as tar:
                    files.update({m.name: tar.extractfile(m).read() for m in tar.getmembers()})
        return files

    def test_first_sync_copies_everything_but_vendor(self):
        with FakeDocker() as docker:
            changed = self.box.sync()
        self.assertEqual(sorted(changed), [".gitignore", "app/a.php", "app/b.php"])
        self.assertEqual(sorted(self._copied(docker)), [".gitignore", "app/a.php", "app/b.php"])

    def test_later_syncs_copy_changes_and_delete_removed_files(self):
        with FakeDocker():
            self.box.sync()
        with FakeDocker() as docker:
            self.assertEqual(self.box.sync(), [])
        self.assertEqual(docker.calls, [])
        (self.src / "app" / "a.php").write_bytes(b"<?php // a, edited\n")
        (self.src / "app" / "b.php").unlink()
        with FakeDocker() as docker:
            changed = self.box.sync()
        self.assertEqual(changed, ["app/a.php", "app/b.php"])
        self.assertEqual(self._copied(docker), {"app/a.php": b"<?php // a, edited\n"})
        removals = [args for args, _ in docker.calls if "rm" in args]
        self.assertEqual(len(removals), 1)
        self.assertEqual(removals[0][-1], "/var/www/html/app/b.php")

    def test_fetch_copies_reports_back_and_skips_unsafe_paths(self):
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for name, body in (("output/api-tester-acl-report.json", b'{"ok":1}'), ("../escape.json", b"x")):
                info = tarfile.TarInfo(name)
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
        with FakeDocker(lambda args: buf.getvalue()) as docker:
            copied = self.box.fetch_reports()
        self.assertEqual(copied, ["output/api-tester-acl-report.json"])
        self.assertEqual((self.src / "output" / "api-tester-acl-report.json").read_bytes(), b'{"ok":1}')
        self.assertFalse((Path(self.tmp.name) / "escape.json").exists())
        self.assertIn(testenv.REPORTS, docker.calls[0][0][-1])

    def test_fetch_without_matches_copies_nothing(self):
        with FakeDocker():
            self.assertEqual(self.box.fetch("tools/api-tester/apis/backend.json"), [])


class GaiaContainerTest(unittest.TestCase):
    """The gaia test container: gaia-test's settings, no source bind mount, OPcache on, Xdebug off."""

    SHARED = {
        "Image": "sha256:abc",
        "Config": {"Image": "gaia-gaia-test", "Entrypoint": ["docker-php-entrypoint"], "Cmd": ["apache2-foreground"],
                   "WorkingDir": "/var/www/html", "User": "",
                   "Env": ["DB_NAME=pr4m_test", "XDEBUG_MODE=debug,develop"]},
        "NetworkSettings": {"Networks": {"backend": {}, "frontend": {}}},
        "Mounts": [
            {"Type": "bind", "Source": "D:\\Old\\gaia", "Destination": "/var/www/html", "RW": True},
            {"Type": "volume", "Name": "gaia_vendor", "Destination": "/var/www/html/vendor", "RW": True},
            {"Type": "bind", "Source": "/run/desktop/mnt/host/d/Old/gaia/docker/php/conf.d/custom.ini",
             "Destination": "/usr/local/etc/php/conf.d/zz-custom.ini", "RW": False},
        ],
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.box = testenv.GaiaTestContainer(Path(self.tmp.name) / "gaia", Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_args(self):
        args, image, command = self.box.run_args(self.SHARED)
        self.assertEqual(image, "gaia-gaia-test")
        volumes = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
        self.assertIn("gaia_vendor:/var/www/html/vendor", volumes)
        self.assertFalse([v for v in volumes if v.endswith(":/var/www/html")])     # no source bind mount
        ini = next(v for v in volumes if "zz-custom.ini" in v)
        self.assertTrue(ini.startswith(self.box.checkout.as_posix() + "/docker/php/conf.d/custom.ini:"))
        self.assertTrue(ini.endswith(":ro"))
        self.assertEqual(args[args.index("--network") + 1], "backend")
        self.assertEqual(command[-2:], ["docker-php-entrypoint", "apache2-foreground"])
        self.assertIn("zend_extension=opcache", command[1])
        self.assertIn("opcache.validate_timestamps=1", command[1])
        self.assertIn(testenv.GAIA_TEST_INI_FILE, command[1])

    def test_xdebug_is_off(self):
        env = self.box.env(self.SHARED)
        self.assertIn("XDEBUG_MODE=off", env)
        self.assertNotIn("XDEBUG_MODE=debug,develop", env)
        self.assertIn("DB_NAME=pr4m_test", env)

    def test_running_container_from_the_current_image_is_reused(self):
        own = {"Image": "sha256:abc", "State": {"Running": True}}
        old = testenv.inspect
        testenv.inspect = lambda name: self.SHARED if name == "gaia-test" else own
        try:
            with FakeDocker() as docker:
                self.assertFalse(self.box.ensure())
            self.assertEqual(docker.calls, [])
            own["Image"] = "sha256:new"                        # gaia-test was rebuilt: start over
            self.box._create = lambda shared: None
            with FakeDocker() as docker:
                self.assertTrue(self.box.ensure())
            self.assertEqual(docker.calls[0][0], ("rm", "-f", self.box.name))
        finally:
            testenv.inspect = old

    def test_name_is_per_checkout(self):
        other = testenv.GaiaTestContainer(Path(self.tmp.name) / "other", Path(self.tmp.name))
        self.assertTrue(self.box.name.startswith("p4m-test-gaia-"))
        self.assertNotEqual(self.box.name, other.name)


class GaiaSlotContainerTest(unittest.TestCase):
    """A test slot's gaia container: keyed by the slot, on the slot's database, re-filled per checkout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.old = testenv.inspect

    def tearDown(self):
        testenv.inspect = self.old
        self.tmp.cleanup()

    def _box(self, checkout, slot=2):
        return testenv.GaiaTestContainer(Path(self.tmp.name) / checkout, self.state, slot=slot,
                                         db_name=f"pr4m_test_{slot}")

    def test_name_and_label_are_per_slot_not_per_checkout(self):
        a, b = self._box("a"), self._box("b")
        self.assertEqual(a.name, "p4m-test-gaia-slot2")
        self.assertEqual(a.name, b.name)
        self.assertEqual(a.manifest_file, b.manifest_file)
        self.assertNotEqual(a.name, self._box("a", slot=1).name)
        args, _, _ = a.run_args(GaiaContainerTest.SHARED)
        self.assertEqual(args[args.index("--label") + 1], f"{testenv.LABEL}=slot2")

    def test_db_name_is_overridden(self):
        env = self._box("a").env(GaiaContainerTest.SHARED)
        self.assertIn("DB_NAME=pr4m_test_2", env)
        self.assertNotIn("DB_NAME=pr4m_test", env)
        self.assertIn("XDEBUG_MODE=off", env)

    def test_another_checkout_empties_the_slot_first(self):
        own = {"Image": "sha256:abc", "State": {"Running": True}}
        testenv.inspect = lambda name: GaiaContainerTest.SHARED if name == "gaia-test" else own
        a, b = self._box("a"), self._box("b")
        with FakeDocker():
            self.assertTrue(a.ensure())                  # nothing recorded yet: start from an empty tree
        a.manifest_file.parent.mkdir(parents=True, exist_ok=True)
        a.manifest_file.write_text("{}", encoding="utf-8")
        with FakeDocker() as docker:
            self.assertFalse(a.ensure())                 # same checkout: keep the copy, sync only changes
        self.assertEqual(docker.calls, [])
        with FakeDocker() as docker:
            self.assertTrue(b.ensure())                  # another checkout: empty it, copy everything
        self.assertIn("find /var/www/html", docker.calls[0][0][-1])
        self.assertNotIn(("rm", "-f", b.name), [c[0] for c in docker.calls])   # the container is kept
        self.assertFalse(b.manifest_file.exists())

    def test_clean_without_src_removes_slot_containers(self):
        with FakeDocker(lambda a: "p4m-test-gaia-slot1\n" if f"label={testenv.LABEL}" in a else "") as docker:
            self.assertEqual(testenv.clean(), ["p4m-test-gaia-slot1"])
        self.assertIn((("rm", "-f", "p4m-test-gaia-slot1"), None), docker.calls)

    def test_containers_under_a_folder_by_their_checkout_label(self):
        under = Path(tempfile.gettempdir()) / "ws" / ".runs"
        base = testenv.norm(under)
        listing = (f"p4m-test-prometheus-aa\t{base}/issue-7/prometheus\n"
                   f"p4m-test-gaia-bb\t{base}/issue-8/gaia\n"
                   f"p4m-test-prometheus-cc\t{testenv.norm(under.parent)}/prometheus\n"
                   f"p4m-test-gaia-slot1\tslot1\n"
                   f"p4m-test-other\t{base}-old/issue-9/gaia\n")
        with FakeDocker(lambda a: listing if a[0] == "ps" else "") as docker:
            found = testenv.containers(under)
        self.assertEqual(found, [("p4m-test-prometheus-aa", "issue-7/prometheus"), ("p4m-test-gaia-bb", "issue-8/gaia")])
        self.assertIn(f"label={testenv.LABEL}", docker.calls[0][0])

    def test_remove_raises_when_a_container_cant_be_removed(self):
        with FakeDocker() as docker:
            testenv.remove(["a", "b"])
        self.assertEqual([c[0] for c in docker.calls], [("rm", "-f", "a"), ("rm", "-f", "b")])
        failing = subprocess.CompletedProcess([], 1, "", "no daemon")
        with mock.patch.object(testenv.subprocess, "run", return_value=failing), \
                self.assertRaises(testenv.TestEnvError):
            testenv.remove(["a"])


class BindContainerChoiceTest(unittest.TestCase):
    """Which container a checkout's gaia commands (tests, migrations) run in."""

    def setUp(self):
        self.checkout = Path(tempfile.gettempdir()).resolve() / "ws-gaia"
        self.old = testenv.inspect, testenv._docker
        self.created = []

    def tearDown(self):
        testenv.inspect, testenv._docker = self.old

    def _shared(self, source):
        return {"Config": {"Image": "gaia", "Env": [], "Cmd": [], "Entrypoint": []},
                "State": {"Running": True}, "NetworkSettings": {"Networks": {"backend": {}}},
                "Mounts": [{"Type": "bind", "Source": str(source), "Destination": "/var/www/html"}]}

    def _resolve(self, source):
        shared = self._shared(source)
        testenv.inspect = lambda name: shared if name == "gaia-dev" else None
        testenv._docker = lambda *a, **k: self.created.append(a) or subprocess.CompletedProcess(a, 0, "", "")
        return testenv.BindTestContainer("gaia-dev", self.checkout, "/var/www/html").resolve()

    def test_shared_container_serving_the_checkout_is_used(self):
        self.assertEqual(self._resolve(self.checkout), ("gaia-dev", ""))
        self.assertEqual(self.created, [])

    def test_shared_container_with_another_checkout_gets_a_throwaway(self):
        name, note = self._resolve(self.checkout.parent / "other-gaia")
        self.assertTrue(name.startswith("p4m-test-gaia-dev-"))
        self.assertIn("started", note)
        run = next(c for c in self.created if c[0] == "run")
        self.assertIn(f"{self.checkout.as_posix()}:/var/www/html", run)


class CleanMatchTest(unittest.TestCase):
    """`p4m test clean --src <path>` finds the container started for that path, however --src is spelt."""

    def test_clean_filter_matches_the_gaia_container_label(self):
        with tempfile.TemporaryDirectory() as d:
            checkout = Path(d) / "baseline" / "gaia"
            box = testenv.GaiaTestContainer(checkout, Path(d))
            args, _, _ = box.run_args(GaiaContainerTest.SHARED)
            label = args[args.index("--label") + 1]
            spellings = [str(checkout), str(checkout) + os.sep, checkout.as_posix()]
            if testenv.IS_WINDOWS:
                spellings.append(str(checkout).upper())
            for src in spellings:
                with FakeDocker(lambda a: box.name + "\n" if f"label={label}" in a else "") as docker:
                    self.assertEqual(testenv.clean(src), [box.name], src)
                self.assertIn((("rm", "-f", box.name), None), docker.calls)


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
