"""p4m.env: the per-machine settings file. Run: python -m unittest discover -s tests"""

import base64
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import p4m  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
