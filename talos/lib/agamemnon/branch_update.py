"""Bringing a PR's branch up to date with its base on GitHub: a merge of the base (never a rebase), the same call
for the `update-branch` verb, the branch keeper and the `hold-until-done` release."""

from ..config import TalosError

UPDATED, CONFLICT = "updated", "conflict"


def request(gh, slug, pr_number, head):
    """Ask GitHub to merge the PR's base into its branch (expected_head_sha guards a moved head); UPDATED, or
    CONFLICT when GitHub says the merge conflicts. Any other failure is a TalosError."""
    try:
        gh("api", "-X", "PUT", f"repos/{slug}/pulls/{pr_number}/update-branch", "-f", f"expected_head_sha={head}")
    except TalosError as e:
        if "merge conflict" in str(e).lower():
            return CONFLICT
        raise
    return UPDATED
