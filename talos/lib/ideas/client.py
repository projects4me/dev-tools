"""Idea resources as the agent on Gaia, through the gaia MCP's idea tools (the tracker's Tools, as the agent).

Worker, conversation and approval work on one shape of an idea (the sim's, see sim_client.py); this maps Gaia's
onto it:
- a message's `kind` plus its author become `role`: kind "message" by the agent is "agent", by anyone else "user";
  "system" and "error" stay as they are
- `ideaplanId` is `plan_id`, `costUsd` is `cost_usd`; `steps` and `screens` are JSON in Gaia
- a plan's created issues are its Ideaplanissues; the idea's screens are the ones its messages carry (an upload
  each, the latest version of a key last)
- the idea's owner is who created it; Gaia knows nothing about agents, so every idea in the agent's projects is
  the agent's, and its working state is a "system" message rather than a field
Ids are GUIDs (strings).
"""

import base64
import json

AGENT_KINDS = {"agent": "message"}           # role -> kind; "system" and "error" are both


def _json(text, default):
    if text is None or text == "":
        return default
    if not isinstance(text, str):
        return text
    try:
        return json.loads(text)
    except ValueError:
        return default


class GaiaIdeasClient:
    def __init__(self, tracker, user_id, display_name):
        self.tracker, self.user_id, self.display_name = tracker, str(user_id), display_name

    def _tool(self, name, args):
        return self.tracker.tool(name, args)

    # -- reads --------------------------------------------------------------------------------------------------

    def ideas(self):
        """Every idea in the agent's projects."""
        rows = []
        for project in self.tracker.agent.projects:
            for idea in self._tool("list_ideas", {"project": project})["ideas"]:
                rows.append({"id": idea["id"], "title": idea.get("title"), "status": idea.get("status"),
                             "agent": self.user_id})
        return rows

    def idea(self, idea_id):
        """{idea, messages (oldest first), plans (each with issues), screens}."""
        return to_idea(self._tool("get_idea", {"idea": str(idea_id)}), self.user_id)

    def screen_png(self, upload_id):
        return self.tracker.download(upload_id)

    # -- writes -------------------------------------------------------------------------------------------------

    def set_state(self, idea_id, agent_state):
        """Gaia ideas have no agent state: "working" is a notice in the thread, the rest isn't shown."""
        if agent_state == "working":
            self.post(idea_id, "system", f"{self.display_name} is working on it.")

    def mark_created(self, idea_id):
        """Nothing to do: the plan's status (created) is the record, the idea stays open for more."""

    def post(self, idea_id, role, body, steps=None, plan_id=None, cost=None, screens=None):
        args = {"idea": str(idea_id), "body": body, "kind": AGENT_KINDS.get(role, role)}
        if steps:
            args["steps"] = steps
        if plan_id:
            args["ideaplanId"] = str(plan_id)
        if cost is not None:
            args["costUsd"] = cost
        if screens:
            args["screens"] = screens
        return self._tool("add_idea_message", args)

    def add_screen(self, idea_id, screen, png):
        """Upload a rendered screen (screens.py dict + its PNG bytes); returns the ref post() takes in screens."""
        version = 1 + sum(1 for s in self.idea(idea_id)["screens"] if s["key"] == screen["key"])
        up = self._tool("upload_idea_screen", {"idea": str(idea_id), "key": screen["key"],
                                               "png_base64": base64.b64encode(png).decode("ascii")})
        return {"uploadId": up["uploadId"], "key": screen["key"], "title": screen["title"], "version": version,
                "width": screen["width"], "height": screen["height"]}

    def add_plan(self, idea_id, body):
        """(plan id, version)."""
        plan = self._tool("add_idea_plan", {"idea": str(idea_id), "body": body})
        return plan["id"], plan.get("version")

    def set_plan_status(self, plan_id, status, error=None):
        return self._tool("update_idea_plan", {"plan": str(plan_id), "status": status, "error": error})

    def add_issue(self, plan_id, key, issue):
        """Record the issue created for a plan key (Gaia links it by the issue's id)."""
        return self._tool("add_idea_plan_issue", {
            "plan": str(plan_id), "key": key, "issue_number": int(issue["issueNumber"]),
            "project": issue.get("projectShortcode") or issue.get("project")})


def to_idea(data, user_id):
    """gaia_get_idea's reply in the shape worker, conversation and approval use."""
    user_id = str(user_id)
    messages, screens = [], []
    for m in data.get("messages") or []:
        kind = m.get("kind") or "message"
        author = str(m.get("createdUser") or "")
        role = ("agent" if author == user_id else "user") if kind == "message" else kind
        shots = _json(m.get("screens"), []) or []
        messages.append({"id": m["id"], "role": role, "body": m.get("body") or "", "author": author,
                         "steps": _json(m.get("steps"), None), "plan_id": m.get("ideaplanId"),
                         "cost_usd": m.get("costUsd"), "screens": shots})
        screens += [{"id": s.get("uploadId"), "key": s.get("key"), "title": s.get("title"),
                     "version": s.get("version")} for s in shots if isinstance(s, dict) and s.get("uploadId")]
    issues = data.get("planIssues") or []
    plans = [{"id": p["id"], "version": int(p.get("version") or 0), "status": p.get("status"),
              "error": p.get("error"), "body": _json(p.get("body"), {}),
              "issues": [{"key": i.get("key"), "number": i.get("issueNumber"),
                          "project_code": i.get("projectShortcode")} for i in issues if i.get("ideaplanId") == p["id"]]}
             for p in data.get("plans") or []]
    idea = {"id": data["id"], "title": data.get("title"), "status": data.get("status"),
            "owner": str(data.get("createdUser") or ""), "agent": user_id, "agent_state": "idle",
            "projectId": data.get("projectId")}
    return {"idea": idea, "messages": messages, "plans": plans, "screens": screens}
