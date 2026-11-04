"""Workflow 'Run agent' steps on a record that isn't an issue (a milestone, ...): what such a run is given, where it
runs and how it ends. The run has no repo: Claude works in a scratch folder under the per-machine state with the
step's prompt, the agent's spec and a short context block about the record, and reaches the tracker through its Gaia
tools. No branch, commit, PR, ship session, CodeRabbit loop or test slot (record runs that change code are out of
scope).

Sessions are kept per agent and record (type + id) in the `records` table, so a step's `session: resume` continues
the record's latest session."""

import html
import re
from dataclasses import dataclass, field

from . import db
from .config import STATE_DIR

RECORDS_DIR = STATE_DIR / "records"     # <agent>/<record slug>: one scratch folder per agent and record
MILESTONE = "milestone"


@dataclass(frozen=True)
class Record:
    type: str
    id: str
    project: str = ""
    details: dict = field(default_factory=dict)     # what the tracker says about it (a milestone: get_milestone)

    @property
    def key(self):
        """Its key for sessions, runs and reservations: ids are only unique within a type."""
        return f"{self.type}:{self.id}"

    @property
    def slug(self):
        """Its key as a file name on any OS (scratch folder, run log)."""
        return re.sub(r"[^\w.-]", "_", f"{self.type}-{self.id}")


def read(tracker, entity_type, entity_id):
    """The step's record, with the tracker's details for the types Talos knows (a milestone). Raises TalosError when
    they can't be read."""
    if entity_type == MILESTONE:
        details = tracker.milestone(entity_id)
        return Record(entity_type, entity_id, str(details.get("project") or ""), details)
    return Record(entity_type, entity_id)


def context(record):
    """The block a fresh session's prompt starts with: the record and what the run has to work with."""
    lines = [f"This run is about the {record.type} with id {record.id}"
             + (f" in project {record.project}" if record.project else "") + "."]
    d = record.details
    if record.type == MILESTONE and d:
        counts = ", ".join(f"{n} {status}" for status, n in sorted((d.get("issuesByStatus") or {}).items()))
        lines.append(f"Milestone '{d.get('name')}': status {d.get('status') or 'not set'}, "
                     f"{d.get('startDate') or 'no start date'} to {d.get('endDate') or 'no end date'}, "
                     f"{d.get('issueCount') or 0} issues" + (f" ({counts})" if counts else "") + ".")
    lines.append("It is not an issue: there is no repo checkout, branch or PR. Your working folder is a scratch "
                 "folder; read and change records through the Gaia tools.")
    return "\n".join(lines)


def with_context(prompt, record):
    return f"{context(record)}\n\n{prompt}"


def folder(agent_name, record):
    """The record's scratch folder (created), under the per-machine state: never in a repo or the runtime copy."""
    path = RECORDS_DIR / agent_name / record.slug
    path.mkdir(parents=True, exist_ok=True)
    return path


def report(outcome, summary, comment):
    """(Gaia outcome, comment) for a record run that ended (not on a usage limit). done, needs_input and blocked pass
    on; escalate is needs_input (a record run is not re-run on a stronger model); ready_to_ship and
    awaiting_bot_review need a repo, so like any other end they are failed."""
    summary_html = f"<p>{html.escape(summary)}</p>" if summary else ""
    if outcome == "done":
        return "done", summary_html
    if outcome in ("needs_input", "blocked", "escalate"):
        return ("blocked" if outcome == "blocked" else "needs_input"), comment or summary_html
    unusable = "" if outcome == "failed" else (f"<p>The run ended with '{html.escape(outcome)}', which a record run "
                                               f"can't use.</p>")
    return "failed", unusable + summary_html + (f"<pre>{html.escape(comment)}</pre>" if comment else "")


# -- sessions (the records table) -------------------------------------------------------------------------------

def session(agent, record):
    """The record's stored session for the agent: session_id, session_cost_usd, last_ended_at; {} when none."""
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM records WHERE agent = ? AND record = ?", (agent, record.key)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def save_session(agent, record, session_id, session_cost_usd, last_ended_at):
    conn = db.connect()
    try:
        with conn:
            conn.execute("INSERT OR REPLACE INTO records (agent, record, session_id, session_cost_usd, last_ended_at) "
                         "VALUES (?, ?, ?, ?, ?)", (agent, record.key, session_id, session_cost_usd, last_ended_at))
    finally:
        conn.close()
