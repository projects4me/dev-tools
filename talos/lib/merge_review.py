"""Routing a PR whose hand-off was deferred (`ai ship --defer-handoff`, outcome pr_opened) by its merge review: the
clean design review `ai ship` records in the PR body, read with `ai merge-review`. A clean PR in an auto-merge
milestone goes to the orchestrator that merges it; everything else goes to the reviewer, with a comment when the
review is missing or unreadable. The review is recorded before the PR opens, so nothing waits and nothing is stored.
"""

import html

from .ai_cli import ai_handoff, ai_merge_review
from .config import TalosError, log

CLEAN = "clean"
UNREVIEWED = "unreviewed"


class Router:
    """Hands an issue whose PR is open to its merger or the reviewer, from the PR's merge review."""

    def __init__(self, name, display_name, tracker, review=None, handoff=None, route=None, find_pr=None):
        self.name, self.display_name, self.tracker = name, display_name, tracker
        self.review = review or (lambda repo, pr: ai_merge_review(tracker.child_env(), repo, pr))
        self.handoff = handoff or (lambda issue, to=None: ai_handoff(tracker.child_env(), issue, to))
        # route(issue) -> (Gaia user id, name) of an orchestrator that merges a clean PR itself, or None (the reviewer)
        self.route = route or (lambda issue: None)
        # find_pr(repo, issue number) -> {"number", "merged", ...} or None: the issue's PR, for route_after_review
        self.find_pr = find_pr

    def _state(self, number, repo, pr):
        try:
            return self.review(repo, pr)
        except TalosError as e:
            log(f"#{number} merge review of {repo} PR {pr} not read: {e}", self.name)
            return None

    def route_pr(self, issue, repo, pr):
        """The PR is open with the hand-off deferred: hand the issue on now."""
        number = issue["number"]
        state = self._state(number, repo, pr)
        target = self.route(issue) if state == CLEAN else None
        if state != CLEAN:      # explain first, while the issue is still the agent's
            self._explain(number, repo, pr, state)
        self.handoff(number, to=target[0] if target else None)
        who = f"{target[1]} to merge" if target else "the reviewer"
        log(f"#{number} {repo} PR {pr} merge review {state or 'unreadable'}: handed to {who}", self.name)

    def _explain(self, number, repo, pr, state):
        """Why a PR goes to the reviewer instead of being merged automatically."""
        if state == UNREVIEWED:
            title, why = "no recorded design review", "records no clean design review"
        else:
            title, why = "merge review unreadable", "has a merge review that could not be read"
        try:
            self.tracker.comment(number, f"{self.display_name}: {title}",
                                 f"<p>PR {pr} in {html.escape(repo)} {why}, so it goes to the reviewer instead of "
                                 f"being merged automatically.</p>")
        except TalosError as e:     # the hand-off matters more than the note: it goes ahead without it
            log(f"#{number} comment failed: {e}", self.name)

    def route_after_review(self, issue, repo):
        """A reviewer's changes were addressed on an issue an orchestrator merges: when the PR is still open and its
        review is clean, hand it to the merger instead of leaving it with the reviewer. True when it did."""
        number = issue["number"]
        if not self.find_pr or not repo:
            return False
        target = self.route(issue)
        if not target:
            return False
        pr = self.find_pr(repo, number)
        if not pr or pr.get("merged") or self._state(number, repo, pr["number"]) != CLEAN:
            return False
        self.handoff(number, to=target[0])
        log(f"#{number} re-review addressed on {repo} PR {pr['number']}: handed to {target[1]} to merge", self.name)
        return True
