"""Bring an unshipped issue branch (no commits of its own) up to date with its base before `ai ship` commits.

When the base moved and the tree has uncommitted work, `git merge` refuses if a file changed on both sides. For a
branch with no commits of its own that is a fast-forward, so: stash (named, worktrees share the stash list),
`merge --ff-only`, pop. Branches that already have commits keep the old rule (merge the base, don't rebase).
Intent-to-add files (`git add -N`) can't be stashed ("Entry not uptodate"), so they are set back to untracked for the
update, which leaves them in the tree, and marked intent-to-add again afterwards.

The git calls are in `_git`; `update_unshipped` decides from the branch's state."""

import subprocess
from pathlib import Path

UP_TO_DATE, UPDATED, HAS_COMMITS, CONFLICT = "up-to-date", "updated", "has-commits", "conflict"


def _git(folder, *args):
    """(exit code, output) of one git command in the folder; never raises."""
    result = subprocess.run(["git", *args], cwd=folder, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", stdin=subprocess.DEVNULL)
    return result.returncode, ((result.stdout or "") + (result.stderr or "")).strip()


def _is_ancestor(folder, ancestor, descendant):
    return _git(folder, "merge-base", "--is-ancestor", ancestor, descendant)[0] == 0


def conflicted_files(folder):
    return _git(folder, "diff", "--name-only", "--diff-filter=U")[1].splitlines()


def intent_to_add_files(folder):
    """The files marked intent-to-add (`git add -N`): the only ones the tree-vs-index diff reports as added."""
    return [f for f in _git(folder, "diff", "--name-only", "--diff-filter=A", "-z")[1].split("\0") if f]


def update_unshipped(folder, base, tag):
    """Fast-forward HEAD to origin/<base> keeping uncommitted work. Returns (state, detail):
    UP_TO_DATE (nothing to do), UPDATED (tree changed: re-test), HAS_COMMITS (own commits, caller refuses as
    before) or CONFLICT (detail = the conflicted files; the conflict is left in the tree, the stash is kept)."""
    target = f"origin/{base}"
    if _is_ancestor(folder, target, "HEAD"):
        return UP_TO_DATE, ""
    if not _is_ancestor(folder, "HEAD", target):
        return HAS_COMMITS, ""
    intents = intent_to_add_files(folder)
    if intents:
        code, out = _git(folder, "reset", "-q", "--", *intents)
        if code != 0:
            return CONFLICT, f"could not unmark the intent-to-add files {', '.join(intents)}: {out}"
    state, detail = _fast_forward(folder, target, f"ai-ship-{tag}")
    remaining = [f for f in intents if (Path(folder) / f).exists()]
    if remaining:
        code, out = _git(folder, "add", "-N", "--", *remaining)
        if code != 0:
            detail = "; ".join(filter(None, [detail, f"could not mark {', '.join(intents)} intent-to-add again "
                                                      f"(they are untracked now): {out}"]))
    return state, detail


def _fast_forward(folder, target, message):
    """Stash tracked changes, `merge --ff-only` to target, re-apply; the states of update_unshipped."""
    dirty = bool(_git(folder, "status", "--porcelain", "--untracked-files=no")[1])
    if dirty:
        code, out = _git(folder, "stash", "push", "-m", message)
        if code != 0:
            return CONFLICT, f"could not stash before updating: {out}"
        sha = _git(folder, "rev-parse", "stash@{0}")[1]
    code, out = _git(folder, "merge", "--ff-only", "-q", target)
    if code != 0:
        if dirty:
            _git(folder, "stash", "pop", "-q")
        return CONFLICT, f"could not fast-forward to {target}: {out}"
    if not dirty:
        return UPDATED, ""
    code, out = _git(folder, "stash", "apply", "-q", sha)
    files = conflicted_files(folder)
    if code != 0 or files:
        return CONFLICT, ", ".join(files) or out
    _drop_stash(folder, sha)
    return UPDATED, ""


def _drop_stash(folder, sha):
    """Drop our stash entry, found by its commit (another session may have pushed since)."""
    for line in _git(folder, "stash", "list", "--format=%H %gd")[1].splitlines():
        entry, _, ref = line.partition(" ")
        if entry == sha:
            _git(folder, "stash", "drop", "-q", ref)
            return
