"""Talos decisions as pure functions: which events matter, and what an agent should do with an issue."""

from dataclasses import dataclass
from datetime import datetime

ASSIGNEE_EVENT = "issue.assignee.changed"
OUTCOMES = ("done", "needs_input", "blocked", "escalate")


@dataclass
class Decision:
    run: bool
    reason: str
    action: object = None   # registry.Action when run is True
    repo: str = None


def event_issue(envelope, agent, agent_user_id):
    """The issue number when an envelope hands this agent work from an allowed user, else None."""
    if not isinstance(envelope, dict) or envelope.get("eventName") != ASSIGNEE_EVENT:
        return None
    changes = envelope.get("changes") or {}
    if str(changes.get("assignee") or "") != agent_user_id:
        return None
    if str(envelope.get("actorId") or "") not in agent.accept_from:
        return None
    number = (envelope.get("meta") or {}).get("issueNumber")
    try:
        return int(number)
    except (TypeError, ValueError):
        return None


def decide(issue, agent, agent_user_id, require_allowed_modifier=False):
    """Whether the agent should work this issue now, from the issue as Gaia has it.

    require_allowed_modifier is for reconciliation, where there is no event telling us who
    assigned it: only act if the last person to change the issue is allowed to hand off work.
    """
    if issue["assignee"] != agent_user_id:
        return Decision(False, "no longer assigned to the agent")
    repo = agent.repo_for(issue["project"])
    if not repo:
        return Decision(False, f"project '{issue['project']}' is not one of the agent's projects")
    if agent.milestones and issue["milestone"] not in agent.milestones:
        return Decision(False, f"milestone '{issue['milestone']}' is not one of {agent.milestones}")
    if require_allowed_modifier and issue["modifiedUser"] not in agent.accept_from:
        return Decision(False, "last changed by a user who can't hand this agent work")
    action = agent.action_for(issue["status"])
    if not action:
        return Decision(False, f"no action for status '{issue['status']}'")
    return Decision(True, f"status '{issue['status']}'", action, repo)


def choose_model(agent, issue, escalated=False):
    """Escalation wins; then the issue's complexity recommendation; then the agent's default."""
    if escalated:
        return agent.model_escalate
    return agent.model_by_complexity.get(issue.get("complexity")) or agent.model_default


def iterations_left(agent, runs):
    """How many more Claude runs this issue may have (None = unlimited)."""
    if agent.max_iterations <= 0:
        return None
    return max(agent.max_iterations - runs, 0)


CATCH_UP = (" A previous Claude session worked on this issue, but it is not resumed (its cached context has "
            "expired, so a fresh start is cheaper). Catch up from the issue thread (dev-tools/ai/ai issue {issue}) "
            "and the branch (in {repo}: git log --oneline origin/dev..claude-issue-{issue} and "
            "git diff --stat origin/dev...claude-issue-{issue}), then read only the files you need.")


def resume_session(action, state, now, max_idle_minutes):
    """(session id to resume or None, why). Resume only while the session's prompt cache is likely warm:
    a cold resume re-pays the whole earlier context, which costs more than a fresh catch-up."""
    session_id = state.get("session_id")
    if not action.resume or not session_id:
        return None, "new session"
    ended = state.get("last_ended_at")
    if not ended:
        return None, "previous session's end time unknown"
    idle = (now - datetime.fromisoformat(ended)).total_seconds() / 60
    if max_idle_minutes and idle > max_idle_minutes:
        return None, f"previous session idle {idle:.0f} min (> {max_idle_minutes}), cache cold"
    return session_id, f"resume {session_id[:8]} (idle {idle:.0f} min)"


def render_prompt(action, issue, repo, catch_up=False, reviewer="your reviewer"):
    """Fill the action's prompt. Only tracker-controlled values go in, never issue text."""
    values = dict(issue=issue["number"], repo=repo, project=issue["project"], status=issue["status"],
                  reviewer=reviewer)
    prompt = action.prompt.format(**values)
    return prompt + CATCH_UP.format(**values) if catch_up else prompt


def read_outcome(result):
    """(outcome, summary, comment) from a `claude -p --output-format json` result dict.

    Missing or malformed structured output counts as a failure, never as success.
    """
    if not isinstance(result, dict):
        return "failed", "no result from Claude", ""
    if result.get("is_error") or result.get("subtype") not in (None, "success"):
        detail = result.get("result") or result.get("subtype") or "unknown error"
        return "failed", f"Claude run ended with {result.get('subtype')}: {str(detail)[:1500]}", ""
    out = result.get("structured_output")
    if not isinstance(out, dict) or out.get("outcome") not in OUTCOMES:
        return "failed", "Claude finished without a valid outcome", str(result.get("result") or "")[:1500]
    return out["outcome"], str(out.get("summary") or ""), str(out.get("comment") or "")
