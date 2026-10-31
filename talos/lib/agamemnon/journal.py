"""Agamemnon's records in state/talos.db: its incidents, the action journal (one row per `aga` action, with the state
before and after and how to undo it) and the issues it holds until a later time."""

import json

from .. import db

ACTION_FIELDS = ("args", "state_before", "state_after", "undo")    # stored as JSON


def _action(row):
    if not row:
        return None
    a = dict(row)
    for f in ACTION_FIELDS:
        a[f] = json.loads(a[f]) if a.get(f) else None
    return a


def _query(sql, args=()):
    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def _write(sql, args=()):
    conn = db.connect()
    try:
        with conn:
            return conn.execute(sql, args).lastrowid
    finally:
        conn.close()


# -- incidents -----------------------------------------------------------------------------

def open_incident(issue, incident_type, at):
    """A new open incident; its id is what an Agamemnon session is given (AGA_INCIDENT)."""
    return _write("INSERT INTO aga_incidents (issue, type, opened_at) VALUES (?, ?, ?)",
                  (int(issue), incident_type, at))


def incident(incident_id):
    rows = _query("SELECT * FROM aga_incidents WHERE id = ?", (int(incident_id),))
    return rows[0] if rows else None


def close_incident(incident_id, at, note=None):
    _write("UPDATE aga_incidents SET closed_at = ?, note = ? WHERE id = ?", (at, note, int(incident_id)))


# -- actions -------------------------------------------------------------------------------

def record(incident_id, issue, verb, args, before, after, why, undo, undo_note, at):
    """One action; returns its id."""
    return _write(
        "INSERT INTO aga_actions (incident, issue, verb, args, state_before, state_after, why, undo, undo_note, at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (int(incident_id), int(issue), verb, json.dumps(args or {}), json.dumps(before), json.dumps(after), why,
         json.dumps(undo) if undo else None, undo_note, at))


def action(action_id):
    rows = _query("SELECT * FROM aga_actions WHERE id = ?", (int(action_id),))
    return _action(rows[0]) if rows else None


def actions(since=None, issue=None, incident_id=None):
    """Actions oldest first, optionally from `since` (an ISO time), for one issue or one incident."""
    where, args = [], []
    for column, value in (("at >= ?", since), ("issue = ?", issue), ("incident = ?", incident_id)):
        if value is not None:
            where.append(column)
            args.append(value)
    sql = "SELECT * FROM aga_actions" + (f" WHERE {' AND '.join(where)}" if where else "") + " ORDER BY id"
    return [_action(r) for r in _query(sql, args)]


def count(incident_id):
    return len(actions(incident_id=incident_id))


def count_verb_on(verb, day):
    """How many times the verb was taken on the day (YYYY-MM-DD), across incidents."""
    return _query("SELECT COUNT(*) AS n FROM aga_actions WHERE verb = ? AND substr(at, 1, 10) = ?",
                  (verb, day))[0]["n"]


def mark_undone(action_id, at, note):
    _write("UPDATE aga_actions SET undone_at = ?, undone_note = ? WHERE id = ?", (at, note, int(action_id)))


# -- holds ---------------------------------------------------------------------------------

def hold(issue, until, assignee):
    _write("INSERT OR REPLACE INTO aga_holds (issue, until, assignee, released_at) VALUES (?, ?, ?, NULL)",
           (int(issue), until, assignee))


def active_hold(issue):
    rows = _query("SELECT * FROM aga_holds WHERE issue = ? AND released_at IS NULL", (int(issue),))
    return rows[0] if rows else None


def due_holds(now):
    """Holds whose time has come and that are not released yet."""
    return _query("SELECT * FROM aga_holds WHERE released_at IS NULL AND until <= ? ORDER BY until", (now,))


def release_hold(issue, at):
    _write("UPDATE aga_holds SET released_at = ? WHERE issue = ?", (at, int(issue)))
