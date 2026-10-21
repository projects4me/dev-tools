"""Sim Gaia's data (state/sim.db, SQLite): the idea resources Gaia doesn't have yet.

One table per future Gaia model: idea, ideamessage, ideaplan, ideaissue. Plain data mapper; the rules about who
may change what, and the events each change publishes, are in ideas.py.
"""

import json
import sqlite3
import threading
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS idea (
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
    owner TEXT NOT NULL, agent TEXT NOT NULL, agent_state TEXT NOT NULL DEFAULT 'idle',
    created TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS ideamessage (
    id INTEGER PRIMARY KEY AUTOINCREMENT, idea_id INTEGER NOT NULL, role TEXT NOT NULL, author TEXT NOT NULL,
    body TEXT NOT NULL, steps TEXT, plan_id INTEGER, cost_usd REAL, created TEXT);
CREATE INDEX IF NOT EXISTS ideamessage_by_idea ON ideamessage(idea_id, id);
CREATE TABLE IF NOT EXISTS ideaplan (
    id INTEGER PRIMARY KEY AUTOINCREMENT, idea_id INTEGER NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL,
    status TEXT NOT NULL, error TEXT, created TEXT, updated TEXT, UNIQUE (idea_id, version));
CREATE TABLE IF NOT EXISTS ideascreen (
    id INTEGER PRIMARY KEY AUTOINCREMENT, idea_id INTEGER NOT NULL, key TEXT NOT NULL, version INTEGER NOT NULL,
    title TEXT, width INTEGER, height INTEGER, png BLOB NOT NULL, created TEXT, UNIQUE (idea_id, key, version));
CREATE TABLE IF NOT EXISTS ideaissue (
    id INTEGER PRIMARY KEY AUTOINCREMENT, idea_id INTEGER NOT NULL, plan_id INTEGER NOT NULL, key TEXT NOT NULL,
    number INTEGER NOT NULL, project_code TEXT, subject TEXT, url TEXT, created TEXT, UNIQUE (plan_id, key));
"""


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript(SCHEMA)
            columns = {r["name"] for r in self.db.execute("PRAGMA table_info(ideamessage)")}
            if "screens" not in columns:          # added with UI screens
                self.db.execute("ALTER TABLE ideamessage ADD COLUMN screens TEXT")
                self.db.commit()

    def close(self):
        self.db.close()

    def rows(self, sql, params=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, params)]

    def one(self, sql, params=()):
        rows = self.rows(sql, params)
        return rows[0] if rows else None

    def write(self, sql, params=()):
        with self.lock, self.db:
            return self.db.execute(sql, params).lastrowid

    def transaction(self):
        """`with store.transaction():` for multi-statement writes (holds the lock, commits once)."""
        store = self

        class _Tx:
            def __enter__(self):
                store.lock.acquire()
                store.db.__enter__()
                return store.db

            def __exit__(self, *exc):
                try:
                    return store.db.__exit__(*exc)
                finally:
                    store.lock.release()
        return _Tx()

    # -- idea ---------------------------------------------------------------------------------------------------

    def insert_idea(self, title, owner, agent):
        t = now()
        return self.write("INSERT INTO idea (title, owner, agent, created, updated) VALUES (?, ?, ?, ?, ?)",
                          (title, owner, agent, t, t))

    def idea(self, idea_id):
        return self.one("SELECT * FROM idea WHERE id = ?", (idea_id,))

    def ideas_for(self, user):
        """Ideas the user owns or is the agent of (not archived), latest first."""
        return self.rows("SELECT * FROM idea WHERE (owner = ? OR agent = ?) AND status != 'archived' "
                         "ORDER BY updated DESC", (user, user))

    def update_idea(self, idea_id, **fields):
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.write(f"UPDATE idea SET {sets}, updated = ? WHERE id = ?", (*fields.values(), now(), idea_id))

    def touch(self, idea_id):
        self.write("UPDATE idea SET updated = ? WHERE id = ?", (now(), idea_id))

    # -- ideamessage --------------------------------------------------------------------------------------------

    def insert_message(self, idea_id, role, author, body, steps=None, plan_id=None, cost=None, screens=None):
        mid = self.write("INSERT INTO ideamessage (idea_id, role, author, body, steps, plan_id, cost_usd, screens, "
                         "created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (idea_id, role, author, body, json.dumps(steps) if steps else None, plan_id, cost,
                          json.dumps(screens) if screens else None, now()))
        self.touch(idea_id)
        return mid

    def messages(self, idea_id):
        rows = self.rows("SELECT * FROM ideamessage WHERE idea_id = ? ORDER BY id", (idea_id,))
        for r in rows:
            r["steps"] = json.loads(r["steps"]) if r["steps"] else []
            r["screens"] = json.loads(r["screens"]) if r.get("screens") else []
        return rows

    def cost(self, idea_id):
        return self.one("SELECT coalesce(sum(cost_usd), 0) AS c FROM ideamessage WHERE idea_id = ?", (idea_id,))["c"]

    # -- ideaplan -----------------------------------------------------------------------------------------------

    def insert_plan(self, idea_id, body):
        """A new version; an earlier proposed or failed version is superseded. Returns (id, version)."""
        with self.transaction() as db:
            row = db.execute("SELECT max(version) AS v FROM ideaplan WHERE idea_id = ?", (idea_id,)).fetchone()
            version = (row["v"] or 0) + 1
            db.execute("UPDATE ideaplan SET status = 'superseded', updated = ? WHERE idea_id = ? AND "
                       "status IN ('proposed', 'failed')", (now(), idea_id))
            pid = db.execute("INSERT INTO ideaplan (idea_id, version, body, status, created, updated) "
                             "VALUES (?, ?, ?, 'proposed', ?, ?)",
                             (idea_id, version, json.dumps(body), now(), now())).lastrowid
        self.touch(idea_id)
        return pid, version

    def plan(self, plan_id):
        row = self.one("SELECT * FROM ideaplan WHERE id = ?", (plan_id,))
        if row:
            row["body"] = json.loads(row["body"])
        return row

    def plans(self, idea_id):
        rows = self.rows("SELECT * FROM ideaplan WHERE idea_id = ? ORDER BY version", (idea_id,))
        for r in rows:
            r["body"] = json.loads(r["body"])
        return rows

    def set_plan_status(self, plan_id, status, error=None, only_from=None):
        """Change a plan's status; with only_from, only if it is currently one of those (returns whether it did)."""
        sql = "UPDATE ideaplan SET status = ?, error = ?, updated = ? WHERE id = ?"
        params = [status, error, now(), plan_id]
        if only_from:
            sql += f" AND status IN ({', '.join('?' for _ in only_from)})"
            params += list(only_from)
        with self.lock, self.db:
            return self.db.execute(sql, params).rowcount == 1

    # -- ideascreen ---------------------------------------------------------------------------------------------

    def insert_screen(self, idea_id, key, title, width, height, png):
        """A new version of the idea's screen `key`; returns (id, version)."""
        with self.transaction() as db:
            row = db.execute("SELECT max(version) AS v FROM ideascreen WHERE idea_id = ? AND key = ?",
                             (idea_id, key)).fetchone()
            version = (row["v"] or 0) + 1
            sid = db.execute("INSERT INTO ideascreen (idea_id, key, version, title, width, height, png, created) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (idea_id, key, version, title, width, height, png, now())).lastrowid
        self.touch(idea_id)
        return sid, version

    def screens(self, idea_id):
        """Every version of the idea's screens, without the images."""
        return self.rows("SELECT id, idea_id, key, version, title, width, height, created FROM ideascreen "
                         "WHERE idea_id = ? ORDER BY key, version", (idea_id,))

    def screen(self, screen_id):
        return self.one("SELECT * FROM ideascreen WHERE id = ?", (screen_id,))

    # -- ideaissue ----------------------------------------------------------------------------------------------

    def insert_issue(self, idea_id, plan_id, key, number, project_code, subject, url):
        iid = self.write("INSERT INTO ideaissue (idea_id, plan_id, key, number, project_code, subject, url, created) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (idea_id, plan_id, key, number, project_code, subject, url, now()))
        self.touch(idea_id)
        return iid

    def issues(self, idea_id):
        return self.rows("SELECT * FROM ideaissue WHERE idea_id = ? ORDER BY id", (idea_id,))
