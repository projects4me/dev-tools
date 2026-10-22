"""An approved plan -> Gaia issues: exactly what the plan says, in dependency order, through the gaia MCP client
as the agent, assigned to the idea's owner. Each created issue is recorded on the idea right away, so approving a
failed plan again creates only the ones still missing. No LLM involved. Each issue goes in the plan's milestone
(Claude-v1 unless the engineer asked otherwise; the gaia MCP client creates a milestone the project doesn't
have yet) and gets the PNGs of the screens it lists attached.

A plan left "creating" (Talos stopped mid-approval) is taken up again the same way on the next reconcile. One
window is left: if Talos dies between Gaia creating an issue and the idea recording it, carrying on creates that
one issue a second time (the failure message says to check Gaia first)."""

from ..config import log
from . import plan as plans
from . import policy, state


class Approval:
    def __init__(self, agent, client, tracker):
        self.agent, self.client, self.tracker = agent, client, tracker

    def run(self, idea_id, plan_id):
        data = self.client.idea(idea_id)
        idea = data["idea"]
        plan = next((p for p in data["plans"] if p["id"] == plan_id), None)
        if not plan or plan["status"] not in policy.RESUMABLE:
            return None                                  # handled already, or no longer approved
        self.client.set_plan_status(plan_id, "creating")
        issues = plan["body"]["issues"]
        refs = {i["key"]: f"{i['project_code']}-{i['number']}" for i in plan.get("issues") or []}
        latest = {}                                           # screen key -> id of its latest version
        for screen in data.get("screens") or []:
            latest[screen["key"]] = screen["id"]
        unattached = []
        try:
            for issue in plans.ordered(issues):
                if issue["key"] in refs:
                    continue
                made = self.tracker.create_issue(issue["project"], issue["subject"], self.description(issue, refs),
                                                 issue["type"], issue["priority"], issue["complexity"], idea["owner"],
                                                 issue.get("milestone") or plans.DEFAULT_MILESTONE)
                # attach before recording it: a recorded issue is skipped on retry, so its screens would never come
                unattached += self._attach(made["issueNumber"], issue.get("screens") or [], latest)
                self.client.add_issue(plan_id, issue["key"], made)
                refs[issue["key"]] = f"{made.get('projectShortcode')}-{made['issueNumber']}"
        except Exception as e:
            try:
                self.client.set_plan_status(plan_id, "failed", error=str(e))
                self._tell(idea, plan, refs, failed=str(e))
                self._tell_unattached(idea_id, unattached)
            except Exception as also:  # Gaia unreachable too: the plan stays "creating"; reconcile marks it failed
                log(f"idea #{idea_id}: creating plan v{plan['version']} stopped ({e}); recording that failed "
                    f"({also})", self.agent.name)
            return refs
        self.client.set_plan_status(plan_id, "created")
        self.client.mark_created(idea_id)
        self._tell(idea, plan, refs)
        self._tell_unattached(idea_id, unattached)
        return refs

    def _tell_unattached(self, idea_id, unattached):
        if unattached:
            self.client.post(idea_id, "system", "Some screens could not be attached (attach them by hand): "
                             + "; ".join(unattached))

    def _attach(self, number, keys, latest):
        """Attach the latest PNG of each screen key to the issue; returns what could not be attached."""
        missed = []
        for key in keys:
            try:
                self.tracker.attach_png(number, f"{key}.png", self.client.screen_png(latest[key]))
            except Exception as e:
                missed.append(f"{key}.png to #{number} ({e})")
                log(f"attach {key}.png to #{number}: {e}", self.agent.name)
        return missed

    @staticmethod
    def description(issue, refs):
        """The issue's description plus "Depends on" (plan keys become the numbers created for them; existing issues
        such as adt-4061 are kept as written) and "Screens" (the PNGs attached to it) lines."""
        depends = [refs.get(d, d) for d in issue["dependsOn"]]
        line = f"<p><strong>Depends on:</strong> {', '.join(depends)}</p>" if depends else ""
        shots = issue.get("screens") or []
        if shots:
            line += f"<p><strong>Screens:</strong> {', '.join(f'{k}.png' for k in shots)}</p>"
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
