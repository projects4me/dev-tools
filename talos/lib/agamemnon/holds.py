"""Giving held issues back (`aga hold-until-tomorrow`): on each orchestrator sweep, a hold whose time has come is
released and the issue assigned to the worker, as long as it is still assigned to Agamemnon. When someone moved it in
the meantime, the hold is only released: their choice stands."""

from ..config import TalosError, log
from . import journal


def release_due(tracker, worker_id, my_id, now, agent_name, dry_run=False):
    """Release the due holds; a short note of what was done ("" when nothing was due)."""
    notes = []
    for hold in journal.due_holds(now):
        number = hold["issue"]
        try:
            issue = tracker.issue(number)
            if dry_run:
                notes.append(f"#{number} would be given back (dry run)")
                continue
            if str(issue.get("assignee") or "") == str(my_id):
                tracker.assign(number, worker_id)
                notes.append(f"#{number} given back to the worker")
            else:
                notes.append(f"#{number} hold ended (reassigned meanwhile)")
            journal.release_hold(number, now)
        except TalosError as e:         # Gaia failed: the next sweep tries again
            log(f"#{number} hold not released: {e}", agent_name)
    return ", ".join(notes)
