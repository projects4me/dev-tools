"""Taking owner-held PRs to the automatic merges (adt-5045). An issue in an auto-merge milestone that waits for its
owner's review, untouched since the worker handed it off, is handed to the orchestrator when its merge gate passes,
is still pending, or fails only on CI. The merge sweep (merging.py) then merges it,
or re-runs CI and, when CI stays red, hands it to the worker to fix (maxCiFixAttempts). Anything else (people on the
PR, a do-not-merge label, a conflict the keeper owns, ...) stays with the owner.

Loop guards: only the worker's hand-off arms it, so the merge sweep's hand-back (the orchestrator's own change) and
an owner's edit both leave the issue alone; one adoption per PR head (adopted_head in the worker's state), so only a
new push can bring it back; a run in flight or an unfinished blocking issue leaves it alone."""

import html

from .. import sessions
from ..config import TalosError, log
from ..dependency_gate import unmet_dependencies
from . import policy
from .milestones import auto_merge_pool


class Adopter:
    """Hands owner-held, mergeable-looking PRs of the worker's auto-merge milestones to the orchestrator."""

    def __init__(self, agent, worker, tracker, merger, worker_id, my_id, owners, milestones,
                 worker_state=sessions.get, update_worker_state=sessions.update, dry_run=False):
        self.agent, self.worker, self.tracker, self.merger = agent, worker, tracker, merger
        self.worker_id = str(worker_id) if worker_id else None
        self.my_id = str(my_id) if my_id else None
        self.owners = list(owners)
        self.milestones = milestones    # milestones() -> the auto-merge milestones now (milestones.live)
        self.worker_state, self.update_worker_state = worker_state, update_worker_state
        self.projects = list(worker.projects)
        self.dry_run = dry_run

    def sweep(self, issues, skip_statuses):
        """One pass. Returns a short note ("" when nothing was taken)."""
        milestones = self.milestones()
        if not (milestones and self.my_id and self.merger):
            return ""
        notes = []
        pool = auto_merge_pool(self.tracker, self.projects, issues, milestones,
                               self.agent.orchestration.milestone_prefix, skip_statuses)
        for issue in {i["number"]: i for i in pool}.values():
            if policy.adopt_skip_reason(issue, milestones, self.projects, self.owners, self.worker_id, {}):
                continue                # the cheap checks first: most issues never read the worker's state
            state = self.worker_state(self.worker.name, issue["number"])
            if policy.adopt_skip_reason(issue, milestones, self.projects, self.owners, self.worker_id, state):
                continue
            try:
                note = self._one(issue, state)
            except TalosError as e:     # GitHub, Gaia or ai failed: the next sweep looks again
                log(f"#{issue['number']} not taken to merge: {e}", self.agent.name)
                note = "error"
            if note:
                notes.append(f"#{issue['number']} {note}")
        return ", ".join(notes)

    def _one(self, issue, state):
        number = issue["number"]
        dependencies = issue.get("dependsOn") or []
        if dependencies and unmet_dependencies(dependencies, self.tracker.statuses_of(dependencies),
                                               self.worker.dependency_done_statuses):
            return ""
        repo = self.worker.repo_for(issue.get("project"))
        pr = self.merger.find_pr(repo, number) if repo else None
        if not pr or pr.get("merged"):
            return ""
        gate = self.merger.check(repo, pr["number"])
        if not policy.adoptable(gate, state.get("adopted_head")):
            return ""
        sha = gate["sha"]
        if self.dry_run:
            log(f"#{number} dry run, would take PR {repo}#{pr['number']} from the owner to merge "
                f"(gate {gate.get('state')})", self.agent.name)
            return "would take to merge (dry run)"
        # the head first, so a failure after the hand-off can never let the same head be taken twice; undone when
        # the hand-off itself fails
        self.update_worker_state(self.worker.name, number, adopted_head=sha)
        try:
            self.tracker.hand_off(number, policy.REVIEW_STATUS, self.my_id)
        except TalosError:
            self.update_worker_state(self.worker.name, number, adopted_head=state.get("adopted_head") or "")
            raise
        log(f"#{number} PR {repo}#{pr['number']} taken from the owner to merge (gate {gate.get('state')} at "
            f"{sha[:7]})", self.agent.name)
        self._comment(number, repo, pr["number"], gate)
        return "taken to merge"

    def _comment(self, number, repo, pr, gate):
        """Tell the owner why their review queue lost the issue (a failed comment never undoes the hand-off)."""
        state = gate.get("state")
        why = ("its merge gate passed" if state == "ok" else "its merge gate is still running" if state == "pending"
               else f"its merge gate fails only on CI, which {self.worker.display_name} can fix")
        try:
            self.tracker.comment(number, f"{self.agent.display_name}: taken to merge automatically",
                                 f"<p>PR {html.escape(repo)}#{pr} is in an auto-merge milestone and "
                                 f"{html.escape(why)} (head <code>{html.escape(gate['sha'][:7])}</code>), so "
                                 f"{html.escape(self.agent.display_name)} took it from your review queue to merge "
                                 f"it. Assign it back to yourself to keep it.</p>")
        except TalosError as e:
            log(f"#{number} comment failed: {e}", self.agent.name)
