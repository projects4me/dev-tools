"""One row per Claude run in state/talos.db: what ran, how it ended, what it cost."""

from . import db

STATS = ("turns", "input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens",
         "peak_context_tokens")


def start(agent, issue, iteration, model, session_id, resumed, status_at_start, started_at):
    """Record a run as it starts (ended_at stays NULL while it runs). Returns the run id."""
    conn = db.connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO runs (agent, issue, iteration, model, session_id, resumed, status_at_start, started_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (agent, issue, iteration, model, session_id, int(resumed), status_at_start, started_at))
            return cur.lastrowid
    finally:
        conn.close()


def finish(run_id, ended_at, duration_s, outcome, summary, cost_usd, session_cost_usd, stats, log_file):
    conn = db.connect()
    try:
        with conn:
            conn.execute(
                "UPDATE runs SET ended_at = ?, duration_s = ?, outcome = ?, summary = ?, cost_usd = ?, "
                "session_cost_usd = ?, " + ", ".join(f"{k} = ?" for k in STATS) + ", log_file = ? WHERE id = ?",
                (ended_at, duration_s, outcome, summary, cost_usd, session_cost_usd,
                 *[stats.get(k) for k in STATS], log_file, run_id))
    finally:
        conn.close()


def close_unfinished(agent, issue, ended_at, outcome="interrupted"):
    """Mark runs left open by a Talos restart."""
    conn = db.connect()
    try:
        with conn:
            conn.execute("UPDATE runs SET ended_at = ?, outcome = ? WHERE agent = ? AND issue = ? AND ended_at IS NULL",
                         (ended_at, outcome, agent, issue))
    finally:
        conn.close()


def recent(limit=20, issue=None):
    conn = db.connect()
    try:
        if issue is None:
            rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = conn.execute("SELECT * FROM runs WHERE issue = ? ORDER BY id DESC LIMIT ?", (issue, limit))
        return [dict(r) for r in rows]
    finally:
        conn.close()


def incremental_cost(reported, previous_session_total, resumed):
    """This run's own cost. Claude reports a resumed session's cumulative cost, so subtract what the
    session had already cost; a fresh session's report is all this run's."""
    if reported is None:
        return None
    if resumed and previous_session_total and reported >= previous_session_total:
        return round(reported - previous_session_total, 6)
    return round(reported, 6)
