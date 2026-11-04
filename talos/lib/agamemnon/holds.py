"""Giving held issues back (`aga hold-until-tomorrow`, `aga hold-until-docker`): on each orchestrator sweep, a hold
whose time has come is released and the issue assigned to the worker, as long as it is still assigned to Agamemnon.
When someone moved it in the meantime, the hold is only released: their choice stands.

A Docker hold is due when Docker's verdict is ok, and then at most one such issue is given back per sweep (the oldest
first), so they don't all start at once. It ends without giving the issue back when it was moved to deferred or won't
fix, and goes to the owner with a comment when Docker did not come back by the hold's latest time."""

import html

from ..config import TalosError, log
from ..docker_free import PARKED, _norm
from . import journal


def release_due(tracker, worker_id, my_id, now, agent_name, dry_run=False, docker_ok=None, owner=None):
    """Release the due holds; a short note of what was done ("" when nothing was due). docker_ok() tells whether
    Docker's verdict is ok (without it, Docker holds are left alone); owner is who gets an expired Docker hold."""
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
    if docker_ok is not None:
        notes += _release_docker(tracker, worker_id, my_id, now, agent_name, dry_run, docker_ok, owner)
    return ", ".join(notes)


def _release_docker(tracker, worker_id, my_id, now, agent_name, dry_run, docker_ok, owner):
    notes, given = [], False
    for hold in journal.docker_holds():
        number = hold["issue"]
        try:
            issue = tracker.issue(number)
            mine = str(issue.get("assignee") or "") == str(my_id)
            if not mine:
                action, note = None, f"#{number} Docker hold ended (reassigned meanwhile)"
            elif _norm(issue.get("status")) in PARKED:
                action, note = None, f"#{number} Docker hold ended ({issue.get('status')}), not given back"
            elif not given and docker_ok():
                action, note = "worker", f"#{number} given back to the worker (Docker is ok)"
            elif hold["until"] <= now:
                action, note = "owner", f"#{number} Docker hold expired, given to the owner"
            else:
                continue
            given = given or action == "worker"
            if dry_run:
                notes.append(note + " (dry run)")
                continue
            if action == "worker":
                tracker.assign(number, worker_id)
            elif action == "owner":
                if owner:
                    tracker.assign(number, owner)
                tracker.comment(number, "Agent Agamemnon: waited for Docker, over to the owner",
                                f"<p>Docker was not ok by {html.escape(str(hold['until']))}. The issue was held for "
                                f"it and is now yours.</p>")
            journal.release_hold(number, now)
            notes.append(note)
        except TalosError as e:         # Gaia failed: the next sweep tries again
            log(f"#{number} Docker hold not released: {e}", agent_name)
    return notes
