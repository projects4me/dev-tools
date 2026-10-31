"""Orchestrator decisions as pure functions: which issues to feed the worker, in what order, and when to stop."""

import fnmatch
import re

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


def triage_skip_reason(issue, state, prefix, projects, owners, worker_id, retry_reasons, max_retries):
    """Why a handed-back issue is not one the owner got for a mechanical reason (so no triage), or None.
    Questions for the reviewer and handbacks still restarted automatically are not incidents."""
    if not in_scope(issue, prefix, projects):
        return f"not in a {prefix} milestone of the worker's projects"
    if issue.get("assignee") not in owners:
        return "not assigned to an owner"
    if _lower(issue.get("status")) not in RESTART_STATUSES:
        return f"status '{issue.get('status')}' is not feedback or pending"
    if issue.get("modifiedUser") != worker_id:
        return "changed by someone since the handback"
    reason = (state or {}).get("handoff_reason")
    if not reason:
        return "no handback reason"
    if reason in QUESTION_REASONS:
        return "a question for the reviewer"
    if reason in retry_reasons and int((state or {}).get("auto_retries") or 0) < max_retries:
        return "restarted automatically"
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


def is_active(issue, done_statuses=DONE_STATUSES):
    """Started and not finished: an issue whose files another issue should not edit at the same time."""
    status = _lower(issue.get("status"))
    return status not in NEW_STATUSES and status not in {_lower(s) for s in (*done_statuses, "wont_fix")}


def ignored(path, patterns):
    """The path matches one of the glob patterns (`*` crosses folders: `docs/*` is everything under docs/)."""
    return any(fnmatch.fnmatchcase(path, p) for p in patterns)


def overlapping(files, others, ignore=()):
    """The first in-flight issue sharing a file with `files`, as (issue, [shared files]), or None. files None or
    empty (nothing predicted) never overlaps; others is [(issue, files or None)]; paths matching `ignore` don't
    count."""
    mine = {f for f in files or () if not ignored(f, ignore)}
    for number, theirs in others:
        shared = sorted(mine & set(theirs or ()))
        if shared:
            return number, shared
    return None


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


# -- automatic merges -------------------------------------------------------------------------------

MERGE, WAIT, HAND_BACK, RERUN = "merge", "wait", "hand_back", "rerun"
REVIEW_STATUS = "in_review"     # where the CodeRabbit loop leaves an issue it hands off


def auto_merges(issue, milestones):
    """The issue's milestone is one whose CodeRabbit-clean PRs are merged automatically (exact name, any case)."""
    return _lower(issue.get("milestone")) in {_lower(m) for m in milestones}


def merge_target(issue, milestones, merger_id, paused):
    """Who the CodeRabbit loop hands a clean issue to: the merger for an auto-merge milestone while it isn't
    paused, else None (the reviewer, as before)."""
    return merger_id if merger_id and not paused and auto_merges(issue, milestones) else None


def is_merge_candidate(issue, milestones, projects, merger_id):
    """Handed to the merger by the CodeRabbit loop: in review, assigned to it, in an auto-merge milestone."""
    return (issue.get("project") in projects and auto_merges(issue, milestones)
            and _lower(issue.get("status")) == REVIEW_STATUS and issue.get("assignee") == merger_id)


def merge_step(gate_state, waited_minutes, wait_limit, merged_today, daily_limit):
    """(MERGE | WAIT | HAND_BACK, why) for one issue, from `ai mergecheck`'s state (ok / pending / fail). A passing
    gate over the daily limit waits: the issue stays with the merger and is merged on the next day's allowance."""
    if gate_state == "ok":
        if merged_today >= daily_limit:
            return WAIT, f"the daily limit of {daily_limit} automatic merge(s) is reached"
        return MERGE, "the merge gate passed"
    if gate_state == "pending":
        if waited_minutes >= wait_limit:
            return HAND_BACK, f"the merge gate was still pending after {int(waited_minutes)} min"
        return WAIT, "the merge gate is pending"
    return HAND_BACK, "the merge gate failed"


CI_RERUN_GRACE_MINUTES = 2      # GitHub may still report the old failure right after a re-run


def only_ci_fails(gate):
    """The merge gate failed only on CI (no conflict, finding or person in the way): a flaky job may pass again."""
    failed = [c for c in gate.get("checks") or [] if c.get("state") == "fail"]
    return gate.get("state") == "fail" and bool(failed) and all(c.get("name") == "ci" for c in failed)


def ci_step(rerun_sha, rerun_minutes, head):
    """RERUN (the first CI failure at this head), WAIT (re-run just now: let GitHub catch up) or HAND_BACK (it failed
    again at the same head). A new push has a new head, so it gets its own re-run."""
    if rerun_sha != head:
        return RERUN
    return WAIT if rerun_minutes < CI_RERUN_GRACE_MINUTES else HAND_BACK


# -- keeping agent PRs mergeable --------------------------------------------------------------------

UP_TO_DATE, BEHIND, CONFLICT, UNKNOWN = "up_to_date", "behind", "conflict", "unknown"
RESOLVE, TELL, NOTHING = "resolve", "tell", "nothing"
AGENT_BRANCH = re.compile(r"^claude-issue-(\d+)$")


def issue_of_branch(name):
    """The issue number of an agent branch (claude-issue-N), or None."""
    match = AGENT_BRANCH.match(str(name or ""))
    return int(match.group(1)) if match else None


def mergeability(pr):
    """CONFLICT, BEHIND, UNKNOWN (GitHub still computing) or UP_TO_DATE, from `gh pr list`'s mergeable and
    mergeStateStatus."""
    mergeable, merge_state = str(pr.get("mergeable") or ""), str(pr.get("mergeStateStatus") or "")
    if mergeable == "CONFLICTING" or merge_state == "DIRTY":
        return CONFLICT
    if mergeable == "UNKNOWN" or merge_state == "UNKNOWN":
        return UNKNOWN
    if merge_state == "BEHIND":
        return BEHIND
    return UP_TO_DATE


def keep_skip_reason(issue, projects, holders, worker_state):
    """Why an agent PR's issue is left alone when its branch is behind or conflicts, or None. Only issues waiting for a
    review or a merge (in_review, held by an owner or the orchestrator) are touched, never one the worker is on."""
    if issue.get("project") not in projects:
        return f"project '{issue.get('project')}' is not one of the worker's"
    if _lower(issue.get("status")) != REVIEW_STATUS:
        return f"status '{issue.get('status')}', not waiting for a review or a merge"
    if issue.get("assignee") not in holders:
        return "assigned to someone the orchestrator doesn't act for"
    if (worker_state or {}).get("running"):
        return "a run is in flight"
    return None


def conflict_step(attempts, max_attempts, noted_head, head):
    """RESOLVE (hand it to the worker to merge the base and resolve), TELL (the attempts are used up: tell the owner,
    once per PR head) or NOTHING."""
    if int(attempts or 0) < max_attempts:
        return RESOLVE
    return TELL if noted_head != head else NOTHING


def only_mergeability_fails(gate):
    """The merge gate failed only because the branch conflicts or is behind: the keeper deals with that."""
    failed = [c for c in gate.get("checks") or [] if c.get("state") == "fail"]
    return gate.get("state") == "fail" and bool(failed) and all(c.get("name") == "mergeable" for c in failed)
