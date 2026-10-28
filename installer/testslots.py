"""Test slots: a fixed set of Gaia test databases, so several suites can run at once.

The shared pr4m_test database lets one gaia suite run at a time: every api run re-seeds it. A slot k owns its own
database, pr4m_test_<k>, and its own Gaia test container (testenv.GaiaTestContainer with `slot=k`). A slot database
is not seeded on its own: it is cloned with `CREATE DATABASE ... TEMPLATE` from a template, which the gaia harness
migrates and seeds once. Resetting a slot is a drop and a clone (seconds, no re-seeding).

A template is built for a key: the schema of pr4m (`p4m db migrate`) and the checkout's seed sources (the harness
script and db/seeds). Each key has its own template, pr4m_test_tpl_<key>, so a branch and its baseline (another
checkout, maybe other seeds) each clone their own instead of rebuilding one in turn. At most `keep` templates are
kept (TEST_TEMPLATES); building one more drops the least recently used. The harness also writes the api-tester
fixtures; each template's copy is kept here and written into the checkout a slot is prepared for, as an unslotted
run does.

Each slot has its own lock (instead of the one pr4m_test lock); the templates share one, held while one is built,
cloned or dropped (Postgres refuses to clone a database someone is connected to). The order is always the slot's
lock, then the templates' (Slots.prepare).
"""

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

DB_PREFIX = "pr4m_test"
TEMPLATE_PREFIX = "pr4m_test_tpl"
TEMPLATE_LOCK = "pr4m_test_template.lock"   # the templates' lock (the name the one template's lock had)
LEGACY_TEMPLATE_DB = "pr4m_test_template"   # the one template before per-key ones: dropped on the next build
DEFAULT_SLOTS = 2
MAX_SLOTS = 16
DEFAULT_TEMPLATES = 3
MAX_TEMPLATES = 16
SEED_SCRIPT = "tools/api-tester/harness/prepare-test-db.sh"
SEED_DIRS = ("db/seeds",)
FIXTURES = "tools/api-tester/fixtures/default.json"
# Columns, types and defaults of pr4m's tables: changes whenever a migration does.
SCHEMA_SQL = ("SELECT md5(coalesce(string_agg(table_name || '.' || column_name || ':' || data_type || ':' "
              "|| coalesce(column_default, ''), ',' ORDER BY table_name, column_name), '')) "
              "FROM information_schema.columns WHERE table_schema = 'public'")


class SlotError(Exception):
    """A slot or a template could not be prepared."""


def _count_setting(cfg, name, default, most):
    raw = (cfg.get(name) or "").strip()
    if not raw:
        return default
    if not raw.isdigit() or not 1 <= int(raw) <= most:
        raise SlotError(f"{name} must be a number from 1 to {most}, not {raw!r}")
    return int(raw)


def slot_count(cfg):
    """Number of slots from the TEST_SLOTS setting (p4m.env), DEFAULT_SLOTS when unset."""
    return _count_setting(cfg, "TEST_SLOTS", DEFAULT_SLOTS, MAX_SLOTS)


def template_count(cfg):
    """How many templates to keep, from the TEST_TEMPLATES setting (p4m.env), DEFAULT_TEMPLATES when unset."""
    return _count_setting(cfg, "TEST_TEMPLATES", DEFAULT_TEMPLATES, MAX_TEMPLATES)


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


def template_db(key):
    """The template database built for `key`."""
    return f"{TEMPLATE_PREFIX}_{key}"


def seed_files(src):
    """The checkout's files a template is seeded from (relative paths, sorted)."""
    src = Path(src)
    files = [SEED_SCRIPT] if (src / SEED_SCRIPT).is_file() else []
    for d in SEED_DIRS:
        if (src / d).is_dir():
            files += sorted(p.relative_to(src).as_posix() for p in (src / d).rglob("*") if p.is_file())
    return files


def template_key(src, schema):
    """What a template depends on: pr4m's schema fingerprint and the checkout's seed sources (line endings
    normalised, so a CRLF checkout of the same files gives the same key)."""
    h = hashlib.sha256(f"schema:{schema}\n".encode("utf-8"))
    for rel in seed_files(src):
        h.update(f"file:{rel}\n".encode("utf-8"))
        h.update((Path(src) / rel).read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:16]


