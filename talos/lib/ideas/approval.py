"""An approved plan -> Gaia issues: exactly what the plan says, in dependency order, through the gaia MCP client
as the agent, assigned to the idea's owner. Each created issue is recorded on the idea right away, so approving a
failed plan again creates only the ones still missing. No LLM involved.

One window is left: if Talos dies between Gaia creating an issue and the idea recording it, approving again
creates that one issue a second time (the failure message says to check Gaia first)."""

from ..config import log
from . import plan as plans
from . import state


class Approval:
    def __init__(self, agent, client, tracker):
        self.agent, self.client, self.tracker = agent, client, tracker

    def run(self, idea_id, plan_id):
        data = self.client.idea(idea_id)
        idea = data["idea"]
        plan = next((p for p in data["plans"] if p["id"] == plan_id), None)
        if not plan or plan["status"] != "approved":
            return None                                  # handled already, or no longer approved
        self.client.set_plan_status(plan_id, "creating")
        issues = plan["body"]["issues"]
        refs = {i["key"]: f"{i['project_code']}-{i['number']}" for i in plan.get("issues") or []}
        try:
            for issue in plans.ordered(issues):
                if issue["key"] in refs:
                    continue
                made = self.tracker.create_issue(issue["project"], issue["subject"], self.description(issue, refs),
                                                 issue["type"], issue["priority"], issue["complexity"], idea["owner"])
                self.client.add_issue(plan_id, issue["key"], made)
                refs[issue["key"]] = f"{made.get('projectShortcode')}-{made['issueNumber']}"
        except Exception as e:
            try:
                self.client.set_plan_status(plan_id, "failed", error=str(e))
                self._tell(idea, plan, refs, failed=str(e))
            except Exception as also:  # Gaia unreachable too: the plan stays "creating"; reconcile marks it failed
                log(f"idea #{idea_id}: creating plan v{plan['version']} stopped ({e}); recording that failed "
                    f"({also})", self.agent.name)
            return refs
        self.client.set_plan_status(plan_id, "created")
        self.client.mark_created(idea_id)
        self._tell(idea, plan, refs)
        return refs

    @staticmethod
    def description(issue, refs):
        """The issue's description plus a "Depends on" line: plan keys become the numbers created for them;
        existing issues (e.g. adt-4061) are kept as written."""
        depends = [refs.get(d, d) for d in issue["dependsOn"]]
        line = f"<p><strong>Depends on:</strong> {', '.join(depends)}</p>" if depends else ""
        return issue["description"].strip() + line

    def _tell(self, idea, plan, refs, failed=None):
        """A line in the idea for the engineer, and a note the agent gets at the start of its next turn."""
        listed = ", ".join(f"{ref} ({key})" for key, ref in refs.items()) or "none"
        total = len(plan["body"]["issues"])
        if failed:
            chat = (f"Created {len(refs)} of {total} issues from plan v{plan['version']}, then it stopped: {failed}. "
                    f"Approve again to create the rest.")
            note = f"The engineer approved plan v{plan['version']}; creating it stopped ({failed}). Created: {listed}."
        else:
            chat = f"Created {len(refs)} issue(s) from plan v{plan['version']}: {listed}."
            note = f"The engineer approved plan v{plan['version']} and these issues were created: {listed}."
        self.client.post(idea["id"], "system", chat)
        state.update(self.agent.name, idea["id"], note=note)
