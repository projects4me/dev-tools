"""Per agent and issue: the Claude session to resume, run count, escalation, and whether a run is in flight.

Backed by the `issues` table in state/talos.db; values use its column names.
"""

from . import db

COLUMNS = ("session_id", "session_cost_usd", "running", "runs", "escalated", "last_model",
           "live_session_id", "started_at", "last_ended_at")


def get(agent, issue):
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM issues WHERE agent = ? AND issue = ?", (agent, issue)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def update(agent, issue, **fields):
    """Set the given columns (None values are ignored), creating the row if needed."""
    fields = {k: v for k, v in fields.items() if v is not None}
    unknown = set(fields) - set(COLUMNS)
    if unknown:
        raise ValueError(f"unknown issue state fields: {', '.join(sorted(unknown))}")
    conn = db.connect()
    try:
        with conn:
            conn.execute("INSERT OR IGNORE INTO issues (agent, issue) VALUES (?, ?)", (agent, issue))
            if fields:
                sets = ", ".join(f"{k} = ?" for k in fields)
                conn.execute(f"UPDATE issues SET {sets} WHERE agent = ? AND issue = ?",
                             (*[int(v) if isinstance(v, bool) else v for v in fields.values()], agent, issue))
        return get(agent, issue)
    finally:
        conn.close()


def all_entries():
    """{(agent, issue): state} for every tracked issue."""
    conn = db.connect()
    try:
        return {(r["agent"], r["issue"]): dict(r) for r in conn.execute("SELECT * FROM issues")}
    finally:
        conn.close()


def interrupted(agent):
    """Issues whose run was in flight when Talos last stopped."""
    conn = db.connect()
    try:
        return [r["issue"] for r in conn.execute("SELECT issue FROM issues WHERE agent = ? AND running = 1",
                                                 (agent,))]
    finally:
        conn.close()
