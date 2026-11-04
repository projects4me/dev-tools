"""Which review a repo's agent PRs need before Agent Agamemnon merges them: CodeRabbit (`codeRabbitRepos`) or,
for a repo CodeRabbit doesn't review, the one named in `mergeReviewers` (config.json). Shared by `ai ship`, the
merge gate and Talos."""

import json

from . import coderabbit, reviewrecord
from .common import REPOS, AiError, config, ody, say

DESIGN_REVIEW = "design-review"
KINDS = (DESIGN_REVIEW,)


def merge_reviewers():
    """`mergeReviewers` as {repo: kind}; an unknown repo or kind, or a repo also in codeRabbitRepos, is an error."""
    reviewers = config().get("mergeReviewers", {})
    unknown = [r for r in reviewers if r not in REPOS]
    if unknown:
        raise AiError(f"mergeReviewers names unknown repos: {', '.join(unknown)} (known: {', '.join(REPOS)})")
    kinds = sorted({k for k in reviewers.values() if k not in KINDS})
    if kinds:
        raise AiError(f"mergeReviewers names unknown reviewers: {', '.join(kinds)} (known: {', '.join(KINDS)})")
    both = [r for r in reviewers if r in coderabbit.enabled_repos()]
    if both:
        raise AiError(f"{', '.join(both)} in both codeRabbitRepos and mergeReviewers: pick one review per repo")
    return dict(reviewers)


def merge_reviewer(repo):
    """The repo's reviewer kind from mergeReviewers, or None (CodeRabbit or no automated review)."""
    return merge_reviewers().get(repo)


def review_state(repo, pr):
    """For a repo in mergeReviewers: clean when the PR body records the design review, else unreviewed. Nothing
    changes between pushes that the recorded line doesn't already bind to the tree (see `ai ship`)."""
    body = ody(repo, "gh", "api", f"repos/{REPOS[repo]}/pulls/{pr}", "--jq", ".body // empty")
    if reviewrecord.REVIEW_LINE in body:
        return {"state": "clean"}
    return {"state": "unreviewed", "detail": "the PR body records no clean design review"}


def cmd_rabbit(args):
    """`ai rabbit`: CodeRabbit's state for the PR, or for a mergeReviewers repo the recorded design review's."""
    if merge_reviewer(args.repo) != DESIGN_REVIEW:
        return coderabbit.cmd_rabbit(args)
    result = review_state(args.repo, args.pr)
    say(json.dumps(result) if args.json else result["state"] + (f" - {result['detail']}" if "detail" in result else ""))
    return coderabbit.EXIT_CODES[result["state"]]
