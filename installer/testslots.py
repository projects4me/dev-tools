"""Test slots: a fixed set of Gaia test databases, so several suites can run at once.

The shared pr4m_test database lets one gaia suite run at a time: every api run re-seeds it. A slot k owns its own
database, pr4m_test_<k>, and its own Gaia test container (testenv.GaiaTestContainer with `slot=k`). A slot database
is not seeded on its own: it is cloned with `CREATE DATABASE ... TEMPLATE` from pr4m_test_template, which the gaia
harness migrates and seeds once. Resetting a slot is a drop and a clone (seconds, no re-seeding).

The template is rebuilt when what it was built from changes: the schema of pr4m (`p4m db migrate`), or the
checkout's seed sources (the harness script and db/seeds). The harness also writes the api-tester fixtures; the
template's copy is kept here and written into the checkout a slot is prepared for, as an unslotted run does.

Each slot has its own lock (instead of the one pr4m_test lock); the template has one too, held while it is built or
cloned (Postgres refuses to clone a database someone is connected to).
"""

import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

DB_PREFIX = "pr4m_test"
TEMPLATE_DB = "pr4m_test_template"
DEFAULT_SLOTS = 2
MAX_SLOTS = 16
SEED_SCRIPT = "tools/api-tester/harness/prepare-test-db.sh"
SEED_DIRS = ("db/seeds",)
FIXTURES = "tools/api-tester/fixtures/default.json"
# Columns, types and defaults of pr4m's tables: changes whenever a migration does.
SCHEMA_SQL = ("SELECT md5(coalesce(string_agg(table_name || '.' || column_name || ':' || data_type || ':' "
              "|| coalesce(column_default, ''), ',' ORDER BY table_name, column_name), '')) "
              "FROM information_schema.columns WHERE table_schema = 'public'")


class SlotError(Exception):
    """A slot or the template could not be prepared."""


def slot_count(cfg):
    """Number of slots from the TEST_SLOTS setting (p4m.env), DEFAULT_SLOTS when unset."""
    raw = (cfg.get("TEST_SLOTS") or "").strip()
    if not raw:
        return DEFAULT_SLOTS
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_SLOTS:
        raise SlotError(f"TEST_SLOTS must be a number from 1 to {MAX_SLOTS}, not {raw!r}")
    return int(raw)


def check_slot(slot, count):
    if not 1 <= slot <= count:
        raise SlotError(f"slot {slot} does not exist: slots are 1..{count} (TEST_SLOTS in p4m.env)")
    return slot


def db_name(slot):
    """The database slot `slot` owns; the shared pr4m_test without a slot."""
    return f"{DB_PREFIX}_{slot}" if slot else DB_PREFIX


def lock_name(slot):
    """Lock file name for a slot's database (the shared pr4m_test's without a slot)."""
    return f"{db_name(slot)}.lock"


def seed_files(src):
    """The checkout's files the template is seeded from (relative paths, sorted)."""
    src = Path(src)
    files = [SEED_SCRIPT] if (src / SEED_SCRIPT).is_file() else []
    for d in SEED_DIRS:
        if (src / d).is_dir():
            files += sorted(p.relative_to(src).as_posix() for p in (src / d).rglob("*") if p.is_file())
    return files


def template_key(src, schema):
    """What the template depends on: pr4m's schema fingerprint and the checkout's seed sources (line endings
    normalised, so a CRLF checkout of the same files gives the same key)."""
    h = hashlib.sha256(f"schema:{schema}\n".encode("utf-8"))
    for rel in seed_files(src):
        h.update(f"file:{rel}\n".encode("utf-8"))
        h.update((Path(src) / rel).read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:16]


