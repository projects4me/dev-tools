"""Handing on an issue whose PR is open with the hand-off deferred (`ai ship --defer-handoff`, outcome pr_opened):
a PR in an auto-merge milestone goes to the orchestrator that merges it, everything else to the reviewer. The
orchestrator's merge gate (CI, the recorded test run, people, labels, ...) decides whether it merges; the design
review is advisory and not read here (adt-5046).
"""

from .ai_cli import ai_handoff
from .config import log


class Router:
    """Hands an issue whose PR is open to its merger or the reviewer, by the issue's milestone."""

    def __init__(self, name, tracker, handoff=None, route=None, find_pr=None):
        self.name, self.tracker = name, tracker
        self.handoff = handoff or (lambda issue, to=None: ai_handoff(tracker.child_env(), issue, to))
        # route(issue) -> (Gaia user id, name) of an orchestrator that merges the issue's milestone, or None (the
        # reviewer)
        self.route = route or (lambda issue: None)
        # find_pr(repo, issue number) -> {"number", "merged", ...} or None: the issue's PR, for route_after_review
        self.find_pr = find_pr

    def route_pr(self, issue, repo, pr):
        """The PR is open with the hand-off deferred: hand the issue on now."""
        number = issue["number"]
        target = self.route(issue)
        self.handoff(number, to=target[0] if target else None)
        who = f"{target[1]} to merge" if target else "the reviewer"
        log(f"#{number} {repo} PR {pr} opened: handed to {who}", self.name)

    def route_after_review(self, issue, repo):
        """A reviewer's changes were addressed on an issue an orchestrator merges: when the PR is still open, hand it
        to the merger instead of leaving it with the reviewer (its gate still fails on change requests, open threads
        or comments from people on the PR; feedback given only in Gaia comments is not seen there). True when it
        did."""
        number = issue["number"]
        if not self.find_pr or not repo:
            return False
        target = self.route(issue)
        if not target:
            return False
        pr = self.find_pr(repo, number)
        if not pr or pr.get("merged"):
            return False
        self.handoff(number, to=target[0])
        log(f"#{number} re-review addressed on {repo} PR {pr['number']}: handed to {target[1]} to merge", self.name)
        return True
