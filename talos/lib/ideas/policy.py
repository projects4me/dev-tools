"""Ideas agents' decisions as pure functions: which events matter, and what is still waiting for the agent."""

MESSAGE = "idea.message.created"
PLAN_STATUS = "idea.plan.status.changed"
UPDATED = "idea.updated"
EVENTS = (MESSAGE, PLAN_STATUS, UPDATED)


def intents(agent, user_id):
    """Hermes intents: the agent's events, user-scoped to the agent's Gaia user (like notification.created)."""
    return [{"projectId": f"user:{user_id}", "eventName": name} for name in (agent.events or EVENTS)]


def job_for(envelope, agent, user_id):
    """The job an event asks of this agent, or None:
    ("turn", idea_id)              the engineer wrote in the idea
    ("approve", idea_id, plan_id)  the engineer approved a plan
    ("stop", idea_id)              the engineer pressed Stop
    Only from users in acceptAssignmentsFrom; events are hints, the worker re-reads the idea."""
    if not isinstance(envelope, dict) or envelope.get("projectId") != f"user:{user_id}":
        return None
    actor = str(envelope.get("actorId") or "")
    if actor == str(user_id) or actor not in agent.accept_from:
        return None
    try:
        idea_id = int((envelope.get("meta") or {}).get("ideaId"))
    except (TypeError, ValueError):
        return None
    name, changes = envelope.get("eventName"), envelope.get("changes") or {}
    if name == MESSAGE and changes.get("role") == "user":
        return ("turn", idea_id)
    if name == PLAN_STATUS and changes.get("status") == "approved":
        try:
            return ("approve", idea_id, int((envelope.get("resource") or {}).get("id")))
        except (TypeError, ValueError):
            return None
    if name == UPDATED and changes.get("agent_state") == "stopping":
        return ("stop", idea_id)
    return None


def unanswered(messages, answered_upto):
    """The engineer's messages no turn has taken in yet: ids above answered_upto (set when a turn starts, so a
    message sent while the agent is answering still counts), or with no state yet, after the last reply."""
    if not answered_upto:
        answered_upto = max((m["id"] for m in messages if m["role"] in ("agent", "error")), default=0)
    return [m for m in messages if m["role"] == "user" and m["id"] > answered_upto]


def approved_plans(plans):
    return [p for p in plans if p["status"] == "approved"]
