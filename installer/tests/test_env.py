"""p4m.env: the per-machine settings file. Run: python -m unittest discover -s tests"""

import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import p4m  # noqa: E402


class MigrateRefusalTest(unittest.TestCase):
    """A Talos run (P4M_WORKSPACE set) never migrates pr4m, the engineer's dev database."""

    def test_refused_in_a_run_workspace(self):
        why = p4m.migrate_refusal({"P4M_WORKSPACE": "/ws/.runs/issue-42"})
        self.assertIn("refusing to migrate pr4m", why)
        self.assertIn("/ws/.runs/issue-42", why)
        self.assertIsNone(p4m.migrate_refusal({}))
        self.assertIsNone(p4m.migrate_refusal({"P4M_WORKSPACE": ""}))

    def test_migrate_stops_before_touching_docker(self):
        with mock.patch.dict(os.environ, {"P4M_WORKSPACE": "/ws/.runs/issue-42"}), \
                mock.patch.object(p4m, "run") as run, mock.patch.object(p4m, "compose") as compose:
            with self.assertRaises(p4m.P4MError):
                p4m.migrate()
        run.assert_not_called()
        compose.assert_not_called()


class SettingsKeyTest(unittest.TestCase):
    """Gaia's settings encryption key is generated once per machine and never replaced."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = p4m.ENV_FILE
        p4m.ENV_FILE = Path(self.tmp.name) / "p4m.env"

    def tearDown(self):
        p4m.ENV_FILE = self.old
        self.tmp.cleanup()

    def test_new_file_gets_a_valid_key(self):
        key = p4m.load_env()["SETTINGS_ENCRYPTION_KEY"]
        self.assertEqual(len(base64.b64decode(key, validate=True)), 32)   # SettingsCipher's format
        self.assertEqual(p4m.load_env()["SETTINGS_ENCRYPTION_KEY"], key)  # stable across loads
        self.assertEqual(p4m.ENV_FILE.read_text(encoding="utf-8").count("SETTINGS_ENCRYPTION_KEY="), 1)

    def test_existing_file_gets_one_and_keeps_its_settings(self):
        p4m.ENV_FILE.write_text("P4M_DOMAIN=example.test\nANTHROPIC_API_KEY=abc", encoding="utf-8")
        env = p4m.load_env()
        self.assertEqual((env["P4M_DOMAIN"], env["ANTHROPIC_API_KEY"]), ("example.test", "abc"))
        self.assertEqual(len(base64.b64decode(env["SETTINGS_ENCRYPTION_KEY"])), 32)

    def test_existing_key_is_kept(self):
        p4m.ENV_FILE.write_text("P4M_DOMAIN=example.test\nSETTINGS_ENCRYPTION_KEY=mine\n", encoding="utf-8")
        self.assertEqual(p4m.load_env()["SETTINGS_ENCRYPTION_KEY"], "mine")

    def test_keys_differ_per_machine(self):
        self.assertNotEqual(p4m.settings_key_entry(), p4m.settings_key_entry())


class PrepareTestDbRetryTest(unittest.TestCase):
    def _run(self, codes):
        from unittest import mock
        results = [mock.Mock(returncode=c) for c in codes]
        with mock.patch.object(p4m, "is_running", return_value=True), \
                mock.patch.object(p4m, "fix_eol_in"), mock.patch.object(p4m, "find_bash", return_value="bash"), \
                mock.patch.object(p4m.time, "sleep"), mock.patch.object(p4m, "warn"), \
                mock.patch.object(p4m, "info"), \
                mock.patch.object(p4m, "run", side_effect=results) as run:
            p4m.prepare_test_db(Path("/tmp/gaia"))
        return run

    def test_retries_dll_init_failure(self):
        self.assertEqual(self._run([3221225794, -1073741502, 0]).call_count, 3)

    def test_gives_up_after_two_retries(self):
        with self.assertRaises(p4m.P4MError):
            self._run([3221225794, 3221225794, 3221225794])

    def test_other_failures_are_not_retried(self):
        with self.assertRaises(p4m.P4MError):
            self._run([1])



class RunBashRetryTest(unittest.TestCase):
    def test_run_bash_retries_dll_init_failure_and_returns_the_result(self):
        from unittest import mock
        results = [mock.Mock(returncode=3221225794), mock.Mock(returncode=7)]
        with mock.patch.object(p4m, "find_bash", return_value="bash"), mock.patch.object(p4m.time, "sleep"), \
                mock.patch.object(p4m, "warn"), mock.patch.object(p4m, "run", side_effect=results) as run:
            self.assertEqual(p4m.run_bash(["x.sh"], cwd=Path("/tmp")).returncode, 7)
        self.assertEqual(run.call_args_list[0].args[0], ["bash", "x.sh"])
        self.assertEqual(run.call_count, 2)

if __name__ == "__main__":
    unittest.main()
