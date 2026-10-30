"""An orchestrator's state in state/talos.db: paused or not, its failure streak, the issues it fed and what the
worker did with them since."""

from .. import db
from . import policy

COLUMNS = ("paused", "pause_reason", "paused_at", "fail_streak", "seen_upto", "last_sweep_at", "last_sweep")
SUCCESS_OUTCOMES = ("done", "awaiting_bot_review")      # a run that shipped or opened the PR


def get(agent):
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM orchestrators WHERE agent = ?", (agent,)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def update(agent, **fields):
    """Set the given columns (None clears one), creating the row if needed."""
    unknown = set(fields) - set(COLUMNS)
    if unknown:
        raise ValueError(f"unknown orchestrator state fields: {', '.join(sorted(unknown))}")
    conn = db.connect()
    try:
        with conn:
            conn.execute("INSERT OR IGNORE INTO orchestrators (agent) VALUES (?)", (agent,))
            if fields:
                sets = ", ".join(f"{k} = ?" for k in fields)
                conn.execute(f"UPDATE orchestrators SET {sets} WHERE agent = ?",
                             (*[int(v) if isinstance(v, bool) else v for v in fields.values()], agent))
        return get(agent)
    finally:
        conn.close()


def all_states():
    """{orchestrator: state} for every orchestrator that has run or been paused."""
    conn = db.connect()
    try:
        return {r["agent"]: dict(r) for r in conn.execute("SELECT * FROM orchestrators ORDER BY agent")}
    finally:
        conn.close()


def record_assignment(agent, issue, worker, at):
    conn = db.connect()
    try:
        with conn:
            conn.execute("INSERT OR IGNORE INTO orchestrated (agent, issue, worker) VALUES (?, ?, ?)",
                         (agent, issue, worker))
            conn.execute("UPDATE orchestrated SET worker = ?, assigned_at = ?, assignments = assignments + 1 "
                         "WHERE agent = ? AND issue = ?", (worker, at, agent, issue))
    finally:
        conn.close()


def events_since(agent, since):
    """What the worker did with the issues this orchestrator fed, after `since` and after each issue's latest
    assignment, oldest first: handbacks (reason, auto_retries) and successful runs (see policy.fail_streak)."""
    conn = db.connect()
    try:
        handbacks = conn.execute(
            "SELECT i.issue, i.handoff_at AS at, i.handoff_reason AS reason, i.auto_retries FROM orchestrated o "
            "JOIN issues i ON i.agent = o.worker AND i.issue = o.issue "
            "WHERE o.agent = ? AND i.handoff_at > ? AND i.handoff_at >= o.assigned_at", (agent, since)).fetchall()
        successes = conn.execute(
            f"SELECT r.issue, r.ended_at AS at FROM orchestrated o "
            f"JOIN runs r ON r.agent = o.worker AND r.issue = o.issue "
            f"WHERE o.agent = ? AND r.ended_at > ? AND r.ended_at >= o.assigned_at "
            f"AND r.outcome IN ({', '.join('?' for _ in SUCCESS_OUTCOMES)})", (agent, since, *SUCCESS_OUTCOMES)
        ).fetchall()
    finally:
        conn.close()
    events = [{**dict(r), "kind": policy.HANDBACK} for r in handbacks]
    events += [{**dict(r), "kind": policy.SUCCESS} for r in successes]
    return sorted(events, key=lambda e: e["at"])
