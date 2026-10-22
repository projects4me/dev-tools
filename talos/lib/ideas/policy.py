"""Ideas agents' decisions as pure functions: which events matter, and what is still waiting for the agent."""

MESSAGE = "idea.message.created"
PLAN_STATUS = "idea.plan.status.changed"
UPDATED = "idea.updated"
EVENTS = (MESSAGE, PLAN_STATUS, UPDATED)
GAIA_EVENTS = (MESSAGE, PLAN_STATUS)     # Gaia ideas: no Stop yet
RESUMABLE = ("approved", "creating")     # creating: Talos stopped mid-approval


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


def gaia_job_for(envelope, agent, user_id, project_ids):
    """The job a Gaia idea event asks of this agent, or None:
    ("turn", idea_id)              someone else wrote a message in the idea
    ("approve", idea_id, plan_id)  someone approved a plan
    Only in the agent's projects, never the agent's own events, only from users in acceptAssignmentsFrom. Idea and
    plan ids are GUIDs; events are hints, the worker re-reads the idea."""
    if not isinstance(envelope, dict) or str(envelope.get("projectId") or "") not in set(project_ids.values()):
        return None
    actor = str(envelope.get("actorId") or "")
    if actor == str(user_id) or actor not in agent.accept_from:
        return None
    idea_id = str((envelope.get("meta") or {}).get("ideaId") or "")
    if not idea_id:
        return None
    name, changes = envelope.get("eventName"), envelope.get("changes") or {}
    if name == MESSAGE and changes.get("kind") == "message":
        return ("turn", idea_id)
    plan_id = str((envelope.get("resource") or {}).get("id") or "")
    if name == PLAN_STATUS and changes.get("status") == "approved" and plan_id:
        return ("approve", idea_id, plan_id)
    return None


def unanswered(messages, answered_upto):
    """The engineer's messages no turn has taken in yet, in order: those after the message answered_upto (the last
    one a turn took in, set when a turn starts, so a message sent while the agent is answering still counts), or
    with no state yet (or that message gone), those after the agent's last reply. Messages come oldest first; ids
    need not be ordered (Gaia's are GUIDs)."""
    ids = [m["id"] for m in messages]
    if answered_upto and answered_upto in ids:
        start = ids.index(answered_upto) + 1
    else:
        replies = [i for i, m in enumerate(messages) if m["role"] in ("agent", "error")]
        start = replies[-1] + 1 if replies else 0
    return [m for m in messages[start:] if m["role"] == "user"]


def resumable_plans(plans):
    """Plans to (re)create: approved, or left creating when Talos stopped."""
    return [p for p in plans if p["status"] in RESUMABLE]
