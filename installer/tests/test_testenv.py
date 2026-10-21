"""Unit tests for the test-container path logic. Run: python -m unittest discover -s tests"""

import sys
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


if __name__ == "__main__":
    unittest.main()


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
