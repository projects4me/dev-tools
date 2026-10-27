"""Unit tests for the gaia test slots (fake psql). Run: python -m unittest discover -s tests"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import testslots  # noqa: E402


class FakePostgres:
    """Records psql statements and keeps a set of databases."""

    def __init__(self, dbs=(), schema="s1"):
        self.dbs = set(dbs)
        self.schema = schema
        self.statements = []

    def __call__(self, cmd):
        db, sql = cmd[cmd.index("-d") + 1], cmd[-1]
        self.statements.append(sql)
        out = ""
        if sql.startswith("SELECT 1 FROM pg_database"):
            out = "1" if sql.split("'")[1] in self.dbs else ""
        elif sql == testslots.SCHEMA_SQL:
            out = self.schema
        elif sql.startswith("DROP DATABASE"):
            self.dbs.discard(sql.split('"')[1])
        elif sql.startswith("CREATE DATABASE"):
            name, template = sql.split('"')[1], sql.split('"')[3]
            if template not in self.dbs:
                return subprocess.CompletedProcess(cmd, 1, "", f'template database "{template}" does not exist')
            self.dbs.add(name)
        return subprocess.CompletedProcess(cmd, 0, out, "")


class NamingTest(unittest.TestCase):
    def test_slot_databases_and_locks(self):
        self.assertEqual(testslots.db_name(3), "pr4m_test_3")
        self.assertEqual(testslots.db_name(None), "pr4m_test")
        self.assertEqual(testslots.lock_name(2), "pr4m_test_2.lock")
        self.assertEqual(testslots.lock_name(None), "pr4m_test.lock")

    def test_slot_count_from_settings(self):
        self.assertEqual(testslots.slot_count({}), testslots.DEFAULT_SLOTS)
        self.assertEqual(testslots.slot_count({"TEST_SLOTS": "4"}), 4)
        for bad in ("0", "x", "99"):
            with self.assertRaises(testslots.SlotError):
                testslots.slot_count({"TEST_SLOTS": bad})

    def test_check_slot(self):
        self.assertEqual(testslots.check_slot(2, 2), 2)
        with self.assertRaises(testslots.SlotError):
            testslots.check_slot(3, 2)

    def test_identifiers_are_never_interpolated_unchecked(self):
        pg = testslots.Postgres("pg", "u", FakePostgres())
        with self.assertRaises(testslots.SlotError):
            pg.drop("x'; DROP DATABASE pr4m; --")


class TemplateKeyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = Path(self.tmp.name)
        (self.src / "db" / "seeds").mkdir(parents=True)
        (self.src / "db" / "seeds" / "a.sql").write_bytes(b"insert 1;\n")
        script = self.src / testslots.SEED_SCRIPT
        script.parent.mkdir(parents=True)
        script.write_bytes(b"#!/bin/bash\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_key_changes_with_seeds_and_schema_but_not_line_endings(self):
        key = testslots.template_key(self.src, "s1")
        self.assertNotEqual(key, testslots.template_key(self.src, "s2"))
        (self.src / "db" / "seeds" / "a.sql").write_bytes(b"insert 1;\r\n")
        self.assertEqual(key, testslots.template_key(self.src, "s1"))
        (self.src / "db" / "seeds" / "b.sql").write_bytes(b"insert 2;\n")
        self.assertNotEqual(key, testslots.template_key(self.src, "s1"))


class SlotsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.src = root / "gaia"
        (self.src / "db" / "seeds").mkdir(parents=True)
        (self.src / "db" / "seeds" / "a.sql").write_text("insert;", encoding="utf-8")
        self.fake = FakePostgres(dbs={"pr4m"})
        self.seeded = []
        self.slots = testslots.Slots(testslots.Postgres("pg", "u", self.fake), root / "state", 2, self._seed)

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self, src, db, fixtures_out):
        self.seeded.append((src, db))
        self.fake.dbs.add(db)
        Path(fixtures_out).write_text('{"seeded": "%s"}' % db, encoding="utf-8")

    def test_template_is_built_once_and_sealed(self):
        self.assertTrue(self.slots.build_template(self.src))
        self.assertEqual(self.seeded, [(self.src, testslots.TEMPLATE_DB)])
        self.assertIn(f'ALTER DATABASE "{testslots.TEMPLATE_DB}" WITH IS_TEMPLATE true ALLOW_CONNECTIONS false',
                      self.fake.statements)
        self.assertTrue(self.slots.template_current(self.src)[0])
        self.assertFalse(self.slots.build_template(self.src))
        self.assertEqual(len(self.seeded), 1)

    def test_template_is_rebuilt_after_a_migration_or_a_seed_change(self):
        self.slots.build_template(self.src)
        self.fake.schema = "s2"                     # `p4m db migrate` changed pr4m
        self.assertFalse(self.slots.template_current(self.src)[0])
        self.assertTrue(self.slots.build_template(self.src))
        (self.src / "db" / "seeds" / "a.sql").write_text("insert 2;", encoding="utf-8")
        self.assertFalse(self.slots.template_current(self.src)[0])
        self.assertTrue(self.slots.build_template(self.src))
        self.assertEqual(len(self.seeded), 3)

    def test_failed_build_does_not_look_current(self):
        self.slots.build_template(self.src)

        def broken(src, db, fixtures_out):
            raise testslots.SlotError("seed failed")
        self.slots.seed = broken
        with self.assertRaises(testslots.SlotError):
            self.slots.build_template(self.src, force=True)
        self.assertFalse(self.slots.template_current(self.src)[0])

    def test_reset_clones_the_slot_from_the_template(self):
        with self.assertRaises(testslots.SlotError):
            self.slots.reset(1)                     # no template yet
        self.slots.build_template(self.src)
        self.fake.dbs.add("pr4m_test_1")            # an old copy with a run's data in it
        self.fake.statements.clear()
        self.slots.reset(1)
        self.assertEqual(self.fake.statements[-2:], [
            'DROP DATABASE IF EXISTS "pr4m_test_1" WITH (FORCE)',
            f'CREATE DATABASE "pr4m_test_1" TEMPLATE "{testslots.TEMPLATE_DB}" OWNER "u"'])
        self.assertEqual(self.slots.status(), [(1, "pr4m_test_1", True), (2, "pr4m_test_2", False)])
        with self.assertRaises(testslots.SlotError):
            self.slots.reset(3)

    def test_fixtures_of_the_template_go_into_the_checkout(self):
        self.slots.build_template(self.src)
        self.slots.write_fixtures(self.src)
        self.assertIn(testslots.TEMPLATE_DB, (self.src / testslots.FIXTURES).read_text(encoding="utf-8"))
        self.slots.fixtures_file.write_bytes(b"{\r\n}\r\n")      # as the harness writes it on Windows
        self.slots.write_fixtures(self.src)
        self.assertEqual((self.src / testslots.FIXTURES).read_bytes(), b"{\n}\n")


if __name__ == "__main__":
    unittest.main()
