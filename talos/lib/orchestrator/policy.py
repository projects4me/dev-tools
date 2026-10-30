"""Orchestrator decisions as pure functions: which issues to feed the worker, in what order, and when to stop."""

from ..dependency_gate import DONE_STATUSES, unmet_dependencies

PRIORITIES = ("blocker", "critical", "high", "medium", "low")   # Gaia's, most urgent first
NEW_STATUSES = ("new",)
RESTART_STATUSES = ("feedback", "pending")      # where a handback leaves an issue (needs_input, blocked/failed)
NOT_FED_TYPES = ("epic",)                       # an epic is a container of issues, never worked itself
QUESTION_REASONS = ("agent_needs_input",)       # a question for the reviewer: neither a failure nor a success
SUCCESS = "success"                             # a fed issue's run ended done or awaiting_bot_review
HANDBACK = "handback"                           # a fed issue was handed back (with a reason code)


def _lower(value):
    return str(value or "").strip().lower()


def in_scope(issue, prefix, projects):
    """The issue is in one of the worker's projects and in a milestone starting with prefix (any case)."""
    return issue.get("project") in projects and _lower(issue.get("milestone")).startswith(_lower(prefix))


def new_skip_reason(issue, prefix, projects, owners, dependency_statuses, done_statuses=DONE_STATUSES):
    """Why a not-yet-started issue is not fed to the worker, or None when it is a candidate."""
    if not in_scope(issue, prefix, projects):
        return f"not in a {prefix} milestone of the worker's projects"
    if _lower(issue.get("status")) not in NEW_STATUSES:
        return f"status '{issue.get('status')}' is not new"
    if issue.get("assignee") and issue["assignee"] not in owners:
        return "assigned to someone other than an owner"
    if _lower(issue.get("type")) in NOT_FED_TYPES:
        return "an epic"
    waiting = unmet_dependencies(issue.get("dependsOn") or [], dependency_statuses or {}, done_statuses)
    if waiting:
        return "waiting on " + ", ".join(f"#{n}" for n in waiting)
    return None


def is_new_candidate(issue, prefix, projects, owners, dependency_statuses, done_statuses=DONE_STATUSES):
    return new_skip_reason(issue, prefix, projects, owners, dependency_statuses, done_statuses) is None


def restart_skip_reason(issue, state, prefix, projects, owners, worker_id, retry_reasons, max_retries,
                        branch_holder=None):
    """Why a handed-back issue is not restarted, or None when it is a candidate.

    state is the worker's Talos state for the issue (handoff_reason, auto_retries); branch_holder is the folder that
    still has the issue's branch checked out elsewhere (only looked up for branch_checked_out), or None."""
    if not in_scope(issue, prefix, projects):
        return f"not in a {prefix} milestone of the worker's projects"
    if issue.get("assignee") not in owners:
        return "not assigned to an owner"
    if _lower(issue.get("status")) not in RESTART_STATUSES:
        return f"status '{issue.get('status')}' is not feedback or pending"
    if issue.get("modifiedUser") != worker_id:
        return "changed by someone since the handback"
    reason = (state or {}).get("handoff_reason")
    if reason not in retry_reasons:
        return f"handback reason '{reason}' is not restarted automatically"
    if int((state or {}).get("auto_retries") or 0) >= max_retries:
        return f"restarted {max_retries} time(s) already"
    if reason == "branch_checked_out" and branch_holder:
        return f"its branch is still checked out in {branch_holder}"
    return None


def is_restart_candidate(issue, state, prefix, projects, owners, worker_id, retry_reasons, max_retries,
                         branch_holder=None):
    return restart_skip_reason(issue, state, prefix, projects, owners, worker_id, retry_reasons, max_retries,
                               branch_holder) is None


def rank(issues):
    """Most urgent priority first, then milestone name, then issue number."""
    def key(issue):
        priority = _lower(issue.get("priority"))
        return (PRIORITIES.index(priority) if priority in PRIORITIES else len(PRIORITIES),
                _lower(issue.get("milestone")), int(issue["number"]))
    return sorted(issues, key=key)


def free_slots(max_in_flight, in_flight):
    """Issues the worker can take now."""
    return max(0, int(max_in_flight) - int(in_flight))


def fail_streak(streak, events, retry_reasons, max_retries):
    """The failure streak after events, oldest first: {"kind": SUCCESS} ends it; a {"kind": HANDBACK, "reason",
    "auto_retries"} lengthens it unless it is a question or a stop that is still restarted automatically."""
    for event in events:
        if event.get("kind") == SUCCESS:
            streak = 0
            continue
        reason = event.get("reason")
        if reason in QUESTION_REASONS:
            continue
        if reason in retry_reasons and int(event.get("auto_retries") or 0) < max_retries:
            continue
        streak += 1
    return streak


def breaker_tripped(streak, limit):
    return limit > 0 and streak >= limit
