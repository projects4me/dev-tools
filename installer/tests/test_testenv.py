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
