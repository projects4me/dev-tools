"""Merges by a person: an agent PR (claude-issue-N by the Agent Odysseus account) the owner merged on GitHub leaves its
issue in_review, so the issue is set done here. PRs Agamemnon merged itself are finished by merging.MergeSweeper.
Separate from feeding the worker (worker.Orchestrator), which calls it once per sweep."""

import html
import json

from ..config import TalosError, log
from . import github, policy
from . import state as store


class HumanMergeSweeper:
    """Sets done the in_review issues of the owner whose agent PR a person merged."""

    def __init__(self, agent, worker, tracker, gh, author, merger, owners, dry_run=False):
        self.agent, self.worker, self.tracker = agent, worker, tracker
        self.gh = gh                    # gh(*args) -> stdout of `gh` as Agamemnon (github.agamemnon_gh)
        self.author = author            # the GitHub login that opens agent PRs (ai config agentLogin)
        self.bots = {author, merger}    # accounts that merge for the agents (agentLogin, mergerLogin): not people
        self.owners = {str(o) for o in owners}
        self.dry_run = dry_run
        self.projects = list(worker.projects)

    def sweep(self):
        """One pass over the owner's in_review issues. Returns a short note ("" when nothing was done)."""
        if not self.owners:
            return ""
        prefix, notes = self.agent.orchestration.milestone_prefix, []
        for project in self.projects:
            for issue in self.tracker.issues_in(project, prefix):
                if (str(issue.get("status") or "").lower() != policy.REVIEW_STATUS
                        or issue.get("assignee") not in self.owners):
                    continue
                try:
                    note = self._one(issue)
                except TalosError as e:     # GitHub or Gaia failed: the next sweep tries again
                    log(f"#{issue['number']} merge by a person not read: {e}", self.agent.name)
                    note = "error"
                if note:
                    notes.append(f"#{issue['number']} {note}")
        return ", ".join(notes)

    def _one(self, issue):
        number = issue["number"]
        repo = self.worker.repo_for(issue.get("project"))
        pr = self._merged_pr(repo, number) if repo else None
        if not pr or store.merge_recorded(self.agent.name, number, pr["number"]):
            return ""
        who = pr["merged_by"]
        if self.dry_run:
            log(f"#{number} dry run, PR {repo}#{pr['number']} merged by {who}: would set done", self.agent.name)
            return "would set done (dry run)"
        try:
            self.tracker.comment(number, f"{self.agent.display_name}: merged by a person",
                                 f"<p>PR {html.escape(repo)}#{pr['number']} merged by {html.escape(who)}; set done.</p>")
        except TalosError as e:     # the status change below ends the issue's sweeps, so a failed comment is not retried
            log(f"#{number} comment failed: {e}", self.agent.name)
        self.tracker.set_status(number, "done")
        log(f"#{number} PR {repo}#{pr['number']} merged by {who}; set done", self.agent.name)
        return f"merged by {who}, set done"

    def _merged_pr(self, repo, number):
        """{"number", "merged_by"} of the issue's agent PR a person merged, or None: no agent PR, one still open
        (or more than one), or only closed unmerged ones are left to the owner."""
        slug = github.known_repos().get(repo)
        if not slug:
            return None
        out = self.gh("pr", "list", "--repo", slug, "--head", f"claude-issue-{number}", "--author", self.author,
                      "--state", "all", "--json", "number,state,mergedBy")
        try:
            prs = json.loads(out or "[]")
        except ValueError:
            raise TalosError(f"gh pr list for claude-issue-{number} in {repo}: {out[-300:]}")
        if any(p.get("state") == "OPEN" for p in prs):
            return None
        merged = sorted((p for p in prs if p.get("state") == "MERGED"), key=lambda p: p["number"], reverse=True)
        if not merged:
            return None
        who = (merged[0].get("mergedBy") or {}).get("login")
        if not who or who in self.bots:     # no merger on record, or an agent account: not a person
            return None
        return {"number": merged[0]["number"], "merged_by": who}
