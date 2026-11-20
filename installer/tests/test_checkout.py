"""Unit tests for which checkout `p4m test` runs against. Run: python -m unittest discover -s tests"""

import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import p4m  # noqa: E402


class DefaultCheckoutTest(unittest.TestCase):
    def test_run_workspace_wins(self):
        self.assertEqual(p4m.default_checkout("gaia", {"P4M_WORKSPACE": "/ws/.runs/issue-1"}),
                         Path("/ws/.runs/issue-1") / "gaia")

    def test_workspace_root_without_run(self):
        self.assertEqual(p4m.default_checkout("gaia", {}), p4m.ROOT / "gaia")

    def test_empty_variable_is_unset(self):
        self.assertEqual(p4m.default_checkout("hermes", {"P4M_WORKSPACE": ""}), p4m.ROOT / "hermes")


class TestCheckoutTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.run_ws = Path(self.dir.name)
        (self.run_ws / "gaia" / ".git").mkdir(parents=True)

    def test_defaults_to_the_run_checkout(self):
        with mock.patch.dict("os.environ", {"P4M_WORKSPACE": str(self.run_ws)}), mock.patch.object(p4m, "info"):
            self.assertEqual(p4m._test_checkout(argparse.Namespace(src=None), "gaia"),
                             (self.run_ws / "gaia").resolve())

    def test_explicit_src_overrides(self):
        other = self.run_ws / "other"
        (other / ".git").mkdir(parents=True)
        with mock.patch.dict("os.environ", {"P4M_WORKSPACE": str(self.run_ws)}), mock.patch.object(p4m, "info"):
            self.assertEqual(p4m._test_checkout(argparse.Namespace(src=str(other)), "gaia"), other.resolve())

    def test_missing_checkout_fails_clearly(self):
        with mock.patch.dict("os.environ", {"P4M_WORKSPACE": str(self.run_ws)}), mock.patch.object(p4m, "info"):
            with self.assertRaises(p4m.P4MError):
                p4m._test_checkout(argparse.Namespace(src=None), "prometheus")


if __name__ == "__main__":
    unittest.main()
