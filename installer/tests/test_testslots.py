"""Unit tests for the gaia test slots (fake psql). Run: python -m unittest discover -s tests"""

import contextlib
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
        self.assertEqual(testslots.template_db("abc123"), "pr4m_test_tpl_abc123")

    def test_slot_count_from_settings(self):
        self.assertEqual(testslots.slot_count({}), testslots.DEFAULT_SLOTS)
        self.assertEqual(testslots.slot_count({"TEST_SLOTS": "4"}), 4)
        for bad in ("0", "x", "99"):
            with self.assertRaises(testslots.SlotError):
                testslots.slot_count({"TEST_SLOTS": bad})

    def test_template_count_from_settings(self):
        self.assertEqual(testslots.template_count({}), testslots.DEFAULT_TEMPLATES)
        self.assertEqual(testslots.template_count({"TEST_TEMPLATES": "5"}), 5)
        with self.assertRaises(testslots.SlotError):
            testslots.template_count({"TEST_TEMPLATES": "0"})

    def test_check_slot(self):
        self.assertEqual(testslots.check_slot(2, 2), 2)
        with self.assertRaises(testslots.SlotError):
            testslots.check_slot(3, 2)

    def test_identifiers_are_never_interpolated_unchecked(self):
        pg = testslots.PsqlGateway("pg", "u", FakePostgres())
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


def make_checkout(path, seed):
    (path / "db" / "seeds").mkdir(parents=True)
    (path / "db" / "seeds" / "a.sql").write_text(seed, encoding="utf-8")
    return path


class SlotsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.src = make_checkout(self.root / "gaia", "insert;")
        self.fake = FakePostgres(dbs={"pr4m"})
        self.seeded = []
        self.locks = []
        self.slots = testslots.Slots(testslots.PsqlGateway("pg", "u", self.fake), self.root / "state", 2,
                                     self._seed, keep=2)

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self, src, db, fixtures_out):
        self.seeded.append((src, db))
        self.fake.dbs.add(db)
        Path(fixtures_out).write_text('{"seeded": "%s"}' % db, encoding="utf-8")

    @contextlib.contextmanager
    def _lock(self, name):
        self.locks.append(("take", name))
        yield
        self.locks.append(("release", name))

    def _template(self, src=None):
        return testslots.template_db(self.slots.wanted_key(src or self.src))

    def test_template_is_built_once_and_sealed(self):
        self.assertTrue(self.slots.build_template(self.src))
        tpl = self._template()
        self.assertTrue(tpl.startswith("pr4m_test_tpl_"))
        self.assertEqual(self.seeded, [(self.src, tpl)])
        self.assertIn(f'ALTER DATABASE "{tpl}" WITH IS_TEMPLATE true ALLOW_CONNECTIONS false', self.fake.statements)
        self.assertTrue(self.slots.template_current(self.src)[0])
        self.assertFalse(self.slots.build_template(self.src))
        self.assertEqual(len(self.seeded), 1)

    def test_a_migration_or_a_seed_change_needs_a_new_template(self):
        self.slots.build_template(self.src)
        self.fake.schema = "s2"                     # `p4m db migrate` changed pr4m
        self.assertFalse(self.slots.template_current(self.src)[0])
        self.assertTrue(self.slots.build_template(self.src))
        (self.src / "db" / "seeds" / "a.sql").write_text("insert 2;", encoding="utf-8")
        self.assertFalse(self.slots.template_current(self.src)[0])
        self.assertTrue(self.slots.build_template(self.src))
        self.assertEqual(len({db for _, db in self.seeded}), 3)

    def test_failed_build_does_not_look_current(self):
        self.slots.build_template(self.src)

        def broken(src, db, fixtures_out):
            raise testslots.SlotError("seed failed")
        self.slots.seed = broken
        with self.assertRaises(testslots.SlotError):
            self.slots.build_template(self.src, force=True)
        self.assertFalse(self.slots.template_current(self.src)[0])

    def test_failed_build_drops_its_half_built_template(self):
        def half(src, db, fixtures_out):
            self.fake.dbs.add(db)
            Path(fixtures_out).write_text("{}", encoding="utf-8")
            raise testslots.SlotError("seed failed")
        self.slots.seed = half
        with self.assertRaises(testslots.SlotError):
            self.slots.build_template(self.src)
        self.assertNotIn(self._template(), self.fake.dbs)
        self.assertFalse(self.slots.fixtures_file(self.slots.wanted_key(self.src)).exists())

    def test_the_single_template_of_before_is_dropped_on_a_build(self):
        self.fake.dbs.add(testslots.LEGACY_TEMPLATE_DB)
        self.slots.build_template(self.src)
        self.assertNotIn(testslots.LEGACY_TEMPLATE_DB, self.fake.dbs)

    def test_reset_clones_the_slot_from_its_template(self):
        key = self.slots.wanted_key(self.src)
        with self.assertRaises(testslots.SlotError):
            self.slots.reset(1, key)                # no template yet
        self.slots.build_template(self.src)
        self.fake.dbs.add("pr4m_test_1")            # an old copy with a run's data in it
        self.fake.statements.clear()
        self.slots.reset(1, key)
        self.assertEqual(self.fake.statements[-2:], [
            'DROP DATABASE IF EXISTS "pr4m_test_1" WITH (FORCE)',
            f'CREATE DATABASE "pr4m_test_1" TEMPLATE "{testslots.template_db(key)}" OWNER "u"'])
        self.assertEqual(self.slots.status(), [(1, "pr4m_test_1", True), (2, "pr4m_test_2", False)])
        with self.assertRaises(testslots.SlotError):
            self.slots.reset(3, key)

    def test_fixtures_of_the_template_go_into_the_checkout(self):
        self.slots.build_template(self.src)
        key = self.slots.wanted_key(self.src)
        self.slots.write_fixtures(self.src, key)
        self.assertIn(testslots.template_db(key), (self.src / testslots.FIXTURES).read_text(encoding="utf-8"))
        self.slots.fixtures_file(key).write_bytes(b"{\r\n}\r\n")      # as the harness writes it on Windows
        self.slots.write_fixtures(self.src, key)
        self.assertEqual((self.src / testslots.FIXTURES).read_bytes(), b"{\n}\n")

    def test_branch_and_baseline_each_keep_their_template(self):
        baseline = make_checkout(self.root / "baseline", "insert other;")
        for src in (self.src, baseline, self.src, baseline):
            self.slots.prepare(1, src, self._lock)
        self.assertEqual(len(self.seeded), 2)       # the second branch and baseline runs build nothing
        self.assertEqual(self.slots.prepare(2, baseline, self._lock), (self._template(baseline), False))
        self.assertIn(self._template(baseline), (baseline / testslots.FIXTURES).read_text(encoding="utf-8"))
        self.assertIn(f'CREATE DATABASE "pr4m_test_2" TEMPLATE "{self._template(baseline)}" OWNER "u"',
                      self.fake.statements)

    def test_least_recently_used_template_is_dropped_beyond_keep(self):
        a, b, c = self.src, make_checkout(self.root / "b", "b;"), make_checkout(self.root / "c", "c;")
        self.slots.prepare(1, a, self._lock)
        self.slots.prepare(1, b, self._lock)
        self.slots.prepare(2, a, self._lock)        # a is used again: b is now the least recently used
        self.slots.prepare(1, c, self._lock)
        self.assertEqual([k for k, *_ in self.slots.template_status()],
                         [self.slots.wanted_key(c), self.slots.wanted_key(a)])
        self.assertNotIn(self._template(b), self.fake.dbs)
        self.assertFalse(self.slots.fixtures_file(self.slots.wanted_key(b)).exists())
        self.assertIn(self._template(a), self.fake.dbs)
        self.slots.prepare(1, b, self._lock)        # b again: built anew, a dropped
        self.assertEqual(len(self.seeded), 4)
        self.assertNotIn(self._template(a), self.fake.dbs)

    def test_prepare_takes_the_slot_lock_then_the_templates_and_syncs_under_the_slot_lock(self):
        synced = []
        self.slots.prepare(2, self.src, self._lock, sync=lambda: synced.append(list(self.locks)))
        slot, tpl = testslots.lock_name(2), testslots.TEMPLATE_LOCK
        self.assertEqual(self.locks, [("take", slot), ("take", tpl), ("release", tpl), ("release", slot)])
        self.assertEqual(synced, [[("take", slot), ("take", tpl), ("release", tpl)]])

    def test_prepare_without_build_needs_the_template(self):
        with self.assertRaises(testslots.SlotError):
            self.slots.prepare(1, self.src, self._lock, build=False)
        self.slots.prepare(1, self.src, self._lock)
        self.assertFalse(self.slots.prepare(1, self.src, self._lock, build=False)[1])
        told = []
        self.assertTrue(self.slots.prepare(1, self.src, self._lock, force=True,
                                           on_build=lambda db, why: told.append(why))[1])
        self.assertEqual(told, ["--force"])
        self.assertEqual(len(self.seeded), 2)


if __name__ == "__main__":
    unittest.main()
