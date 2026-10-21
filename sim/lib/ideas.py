"""Sim Gaia's idea resources: who may change what, and the domain event each change publishes.

Plays the part of Gaia's controllers and LiveEvents components for resources Gaia doesn't have yet. An idea has
an owner (the engineer) and an agent (the Gaia user Talos runs as, e.g. Agent Nestor). Every change publishes one
envelope per participant, user-scoped ("user:<id>"), like notification.created.
"""

from .bus import envelope

USER_ROLES = ("user",)
AGENT_ROLES = ("agent", "system", "error")
OWNER_STATES = ("stopping",)
AGENT_STATES = ("idle", "queued", "working")
# plan status changes each side may make: target -> statuses it may come from
OWNER_PLAN = {"approved": ("proposed", "failed")}
AGENT_PLAN = {"creating": ("approved",), "failed": ("approved", "creating"), "created": ("creating",)}
MAX_TEXT = 100_000


class Forbidden(Exception):
    pass


class NotFound(Exception):
    pass


class Ideas:
    def __init__(self, store, bus):
        self.store, self.bus = store, bus

    # -- events -------------------------------------------------------------------------------------------------

    def _publish(self, idea, event_name, resource_type, resource_id, actor, changes=None, meta=None):
        for user in {idea["owner"], idea["agent"]}:
            self.bus.publish(envelope(event_name, f"user:{user}", resource_type, resource_id, actor, changes,
                                      {"ideaId": idea["id"], **(meta or {})}))

    # -- reads --------------------------------------------------------------------------------------------------

    def _idea(self, idea_id, actor):
        idea = self.store.idea(idea_id)
        if not idea or actor not in (idea["owner"], idea["agent"]):
            raise NotFound(f"no idea #{idea_id}")
        return idea

    def _role(self, idea, actor):
        return "owner" if actor == idea["owner"] else "agent"

    def list(self, actor):
        out = []
        for i in self.store.ideas_for(actor):
            out.append({**i, "cost_usd": self.store.cost(i["id"])})
        return out

    def get(self, idea_id, actor):
        idea = self._idea(idea_id, actor)
        issues = self.store.issues(idea_id)
        plans = self.store.plans(idea_id)
        for p in plans:
            p["issues"] = [i for i in issues if i["plan_id"] == p["id"]]
        return {"idea": {**idea, "cost_usd": self.store.cost(idea_id), "agent_online": self.bus.online(idea["agent"])},
                "messages": self.store.messages(idea_id), "plans": plans}

    # -- idea ---------------------------------------------------------------------------------------------------

    def create(self, actor, title, agent):
        title = " ".join(str(title or "").split())[:200]
        if not title:
            raise ValueError("an idea needs a title")
        if not agent or str(agent) == actor:
            raise ValueError("an idea needs an agent (the Gaia user id of the agent to brainstorm with)")
        idea_id = self.store.insert_idea(title, actor, str(agent))
        idea = self.store.idea(idea_id)
        self._publish(idea, "idea.created", "idea", idea_id, actor, {"title": title})
        return idea

    def update(self, idea_id, actor, fields):
        idea = self._idea(idea_id, actor)
        role = self._role(idea, actor)
        changes = {}
        if "title" in fields:
            if role != "owner":
                raise Forbidden("only the owner renames an idea")
            title = " ".join(str(fields["title"] or "").split())[:200]
            if not title:
                raise ValueError("an idea needs a title")
            changes["title"] = title
        if "status" in fields:
            allowed = ("archived",) if role == "owner" else ("created",)
            if fields["status"] not in allowed:
                raise Forbidden(f"the {role} can't set status '{fields['status']}'")
            changes["status"] = fields["status"]
        if "agent_state" in fields:
            allowed = OWNER_STATES if role == "owner" else AGENT_STATES
            if fields["agent_state"] not in allowed:
                raise Forbidden(f"the {role} can't set agent_state '{fields['agent_state']}'")
            changes["agent_state"] = fields["agent_state"]
        if not changes:
            raise ValueError("nothing to change (title, status or agent_state)")
        self.store.update_idea(idea_id, **changes)
        self._publish(idea, "idea.updated", "idea", idea_id, actor, changes)
        return self.store.idea(idea_id)

    # -- ideamessage --------------------------------------------------------------------------------------------

    def add_message(self, actor, idea_id, role, body, steps=None, plan_id=None, cost=None):
        idea = self._idea(idea_id, actor)
        allowed = USER_ROLES if self._role(idea, actor) == "owner" else AGENT_ROLES
        if role not in allowed:
            raise Forbidden(f"can't post a '{role}' message here")
        body = str(body or "").strip()
        if not body or len(body) > MAX_TEXT:
            raise ValueError(f"a message needs 1 to {MAX_TEXT} characters")
        if plan_id is not None and not (self.store.plan(plan_id) or {}).get("idea_id") == idea_id:
            raise ValueError("plan_id is not a plan of this idea")
        mid = self.store.insert_message(idea_id, role, actor, body, steps=steps, plan_id=plan_id, cost=cost)
        self._publish(idea, "idea.message.created", "ideamessage", mid, actor, {"role": role}, {"messageId": mid})
        return mid

    # -- ideaplan -----------------------------------------------------------------------------------------------

    def add_plan(self, actor, idea_id, body):
        idea = self._idea(idea_id, actor)
        if self._role(idea, actor) != "agent":
            raise Forbidden("only the agent proposes plans")
        if not isinstance(body, dict) or not isinstance(body.get("issues"), list) or not body.get("summary"):
            raise ValueError("a plan needs summary and issues")
        pid, version = self.store.insert_plan(idea_id, body)
        self._publish(idea, "idea.plan.created", "ideaplan", pid, actor, {"version": version}, {"version": version})
        return pid, version

    def set_plan_status(self, actor, plan_id, status, error=None):
        plan = self.store.plan(plan_id)
        if not plan:
            raise NotFound(f"no plan {plan_id}")
        idea = self._idea(plan["idea_id"], actor)
        rules = OWNER_PLAN if self._role(idea, actor) == "owner" else AGENT_PLAN
        if status not in rules:
            raise Forbidden(f"can't set a plan to '{status}'")
        if status == "approved":
            latest = max(p["version"] for p in self.store.plans(idea["id"]))
            if plan["version"] != latest:
                raise ValueError(f"plan v{plan['version']} was superseded by v{latest}")
        if not self.store.set_plan_status(plan_id, status, error=error, only_from=rules[status]):
            raise ValueError(f"plan v{plan['version']} is {plan['status']}; it can't become {status}")
        self._publish(idea, "idea.plan.status.changed", "ideaplan", plan_id, actor, {"status": status},
                      {"version": plan["version"]})
        return self.store.plan(plan_id)

    # -- ideaissue ----------------------------------------------------------------------------------------------

    def add_issue(self, actor, plan_id, key, number, project_code, subject, url):
        plan = self.store.plan(plan_id)
        if not plan:
            raise NotFound(f"no plan {plan_id}")
        idea = self._idea(plan["idea_id"], actor)
        if self._role(idea, actor) != "agent":
            raise Forbidden("only the agent records created issues")
        iid = self.store.insert_issue(idea["id"], plan_id, key, int(number), project_code, subject, url)
        self._publish(idea, "idea.issue.created", "ideaissue", iid, actor, {"number": int(number)},
                      {"version": plan["version"], "key": key})
        return iid
