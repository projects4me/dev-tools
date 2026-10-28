"""Garbage collection of per-issue workspaces: removes the .runs/issue-N folders of finished issues.

An issue is finished when its status is done, closed or wont fix, or when its PR is merged. A workspace with a run
in flight is never touched; one with uncommitted changes is kept unless the issue is closed (then it is forced and the changes are discarded).
"""

import re
import subprocess
from pathlib import Path

from . import workspaces

FINISHED = ("done", "closed", "wontfix")


def _norm(status):
    return re.sub(r"[^a-z]", "", str(status or "").lower())


def issues_on_disk(root):
    """The issue numbers that have a workspace under <root>/.runs."""
    runs = Path(root) / workspaces.RUNS_DIR
    if not runs.is_dir():
        return []
    return sorted(int(m.group(1)) for c in runs.iterdir() if (m := re.fullmatch(r"issue-(\d+)", c.name)))


def pr_merged(root, issue):
    """Whether the issue's branch has a merged PR (best effort: False when `gh` can't tell)."""
    for worktree in workspaces.worktrees(root, issue):
        cp = subprocess.run(["gh", "pr", "list", "--head", workspaces.branch_name(issue), "--state", "merged",
                             "--json", "number", "--jq", "length"], cwd=worktree, capture_output=True, text=True,
                            stdin=subprocess.DEVNULL)
        if cp.returncode == 0 and cp.stdout.strip() not in ("", "0"):
            return True
    return False


def collect(root, statuses, running, merged=lambda issue: False, dry_run=False):
    """Remove (or, with dry_run, only list) the finished issues' workspaces.

    statuses: {issue: status name or None}; running: issue numbers with a run in flight;
    merged: issue -> whether its PR is merged. Returns [(issue, action, reason)], action one of
    removed, would-remove, kept.
    """
    result = []
    for issue in issues_on_disk(root):
        status = _norm(statuses.get(issue))
        if issue in running:
            result.append((issue, "kept", "run in flight"))
        else:
            try:
                finished = status in FINISHED or merged(issue)
            except Exception:           # best effort: a failed PR lookup counts as not merged
                finished = False
            if not finished:
                result.append((issue, "kept", f"issue is {statuses.get(issue) or 'unknown'}"))
                continue
            why = statuses.get(issue) if status in FINISHED else "PR merged"
            force = status == "closed"
            try:
                dirty = ", ".join(workspaces.dirty(root, issue))
                if dirty and not force:
                    result.append((issue, "kept", f"uncommitted changes in {dirty}"))
                elif dry_run:
                    result.append((issue, "would-remove", why))
                else:
                    workspaces.remove(root, issue, force=force)
                    result.append((issue, "removed", why))
            except (workspaces.WorkspaceError, OSError) as e:
                result.append((issue, "kept", str(e)))
    return result