class PsqlGateway:
    """psql in the Postgres container, as the superuser-ish dev user: every statement the slots run."""

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
    """The templates and the slot databases. `pg` is a PsqlGateway, `seed(src, db, fixtures_out)` runs the gaia
    harness and `keep` is how many templates to keep."""

    def __init__(self, pg, state_dir, count, seed, source_db="pr4m", keep=DEFAULT_TEMPLATES):
        self.pg = pg
        self.state_dir = Path(state_dir)
        self.count = count
        self.seed = seed
        self.source_db = source_db
        self.keep = keep

    @property
    def state_file(self):
        return self.state_dir / "templates.json"

    def fixtures_file(self, key):
        return self.state_dir / f"fixtures-{key}.json"

    def templates(self):
        """{key: {"src", "built", "used", "seq"}} for the templates built; a higher `seq` was used more recently."""
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return state if isinstance(state, dict) else {}

    def _save(self, state):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")       # replace, so a reader never sees a half-written file
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def _touch(self, key):
        """Mark template `key` as the most recently used."""
        state = self.templates()
        state[key]["seq"] = max((e.get("seq", 0) for e in state.values()), default=0) + 1
        state[key]["used"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._save(state)

    def wanted_key(self, src):
        return template_key(src, self.pg.schema_fingerprint(self.source_db))

    def template_current(self, src, key=None):
        """(current, reason): whether there is a template for `src`'s seeds and pr4m's schema."""
        key = key or self.wanted_key(src)
        entry = self.templates().get(key)
        if not entry or not self.pg.exists(template_db(key)) or not self.fixtures_file(key).exists():
            return False, f"no template for key {key} (pr4m's schema or the seeds differ from the kept ones)"
        return True, f"{template_db(key)}, built {entry.get('built')} from {entry.get('src')}"

    def build_template(self, src, force=False, key=None):
        """Build `src`'s template unless it is current, then drop the least recently used ones beyond `keep`.
        Returns True if it was built. Run it under the templates' lock."""
        key = key or self.wanted_key(src)
        if not force and self.template_current(src, key)[0]:
            return False
        state = self.templates()
        state.pop(key, None)
        self._save(state)                               # a failed build must not look current
        db = template_db(key)
        self.pg.drop(db)
        self.pg.drop(LEGACY_TEMPLATE_DB)
        try:
            self.seed(Path(src), db, self.fixtures_file(key))
            self.pg.seal(db)
        except BaseException:
            self.pg.drop(db)                            # not in the registry, so evict would never drop it
            self.fixtures_file(key).unlink(missing_ok=True)
            raise
        state = self.templates()
        state[key] = {"src": str(src), "built": time.strftime("%Y-%m-%d %H:%M:%S")}
        self._save(state)
        self._touch(key)
        self.evict(keep_key=key)
        return True

    def evict(self, keep_key=None):
        """Drop the least recently used templates beyond `keep`, never `keep_key`. Returns the keys dropped.
        Run it under the templates' lock."""
        state = self.templates()
        dropped = []
        for key in sorted(state, key=lambda k: state[k].get("seq", 0)):
            if len(state) <= self.keep:
                break
            if key == keep_key:
                continue
            self.pg.drop(template_db(key))
            self.fixtures_file(key).unlink(missing_ok=True)
            del state[key]
            dropped.append(key)
        self._save(state)
        return dropped

    def reset(self, slot, key):
        """Slot `slot`'s database as a fresh copy of template `key`. Run it under the slot's and templates' locks."""
        if key not in self.templates() or not self.pg.exists(template_db(key)):
            raise SlotError(f"no template for key {key} yet - `p4m test slots setup` first")
        self.pg.clone(template_db(key), db_name(check_slot(slot, self.count)))
        self._touch(key)

    def write_fixtures(self, src, key):
        """Template `key`'s api-tester fixtures into checkout `src` (the harness writes them on a normal prepare),
        with LF line endings as committed (the harness's Python writes CRLF on Windows)."""
        target = Path(src) / FIXTURES
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.fixtures_file(key).read_bytes().replace(b"\r\n", b"\n"))

    def prepare(self, slot, src, lock, sync=None, build=True, force=False, on_build=None):
        """Slot `slot` ready for checkout `src`. Under the slot's lock, then the templates' lock: `src`'s template
        built if missing (with `build`; rebuilt with `force`), the slot cloned from it and its fixtures written into
        `src`. Then, still under the slot's lock, `sync()` (copying the checkout into the slot's container).
        `lock(name)` returns the context manager for lock file `name`; `on_build(db, reason)` is told before a
        build. Returns (template database, whether it was built)."""
        check_slot(slot, self.count)
        with lock(lock_name(slot)):
            with lock(TEMPLATE_LOCK):
                key = self.wanted_key(src)
                current, why = (False, "--force") if force else self.template_current(src, key)
                built = False
                if not current and (build or force):
                    if on_build:
                        on_build(template_db(key), why)
                    built = self.build_template(src, force=True, key=key)
                self.reset(slot, key)
                self.write_fixtures(src, key)   # under the lock: a build may be rewriting this key's fixtures
            if sync:
                sync()
        return template_db(key), built

    def status(self):
        """[(slot, database, exists)] for every slot."""
        return [(k, db_name(k), self.pg.exists(db_name(k))) for k in range(1, self.count + 1)]

    def template_status(self):
        """[(key, database, exists, entry)] for every template kept, the most recently used first."""
        state = self.templates()
        return [(k, template_db(k), self.pg.exists(template_db(k)), state[k])
                for k in sorted(state, key=lambda k: -state[k].get("seq", 0))]
