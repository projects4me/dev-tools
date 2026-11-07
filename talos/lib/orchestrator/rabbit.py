"""The shared CodeRabbit budget as Agamemnon uses it: every commit an agent pushes needs a review trigger, and the
triggers are limited per day (`ai rabbit budget` / `ai rabbit request`, ai/lib/rabbitbudget.py). Only what Agamemnon
sends is counted, never the owner's own activity on GitHub."""

from ..ai_cli import ai_json
from ..config import TalosError

FULL = "full"       # the budget's last tier: nothing but a person reviews now


class RabbitBudget:
    """`ai` as the keeper sees it. Every failure is a TalosError, which the keeper answers by handing the issue to
    the owner."""

    def __init__(self, env):
        self.env = env      # a callable returning the environment `ai` runs with

    def at_limit(self):
        """True when the daily limit is reached and no agent push gets a review."""
        return ai_json(self.env(), "rabbit", "budget").get("tier") == FULL

    def request(self, repo, pr, source, issue):
        """Ask for the review of the new head as a re-review; the budget's decision ("coderabbit", "human" or
        "absent" when the repo has no CodeRabbit)."""
        result = ai_json(self.env(), "rabbit", "request", repo, str(pr), "--source", source, "--rereview",
                         "--issue", str(issue))
        if "decision" not in result:
            raise TalosError(f"ai rabbit request {repo} {pr}: no decision in {result}")
        return result["decision"]
