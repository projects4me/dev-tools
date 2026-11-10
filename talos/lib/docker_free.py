"""Frees Docker memory by stopping the p4m test containers nobody is using.

A warm Prometheus test container holds about 3 GB. workspace_gc removes them only for finished issues, so those of
parked or deferred issues keep filling Docker. When Docker is short of memory, the containers of an issue with no run
in flight that is deferred, won't fix or has not changed for a while are stopped (not removed: the workspace and the
next `ai test` still work).

Without memory pressure, the idle rule stops the test container of an issue whose last Talos run (or Agamemnon
`resolve-conflict` test) ended more than idleTestContainerMinutes ago and that has nothing in flight. A container a
`p4m test` run is using (from any session, e.g. a ship run by hand in the issue's workspace) or used within those
minutes is not idle: p4m marks its use (installer/inuse).
"""

import re
import subprocess
from datetime import datetime, timedelta, timezone

from . import workspace_gc

DEFAULT_STALE_HOURS = 72
DEFAULT_IDLE_MINUTES = 15
CONFLICT_TEST_MAX = timedelta(hours=2)     # a resolve-conflict test marked running longer was cut off (Talos stopped)
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


def _conflict_testing(state, now):
    """Whether an Agamemnon resolve-conflict test is using the issue's workspace now (a mark older than
    CONFLICT_TEST_MAX, or one with no time, is one a stopped process left behind)."""
    if not state.get("conflict_test_running"):
        return False
    since = parse_time(state.get("conflict_test_at"))
    return since is not None and now - since < CONFLICT_TEST_MAX


def idle(containers, entries, now, minutes=DEFAULT_IDLE_MINUTES, usage_of=lambda name: (False, None)):
    """[(issue, names, reason)] of the issues whose running test containers have been idle more than `minutes`.

    containers: {issue: [names]} of running containers in Talos workspaces; entries: the sessions entries
    {(agent, issue): state} of talos.db; usage_of(name) -> (in use now, last use as epoch seconds or None), the p4m
    test marks (inuse.usage). An issue qualifies when no agent has a run in flight on it, no resolve-conflict test or
    p4m test run is using it, and the latest end of its runs, conflict tests and test container uses is more than
    `minutes` ago. An issue with no recorded Talos end is left alone; minutes <= 0 turns the rule off."""
    if minutes <= 0:
        return []
    chosen = []
    for issue, names in sorted(containers.items()):
        states = [state for (_, number), state in entries.items() if number == issue]
        if not names or any(s.get("running") or _conflict_testing(s, now) for s in states):
            continue
        used = [usage_of(name) for name in names]
        if any(busy for busy, _ in used):
            continue
        ends = [parse_time(s.get(key)) for s in states for key in ("last_ended_at", "conflict_test_at")]
        ends = [end for end in ends if end is not None]
        if ends:      # a container use only restarts the window of an issue Talos has worked on
            ends += [datetime.fromtimestamp(last, timezone.utc) for _, last in used if last is not None]
        if ends and now - max(ends) > timedelta(minutes=minutes):
            chosen.append((issue, names, f"idle for {(now - max(ends)) // timedelta(minutes=1)}m"))
    return chosen


def plan_idle(root, entries, testenv, inuse, minutes=DEFAULT_IDLE_MINUTES, now=None, run=subprocess.run):
    """idle() for the running test containers under <root>/.runs (a checkout used interactively or a gaia slot
    container never matches). Raises OSError when docker can't be asked."""
    if minutes <= 0:
        return []
    live = running(run)
    boxes = {issue: [n for n in names if n in live]
             for issue, names in workspace_gc.test_containers(root, testenv).items()}
    state = inuse.state_dir(root)
    return idle(boxes, entries, now or datetime.now(timezone.utc), minutes, lambda name: inuse.usage(state, name))


def running(run=subprocess.run):
    """The names of the running containers. Raises OSError when docker can't be asked."""
    try:
        cp = run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        raise OSError(str(e)) from e
    if cp.returncode != 0:
        raise OSError((cp.stderr or cp.stdout).strip() or "docker ps failed")
    return set(cp.stdout.split())


def stop(names, run=subprocess.run):
    """docker stop the named containers. Raises OSError when docker can't be asked or a container won't stop."""
    try:
        cp = run(["docker", "stop", *names], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        raise OSError(str(e)) from e
    if cp.returncode != 0:
        raise OSError((cp.stderr or cp.stdout).strip() or "docker stop failed")


def heaviest(state, paused=()):
    """The running p4m test container using the most memory, as a one-name list, when at least two run (pausing the
    only one frees nothing for the others); else []. state is docker_state.read()'s dict; `paused` names are skipped."""
    running = set(state.get("p4mRunning") or []) - set(paused)
    if len(running) < 2:
        return []
    usage = {c["name"]: c["memory"] for c in state.get("containers") or [] if c["name"] in running}
    return [max(usage, key=usage.get)] if usage else []


def _docker_each(verb, names, run):
    try:
        cp = run(["docker", verb, *names], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        raise OSError(str(e)) from e
    if cp.returncode != 0:
        raise OSError((cp.stderr or cp.stdout).strip() or f"docker {verb} failed")


def pause(names, run=subprocess.run):
    """docker pause the named containers (their memory stays, their processes freeze, so the others can finish)."""
    _docker_each("pause", names, run)


def unpause(names, run=subprocess.run):
    """docker unpause the named containers. Raises OSError when docker can't be asked."""
    _docker_each("unpause", names, run)
