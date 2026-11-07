"""The held issues of a dependency gate, saved in state/talos.db so a restart keeps them (adt-4310)."""

import json

from . import db


def load(agent):
    """{issue: (dependencies, waiting_on or None)} saved for the agent."""
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM dependency_holds WHERE agent = ?", (agent,)).fetchall()
    finally:
        conn.close()
    return {r["issue"]: (tuple(json.loads(r["dependencies"] or "[]")),
                         tuple(json.loads(r["waiting_on"])) if r["waiting_on"] is not None else None)
            for r in rows}


def save(agent, issue, dependencies, waiting_on):
    conn = db.connect()
    try:
        with conn:
            conn.execute("INSERT OR REPLACE INTO dependency_holds (agent, issue, dependencies, waiting_on) "
                         "VALUES (?, ?, ?, ?)",
                         (agent, issue, json.dumps(list(dependencies)),
                          None if waiting_on is None else json.dumps(list(waiting_on))))
    finally:
        conn.close()


def remove(agent, issue):
    conn = db.connect()
    try:
        with conn:
            conn.execute("DELETE FROM dependency_holds WHERE agent = ? AND issue = ?", (agent, issue))
    finally:
        conn.close()