class Postgres:
    """psql in the Postgres container, as the superuser-ish dev user."""

    def __init__(self, container, user, runner=None):
        self.container = container
        self.user = user
        self.runner = runner or self._run

    @staticmethod
    def _run(cmd):
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")

    def sql(self, statement, db="postgres"):
        """Run one statement; returns its unaligned output. Raises SlotError on failure."""
        r = self.runner(["docker", "exec", self.container, "psql", "-U", self.user, "-d", db,
                         "-v", "ON_ERROR_STOP=1", "-Atc", statement])
        if r.returncode != 0:
            raise SlotError(f"psql failed ({statement[:60]}...): {(r.stderr or r.stdout or '').strip()[-400:]}")
        return (r.stdout or "").strip()

    def exists(self, db):
        return self.sql(f"SELECT 1 FROM pg_database WHERE datname = '{_ident(db)}'") == "1"

    def schema_fingerprint(self, db):
        return self.sql(SCHEMA_SQL, db)

    def drop(self, db):
        """Drop `db`, ending its sessions (a slot's Gaia container keeps connections open)."""
        if self.exists(db):
            self.sql(f'ALTER DATABASE "{_ident(db)}" WITH IS_TEMPLATE false')
            self.sql(f'DROP DATABASE IF EXISTS "{_ident(db)}" WITH (FORCE)')

    def clone(self, template, db):
        """`db` as a fresh copy of `template`."""
        self.drop(db)
        self.sql(f'CREATE DATABASE "{_ident(db)}" TEMPLATE "{_ident(template)}" OWNER "{_ident(self.user)}"')

    def seal(self, template):
        """Mark a built template: a template nobody can connect to (a connection would block cloning)."""
        self.sql(f'ALTER DATABASE "{_ident(template)}" WITH IS_TEMPLATE true ALLOW_CONNECTIONS false')


def _ident(name):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise SlotError(f"not a plain identifier: {name!r}")
    return name


class Slots:
    """The template and the slot databases. `seed(src, db, fixtures_out)` runs the gaia harness."""

    def __init__(self, pg, state_dir, count, seed, source_db="pr4m"):
        self.pg = pg
        self.state_dir = Path(state_dir)
        self.count = count
        self.seed = seed
        self.source_db = source_db

    @property
    def template_file(self):
        return self.state_dir / "template.json"

    @property
    def fixtures_file(self):
        return self.state_dir / "template-fixtures.json"

    def template_state(self):
        try:
            return json.loads(self.template_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def wanted_key(self, src):
        return template_key(src, self.pg.schema_fingerprint(self.source_db))

    def template_current(self, src):
        """(current, reason): whether the template matches `src`'s seeds and pr4m's schema."""
        state = self.template_state()
        if not state or not self.pg.exists(TEMPLATE_DB) or not self.fixtures_file.exists():
            return False, "no template yet"
        if state.get("key") != self.wanted_key(src):
            return False, f"migrations or seeds changed since it was built from {state.get('src')}"
        return True, f"built {state.get('built')} from {state.get('src')}"

    def build_template(self, src, force=False):
        """Rebuild the template from `src` unless it is current. Returns True if it was rebuilt.
        Run it under the template lock."""
        if not force and self.template_current(src)[0]:
            return False
        key = self.wanted_key(src)
        self.template_file.unlink(missing_ok=True)       # a failed build must not look current
        self.pg.drop(TEMPLATE_DB)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.seed(Path(src), TEMPLATE_DB, self.fixtures_file)
        self.pg.seal(TEMPLATE_DB)
        self.template_file.write_text(json.dumps({"key": key, "src": str(src),
                                                  "built": time.strftime("%Y-%m-%d %H:%M:%S")}), encoding="utf-8")
        return True

    def reset(self, slot):
        """Slot `slot`'s database as a fresh copy of the template. Run it under the template and slot locks."""
        if not self.pg.exists(TEMPLATE_DB):
            raise SlotError("no template database yet - `p4m test slots setup` first")
        self.pg.clone(TEMPLATE_DB, db_name(check_slot(slot, self.count)))

    def write_fixtures(self, src):
        """The template's api-tester fixtures into checkout `src` (the harness writes them on a normal prepare),
        with LF line endings as committed (the harness's Python writes CRLF on Windows)."""
        target = Path(src) / FIXTURES
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.fixtures_file.read_bytes().replace(b"\r\n", b"\n"))

    def status(self):
        """[(slot, database, exists)] for every slot."""
        return [(k, db_name(k), self.pg.exists(db_name(k))) for k in range(1, self.count + 1)]
