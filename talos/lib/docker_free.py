"""Frees Docker memory by stopping the p4m test containers nobody is using.

A warm Prometheus test container holds about 3 GB. workspace_gc removes them only for finished issues, so those of
parked or deferred issues keep filling Docker. When Docker is short of memory, the containers of an issue with no run
in flight that is deferred, won't fix or has not changed for a while are stopped (not removed: the workspace and the
next `ai test` still work).
"""

import re
import subprocess
from datetime import datetime, timedelta, timezone

from . import workspace_gc

DEFAULT_STALE_HOURS = 72
PARKED = ("deferred", "wontfix")


def _norm(status):
    return re.sub(r"[^a-z]", "", str(status or "").lower())


def parse_time(text):
    """A Gaia time ('2026-11-04 22:15:00' or ISO, UTC unless it carries an offset) as an aware datetime, or None."""
    try:
        when = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def stoppable(containers, issues, running, now, stale_hours=DEFAULT_STALE_HOURS):
    """[(issue, names, reason)] of the issues whose test containers may be stopped.

    containers: {issue: [names]} (workspace_gc.test_containers); issues: {issue: {"status", "modified"}} as the tracker
    reads them (a missing or unreadable issue is never chosen); running: issue numbers with a run in flight; now: aware
    datetime. A status of deferred or won't fix, or no change for more than stale_hours, qualifies."""
    chosen = []
    for issue, names in sorted(containers.items()):
        info = issues.get(issue)
        if not names or issue in running or not info:
            continue
        status = info.get("status")
        modified = parse_time(info.get("modified"))
        if _norm(status) in PARKED:
            chosen.append((issue, names, status))
        elif modified is not None and now - modified > timedelta(hours=stale_hours):
            chosen.append((issue, names, f"unchanged for {(now - modified) // timedelta(hours=1)}h"))
    return chosen


def plan(root, entries, tracker, testenv, stale_hours=DEFAULT_STALE_HOURS, now=None):
    """stoppable() for the test containers under <root>/.runs, reading the issues from the tracker (an unreadable one
    is left out) and the runs in flight from the sessions entries. Raises OSError when docker can't be asked."""
    boxes = workspace_gc.test_containers(root, testenv)
    issues = {}
    for issue in boxes:
        try:
            issues[issue] = tracker.issue(issue)
        except Exception:
            continue
    return stoppable(boxes, issues, workspace_gc.in_flight(entries), now or datetime.now(timezone.utc), stale_hours)


def stop(names, run=subprocess.run):
    """docker stop the named containers. Raises OSError when docker can't be asked or a container won't stop."""
    try:
        cp = run(["docker", "stop", *names], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        raise OSError(str(e)) from e
    if cp.returncode != 0:
        raise OSError((cp.stderr or cp.stdout).strip() or "docker stop failed")
