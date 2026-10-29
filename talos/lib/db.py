"""Talos's SQLite store (state/talos.db): current state per issue, and one row per Claude run.

Kept plain (stdlib sqlite3, WAL) so other tools, e.g. a Projects4Me UI, can read it while Talos runs.
"""

import json
import sqlite3
import threading

from .config import STATE_DIR

DB_FILE = STATE_DIR / "talos.db"
LEGACY_SESSIONS = STATE_DIR / "sessions.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    agent               TEXT    NOT NULL,
    issue               INTEGER NOT NULL,
    session_id          TEXT,               -- Claude session to resume
    session_cost_usd    REAL    DEFAULT 0,  -- that session's cumulative cost as Claude reports it
    running             INTEGER DEFAULT 0,
    runs                INTEGER DEFAULT 0,  -- runs counted toward maxIterations (reset by `talos reset`)
    escalated           INTEGER DEFAULT 0,
    last_model          TEXT,
    live_session_id     TEXT,               -- session of the run in flight
    started_at          TEXT,               -- start of the latest run
    last_ended_at       TEXT,               -- end of the latest run (decides warm vs cold resume)
    PRIMARY KEY (agent, issue)
);
CREATE TABLE IF NOT EXISTS runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    agent               TEXT    NOT NULL,
    issue               INTEGER NOT NULL,
    iteration           INTEGER,
    model               TEXT,
    session_id          TEXT,
    resumed             INTEGER DEFAULT 0,
    status_at_start     TEXT,
    started_at          TEXT,
    ended_at            TEXT,               -- NULL while running
    duration_s          REAL,
    outcome             TEXT,               -- done, needs_input, blocked, escalate, failed
    summary             TEXT,
    cost_usd            REAL,               -- this run only
    session_cost_usd    REAL,               -- the session's cumulative cost after this run
    turns               INTEGER,            -- assistant messages in this run (main session)
    input_tokens        INTEGER,
    cache_read_tokens   INTEGER,
    cache_write_tokens  INTEGER,
    output_tokens       INTEGER,
    peak_context_tokens INTEGER,
    log_file            TEXT
);
CREATE TABLE IF NOT EXISTS ideas (
    agent               TEXT    NOT NULL,
    idea                INTEGER NOT NULL,
    session_id          TEXT,               -- Claude session to resume
    session_cost_usd    REAL    DEFAULT 0,  -- that session's cumulative cost as Claude reports it
    note                TEXT,               -- told to the agent at the start of its next turn
    answered_upto       INTEGER DEFAULT 0,  -- id of the last engineer message a turn has taken in
    last_ended_at       TEXT,
    PRIMARY KEY (agent, idea)
);
CREATE TABLE IF NOT EXISTS bot_reviews (
    agent               TEXT    NOT NULL,
    issue               INTEGER NOT NULL,
    repo                TEXT    NOT NULL,
    pr                  INTEGER NOT NULL,
    status              TEXT,               -- the issue's status when the wait began; a change drops the wait
    phase               TEXT    NOT NULL,   -- waiting (polling ai rabbit) or round (a bot-round run is queued)
    rounds              INTEGER DEFAULT 0,  -- bot rounds run so far (capped by botReviewRounds)
    deadline            TEXT,               -- when a still-pending CodeRabbit review is given up on
    PRIMARY KEY (agent, issue)
);
CREATE INDEX IF NOT EXISTS runs_by_issue ON runs (agent, issue);
CREATE INDEX IF NOT EXISTS runs_by_start ON runs (started_at);
"""

_init_lock = threading.Lock()
_initialized = set()


def connect(path=None):
    """A new connection (one per call keeps threads independent); creates and migrates on first use."""
    path = path or DB_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    with _init_lock:
        if str(path) not in _initialized:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            _initialized.add(str(path))
    return conn


def import_legacy():
    """Import state/sessions.json (the pre-SQLite store) and rename it. Called only when the daemon
    starts, so a Talos still running the old code never has its file moved underneath it."""
    if not LEGACY_SESSIONS.exists():
        return 0
    with open(LEGACY_SESSIONS, encoding="utf-8") as f:
        data = json.load(f)
    conn = connect()
    with conn:
        for key, e in data.items():
            agent, _, issue = key.rpartition(":")
            conn.execute(
                "INSERT OR REPLACE INTO issues (agent, issue, session_id, running, runs, escalated, last_model, "
                "live_session_id, started_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (agent, int(issue), e.get("sessionId"), int(bool(e.get("running"))), int(e.get("runs", 0)),
                 int(bool(e.get("escalated"))), e.get("lastModel") or e.get("model"),
                 e.get("liveSessionId"), e.get("startedAt")))
    conn.close()
    LEGACY_SESSIONS.replace(LEGACY_SESSIONS.with_suffix(".json.imported"))
    return len(data)
