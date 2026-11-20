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


class MigrateBackfillTest(unittest.TestCase):
    """p4m db migrate runs search backfill after the migration, in the same container, as gaia's start.sh does."""

    def _migrate(self, backfill_rc, migrate_error=None):
        def fake_run(cmd, check=True, **_):
            if cmd[-2:] == ["search", "backfill"]:
                if isinstance(backfill_rc, Exception):
                    raise backfill_rc
                return mock.Mock(returncode=backfill_rc)
            if migrate_error:
                raise migrate_error
            return mock.Mock(returncode=0)
        with mock.patch.dict(os.environ, {"P4M_WORKSPACE": ""}), \
                mock.patch.object(p4m, "is_running", return_value=True), \
                mock.patch.object(p4m, "_test_container", return_value="p4m-gaia-tmp") as box, \
                mock.patch.object(p4m, "run", side_effect=fake_run) as run, \
                mock.patch.object(p4m, "warn") as warn:
            p4m.migrate()
        box.assert_called_once()
        return [c.args[0] for c in run.call_args_list], run.call_args_list, warn

    def test_backfill_follows_the_migration_in_the_same_container(self):
        cmds, calls, warn = self._migrate(0)
        self.assertEqual([c[-2:] for c in cmds], [["migration", "migrate"], ["search", "backfill"]])
        self.assertTrue(all("p4m-gaia-tmp" in c for c in cmds))
        self.assertFalse(calls[1].kwargs.get("check", True))
        warn.assert_not_called()

    def test_failed_backfill_warns_and_does_not_raise(self):
        _, _, warn = self._migrate(2)
        warn.assert_called_once()
        self.assertIn("exit 2", warn.call_args.args[0])

    def test_backfill_docker_error_warns_and_does_not_raise(self):
        _, _, warn = self._migrate(p4m.P4MError("docker went away"))
        self.assertIn("docker went away", warn.call_args.args[0])

    def test_failed_migration_raises_without_backfilling(self):
        with mock.patch.object(p4m, "backfill_search") as backfill, self.assertRaises(p4m.P4MError):
            self._migrate(0, migrate_error=p4m.P4MError("migrate failed"))
        backfill.assert_not_called()


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



class PrepareCheckoutTestDbTest(unittest.TestCase):
    def _run(self, migrate_error=None):
        from unittest import mock
        pg = mock.Mock()
        order = mock.Mock()
        order.attach_mock(pg, "pg")
        with mock.patch.object(p4m, "is_running", return_value=True),                 mock.patch.object(p4m, "test_slots", return_value=mock.Mock(pg=pg)),                 mock.patch.object(p4m, "migrate_scratch", side_effect=migrate_error) as migrate,                 mock.patch.object(p4m, "prepare_test_db") as prepare:
            try:
                p4m.prepare_checkout_test_db(Path("/tmp/gaia"))
            finally:
                self.pg, self.migrate, self.prepare = pg, migrate, prepare

    def test_schema_comes_from_a_scratch_copy_migrated_with_the_checkout(self):
        self._run()
        scratch = f"{p4m.testslots.SCRATCH_PREFIX}_unslotted"
        self.pg.copy.assert_called_once_with("pr4m", scratch)
        self.migrate.assert_called_once_with(Path("/tmp/gaia"), scratch)
        self.prepare.assert_called_once_with(Path("/tmp/gaia"), source_db=scratch)
        self.pg.drop.assert_called_with(scratch)

    def test_scratch_is_dropped_when_the_migration_fails(self):
        with self.assertRaises(RuntimeError):
            self._run(RuntimeError("boom"))
        self.prepare.assert_not_called()
        self.pg.drop.assert_called_with(f"{p4m.testslots.SCRATCH_PREFIX}_unslotted")


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
