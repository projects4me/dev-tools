"""Which review a repo's agent PRs need before Agent Agamemnon merges them: CodeRabbit (`codeRabbitRepos`) or,
for a repo CodeRabbit doesn't review, the one named in `mergeReviewers` (config.json). Shared by `ai ship`, the
merge gate and Talos."""

from . import coderabbit
from .common import REPOS, AiError, config

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
