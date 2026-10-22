"""Per agent and idea: the Claude session to resume, its cost so far, and a note for the next turn.

The agent's own memory, in the `ideas` table of state/talos.db (the conversation itself lives in Gaia).
"""

from .. import db

COLUMNS = ("session_id", "session_cost_usd", "note", "answered_upto", "last_ended_at")


def _key(idea):
    """Sim ideas have integer ids, Gaia's are GUIDs."""
    return idea if isinstance(idea, int) else str(idea)


def get(agent, idea):
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM ideas WHERE agent = ? AND idea = ?", (agent, _key(idea))).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def update(agent, idea, **fields):
    """Set the given columns (use "" to clear the note), creating the row if needed."""
    unknown = set(fields) - set(COLUMNS)
    if unknown:
        raise ValueError(f"unknown idea state fields: {', '.join(sorted(unknown))}")
    conn = db.connect()
    try:
        with conn:
            conn.execute("INSERT OR IGNORE INTO ideas (agent, idea) VALUES (?, ?)", (agent, _key(idea)))
            if fields:
                sets = ", ".join(f"{k} = ?" for k in fields)
                conn.execute(f"UPDATE ideas SET {sets} WHERE agent = ? AND idea = ?",
                             (*fields.values(), agent, _key(idea)))
    finally:
        conn.close()
