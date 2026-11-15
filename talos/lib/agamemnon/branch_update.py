"""Bringing a branch up to date with its base: a merge of the base, never a rebase. On GitHub for a PR's branch (the
`update-branch` verb, the branch keeper and the `hold-until-done` release); in the issue's workspace for a branch
with no PR yet (the `update-workspace` verb and the `hold-until-done` release of such an issue)."""

from ..config import TalosError

UPDATED, CONFLICT, CURRENT = "updated", "conflict", "current"


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


def merge_local(git, ody_git, folder, base):
    """Merge origin/<base> into the workspace's branch (uncommitted work stays, as git merge keeps it): UPDATED,
    CURRENT when the branch already has it, or CONFLICT (the merge is aborted, the workspace as it was). The merge
    commit, when there is one, is by Agent Odysseus; nothing is pushed. Returns (result, head before, head after)."""
    git(folder, "fetch", "-q", "origin")
    before = git(folder, "rev-parse", "HEAD")
    if git(folder, "merge-base", "--is-ancestor", f"origin/{base}", "HEAD", check=False) is not None:
        return CURRENT, before, before
    if ody_git(folder, "merge", "--no-edit", f"origin/{base}", check=False) is None:
        git(folder, "merge", "--abort", check=False)
        return CONFLICT, before, before
    return UPDATED, before, git(folder, "rev-parse", "HEAD")
