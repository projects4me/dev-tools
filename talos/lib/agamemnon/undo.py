"""Undoing journaled `aga` actions (`talos agamemnon undo`). An action is undone only while the state it left is
still there: each undo re-reads the state its action recorded after acting and refuses when it has moved since.
Several actions are undone newest first, stopping at the first that cannot be."""

from datetime import datetime

from .. import workspaces
from ..config import TalosError
from . import journal
from .tool import PUSH_REMOTE


class UndoRefused(TalosError):
    """The action cannot be undone (now or ever); nothing was changed."""


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Undoer:
    def __init__(self, settings, ports, now=_now):
        self.s, self.p, self.now = settings, ports, now

    def undo(self, action_id):
        """Undo one action; a sentence saying what was done. UndoRefused when it can't be."""
        action = journal.action(action_id)
        if not action:
            raise UndoRefused(f"no action {action_id}")
        if action["undone_at"]:
            raise UndoRefused(f"action {action_id} was already undone at {action['undone_at']}")
        op = action["undo"]
        if not op:
            raise UndoRefused(f"action {action_id} ({action['verb']}) cannot be undone: {action['undo_note']}")
        note = getattr(self, "_" + op["op"])(action, op)
        journal.mark_undone(action_id, self.now(), note)
        return note

    def undo_since(self, since):
        """Undo every action from `since` that is not undone yet, newest first; [(id, note)]. Stops at the first that
        cannot be undone, which is reported as UndoRefused (the ones before it stay undone)."""
        done = []
        for action in reversed(journal.actions(since=since)):
            if action["undone_at"]:
                continue
            try:
                done.append((action["id"], self.undo(action["id"])))
            except UndoRefused as e:
                raise UndoRefused(f"{e}; undone before stopping: "
                                  f"{', '.join(str(i) for i, _ in done) or 'none'}")
        return done

    # -- the state the action left ---------------------------------------------------------

    def _moved(self, action, now_state, keys):
        after = action["state_after"] or {}
        changed = [k for k in keys if now_state.get(k) != after.get(k)]
        if changed:
            raise UndoRefused(f"action {action['id']}: the state moved since ("
                              + ", ".join(f"{k} {after.get(k)!r} -> {now_state.get(k)!r}" for k in changed) + ")")

    def _issue_state(self, issue):
        i = self.p.tracker.issue(issue)
        return {"status": i.get("status"), "assignee": i.get("assignee"),
                "running": bool(self.p.worker_state(issue).get("running"))}

    def _folder(self, issue):
        """The issue's workspace folder for its repo (the only folder under .runs/issue-N that holds the ref)."""
        root = workspaces.path(self.s.workspace_root, issue)
        repo = self.s.worker.repo_for(self.p.tracker.issue(issue).get("project"))
        folder = root / repo if repo else None
        if not folder or not folder.is_dir():
            raise UndoRefused(f"#{issue} has no workspace any more")
        return folder

    # -- undo operations -------------------------------------------------------------------

    def _none(self, action, op):
        return f"{action['verb']} changed nothing; marked undone"

    def _assign(self, action, op):
        self._moved(action, self._issue_state(op["issue"]), ("status", "assignee", "running"))
        self.p.tracker.assign(op["issue"], op["assignee"])
        if op.get("workerState"):           # a send for a conflict also set the worker's action and attempt count
            self.p.update_worker_state(op["issue"], **op["workerState"])
        return f"#{op['issue']} assigned back to {op['assignee'] or 'nobody'}"

    def _release_hold(self, action, op):
        hold = journal.active_hold(op["issue"])
        if not hold:
            raise UndoRefused(f"action {action['id']}: #{op['issue']} is not held any more")
        self._moved(action, self._issue_state(op["issue"]), ("status", "assignee", "running"))
        self.p.tracker.assign(op["issue"], op["assignee"])
        journal.release_hold(op["issue"], self.now())
        return f"#{op['issue']} released and assigned back to {op['assignee'] or 'nobody'}"

    def _delete_ref(self, action, op):
        folder = self._folder(op["issue"])
        sha = self.p.git(folder, "rev-parse", "--verify", "--quiet", op["ref"], check=False)
        self._moved(action, {"ref": op["ref"] if sha else None, "sha": sha}, ("ref", "sha"))
        needed = [a["id"] for a in journal.actions(incident_id=action["incident"])
                  if a["id"] > action["id"] and not a["undone_at"] and a["undo"]
                  and a["undo"].get("op") == "checkout_files" and a["undo"].get("from") == op["sha"]]
        if needed:
            raise UndoRefused(f"action {action['id']}: restore {', '.join(map(str, needed))} undoes from this "
                              f"backup; undo it first")
        self.p.push(folder, f":{op['ref']}")
        self.p.git(folder, "update-ref", "-d", op["ref"], op["sha"])
        return f"{op['ref']} deleted here and on {PUSH_REMOTE}"

    def _checkout_files(self, action, op):
        folder = self._folder(op["issue"])
        now_files = {p: self.p.git(folder, "hash-object", "--", p, check=False) if (folder / p).exists() else None
                     for p in op["paths"]}
        self._moved(action, {"files": now_files}, ("files",))
        self.p.git(folder, "checkout", op["from"], "--", *op["paths"])
        return f"{', '.join(op['paths'])} put back from {op['from'][:7]}"
