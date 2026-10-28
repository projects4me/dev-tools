"""Garbage collection of per-issue workspaces: removes the .runs/issue-N folders of finished issues.

An issue is finished when its status is done, closed or wont fix, or when its PR is merged. A workspace with a run
in flight is never touched; one with uncommitted changes is kept unless the issue is closed (then it is forced and the changes are discarded).
The container runner's leftovers (containers, Claude volumes and folders) go with it, under the same rules.
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


def _finished(issue, statuses, merged):
    """(finished, why): the status says so, or its PR is merged (a failed PR lookup counts as not merged)."""
    status = _norm(statuses.get(issue))
    if status in FINISHED:
        return True, statuses.get(issue)
    try:
        return bool(merged(issue)), "PR merged"
    except Exception:
        return False, None


def collect_issue(root, issue, statuses, running, merged=lambda issue: False, dry_run=False, leftovers=None):
    """Remove (or, with dry_run, only list) one finished issue's workspace and container leftovers.

    leftovers: issue -> (names, remove) where remove() deletes them; None when the issue has none.
    Returns [(issue, action, reason)], action one of removed, would-remove, kept.
    """
    if issue in running:
        return [(issue, "kept", "run in flight")]
    finished, why = _finished(issue, statuses, merged)
    if not finished:
        return [(issue, "kept", f"issue is {statuses.get(issue) or 'unknown'}")]
    result = []
    force = _norm(statuses.get(issue)) == "closed"
    if issue in issues_on_disk(root):
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
    try:
        names, remove = leftovers(issue) if leftovers else ([], None)
    except (OSError, subprocess.SubprocessError) as e:      # docker not reachable: nothing is removed
        return result + [(issue, "kept", f"container leftovers unknown: {e}")]
    for name in names:
        result.append((issue, "would-remove" if dry_run else "removed", name))
    if names and not dry_run:
        try:
            remove()
        except (OSError, subprocess.SubprocessError) as e:
            result = [r for r in result if r[2] not in names] + [(issue, "kept", f"{names[0]}: {e}")]
    return result


def collect(root, statuses, running, merged=lambda issue: False, dry_run=False, leftovers=None, extra_issues=()):
    """collect_issue for every issue with a workspace (or leftovers, extra_issues) on this machine.

    statuses: {issue: status name or None}; running: issue numbers with a run in flight;
    merged: issue -> whether its PR is merged.
    """
    result = []
    for issue in sorted({*issues_on_disk(root), *extra_issues}):
        result += collect_issue(root, issue, statuses, running, merged, dry_run, leftovers)
    return result


def status_of(issue, tracker):
    """The issue's status name from Gaia, or None when it can't be read (the workspace is then kept)."""
    try:
        return tracker.issue(issue)["status"]
    except Exception:
        return None


def in_flight(entries):
    """Issue numbers with a run in flight, from the sessions entries {(agent, issue): state}."""
    return {issue for (_, issue), v in entries.items() if v.get("running")}


def collect_all(root, entries, agent_for, tracker_for, merged, dry_run=False, only=None, container=None):
    """The gc over what is on this machine, with the lookups injected so it can be tested.

    entries: sessions entries {(agent, issue): state}; agent_for(name) -> Agent or None; tracker_for(agent) ->
    Tracker; merged: issue -> bool; only: collect just this issue; container: the container module (leftovers,
    remove_leftovers). Container leftovers are looked up for issues owned by an agent with a container runner.
    """
    owner = {issue: name for (name, issue) in entries}
    runners = {}
    for issue, name in owner.items():
        agent = agent_for(name)
        if agent is not None and agent.runner.get("type") == "container":
            runners[issue] = (name, agent)
    issues = {*issues_on_disk(root), *runners}
    if only is not None:
        issues &= {only}
    statuses = {}
    for issue in issues:
        agent = (runners.get(issue) or (None, agent_for(owner.get(issue))))[1]
        statuses[issue] = status_of(issue, tracker_for(agent)) if agent is not None else None

    def leftovers(issue):
        if issue not in runners or container is None:
            return [], None
        name, agent = runners[issue]
        found = container.leftovers(name, issue, agent.runner)
        return [f"{kind} {what}" for kind, what in found], \
            lambda: container.remove_leftovers(name, issue, agent.runner, found)

    results = []
    for issue in sorted(issues):
        results += collect_issue(root, issue, statuses, in_flight(entries), merged, dry_run, leftovers)
    return results
