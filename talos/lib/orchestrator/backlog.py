"""The CodeRabbit backlog: when the daily limit sent an agent PR to the human reviewer, the shared ledger recorded it as
deferred (ai/lib/rabbitbudget.py). Once there is credit again the PR still gets a CodeRabbit review to help the person;
the issue stays with them (it is not reassigned and the Talos loop does not start). Oldest first, while the tier allows
it, and the first answer from the budget that is not CodeRabbit ends the pass."""

import html
import json

from ..config import TalosError, log
from . import github, policy
from .rabbit import FULL

SOURCE = "backlog"


class BacklogSender:
    """Sends the deferred PRs of the worker's repos to CodeRabbit as the budget allows."""

    def __init__(self, agent, tracker, gh, rabbit, dry_run=False):
        self.agent, self.tracker, self.gh, self.rabbit = agent, tracker, gh, rabbit
        self.dry_run = dry_run

    def sweep(self):
        """One pass over the deferred entries. Returns a short note ("" when nothing was sent)."""
        try:
            budget = self.rabbit.budget()
        except TalosError as e:
            log(f"CodeRabbit backlog not read: {e}", self.agent.name)
            return ""
        deferred = sorted(budget.get("deferred") or [], key=lambda d: d.get("at") or "")
        if not deferred or budget.get("tier") == FULL:
            return ""
        slugs = github.known_repos()
        sent = []
        for entry in deferred:
            repo, pr, number = entry.get("repo"), entry.get("pr"), entry.get("issue")
            if repo not in slugs or number is None or not self._still_waiting(slugs[repo], repo, pr, number):
                continue
            if self.dry_run:
                log(f"#{number} dry run, would send PR {repo}#{pr} to CodeRabbit from the backlog", self.agent.name)
                sent.append(f"#{number} would be sent (dry run)")
                break
            try:
                decision = self.rabbit.request(repo, pr, SOURCE, number, files=int(entry.get("files") or 0),
                                               rereview=False)
            except TalosError as e:
                log(f"#{number} PR {repo}#{pr} not sent to CodeRabbit: {e}", self.agent.name)
                break
            if decision != "coderabbit":     # the tier does not allow it (yet): the later ones would not fit either
                break
            log(f"#{number} PR {repo}#{pr} sent to CodeRabbit from the backlog", self.agent.name)
            self._comment(number, repo, pr)
            sent.append(f"#{number} PR {repo}#{pr}")
        return ", ".join(sent)

    def _still_waiting(self, slug, repo, pr, number):
        """False (nothing is spent) when the PR is closed or merged, or its issue is no longer in_review."""
        try:
            out = self.gh("pr", "view", str(pr), "--repo", slug, "--json", "state")
            state = json.loads(out or "{}").get("state")
            issue = self.tracker.issue(number)
        except (TalosError, ValueError) as e:
            log(f"#{number} PR {repo}#{pr} not checked for the backlog: {e}", self.agent.name)
            return False
        return state == "OPEN" and str(issue.get("status") or "").lower() == policy.REVIEW_STATUS

    def _comment(self, number, repo, pr):
        try:
            self.tracker.comment(number, f"{self.agent.display_name}: CodeRabbit review requested",
                                 f"<p>CodeRabbit review requested now that credit is available for PR "
                                 f"{html.escape(str(repo))}#{pr}; the issue stays with you.</p>")
        except TalosError as e:
            log(f"#{number} comment failed: {e}", self.agent.name)
