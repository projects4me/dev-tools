"""event_runs in state/talos.db: which (agent, event, resource) an events agent has taken, and how it ended."""

import json

from .. import db

QUEUED, RUNNING, DONE, FAILED, INTERRUPTED = "queued", "running", "done", "failed", "interrupted"


def claim(agent, key, envelope, received_at, path=None):
    """Take an event for a run. True when this is the first time the agent sees (event, resource), False for a
    duplicate, whatever became of the first (queued, ran, failed)."""
    event, resource_id, project_id = key
    conn = db.connect(path)
    try:
        with conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO event_runs (agent, event, resource_id, project_id, envelope, status, "
                "received_at) VALUES (?,?,?,?,?,?,?)",
                (agent, event, resource_id, project_id, json.dumps(envelope), QUEUED, received_at))
            return cur.rowcount == 1
    finally:
        conn.close()


def update(agent, key, path=None, **fields):
    """Set columns of one event's row (status, started_at, ended_at, outcome, summary, detail, run_id)."""
    if not fields:
        return
    conn = db.connect(path)
    try:
        with conn:
            conn.execute(f"UPDATE event_runs SET {', '.join(f'{k} = ?' for k in fields)} "
                         f"WHERE agent = ? AND event = ? AND resource_id = ?",
                         (*fields.values(), agent, key[0], key[1]))
    finally:
        conn.close()


def recover(agent, ended_at, path=None):
    """After a restart: runs left running are marked interrupted (not re-run: they may have acted already); the
    queued ones are returned as [(key, envelope)] to run now, since Hermes keeps no history to send them again."""
    conn = db.connect(path)
    try:
        with conn:
            conn.execute("UPDATE event_runs SET status = ?, ended_at = ?, outcome = ? WHERE agent = ? AND status = ?",
                         (INTERRUPTED, ended_at, INTERRUPTED, agent, RUNNING))
            rows = conn.execute("SELECT event, resource_id, project_id, envelope FROM event_runs "
                                "WHERE agent = ? AND status = ? ORDER BY received_at", (agent, QUEUED)).fetchall()
        return [((r["event"], r["resource_id"], r["project_id"]), json.loads(r["envelope"] or "{}")) for r in rows]
    finally:
        conn.close()


def recent(limit=20, path=None):
    """The latest event runs of every agent, newest first (for `talos status`)."""
    conn = db.connect(path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT agent, event, resource_id, project_id, status, received_at, started_at, ended_at, outcome, "
            "summary, detail FROM event_runs ORDER BY COALESCE(ended_at, started_at, received_at) DESC LIMIT ?",
            (limit,))]
    finally:
        conn.close()
