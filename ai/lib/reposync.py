"""Keep a local checkout's base branch (dev, or main for the tooling) level with origin without ever switching
branches, so it is safe to run unattended (after a merge, on Talos' timer) while a checkout sits on an issue branch.

The git calls are in `_git`; `sync_checkout` decides what to do from the checkout's state."""

import subprocess

SYNCED, CURRENT, SKIPPED, FETCHED = "synced", "current", "skipped", "fetched"


def _git(folder, *args):
    """(exit code, output) of one git command in the folder; never raises."""
    result = subprocess.run(["git", *args], cwd=folder, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", stdin=subprocess.DEVNULL)
    return result.returncode, (result.stdout or result.stderr or "").strip()


def behind(folder, base):
    """How many commits origin/<base> has that the local <base> lacks, or None when either ref is missing."""
    code, out = _git(folder, "rev-list", "--count", f"{base}..origin/{base}")
    return int(out) if code == 0 and out.isdigit() else None


def sync_checkout(folder, base, fetch_only=False):
    """Fetch origin and fast-forward local <base>; returns (state, message). Only fast-forwards: a dirty tree or a
    diverged branch is skipped with a warning, and nothing is forced, reset or switched."""
    code, out = _git(folder, "fetch", "-q", "origin", "--prune")
    if code != 0:
        return SKIPPED, f"fetch failed: {out}"
    if fetch_only:
        return FETCHED, f"fetched; origin/{base} is {behind(folder, base) or 0} ahead of local {base}"
    before = behind(folder, base)
    if before == 0:
        return CURRENT, f"{base} is up to date"
    _, current = _git(folder, "symbolic-ref", "--short", "-q", "HEAD")
    if current == base:
        _, dirty = _git(folder, "status", "--porcelain", "--untracked-files=no")
        if dirty:
            return SKIPPED, f"{base} is checked out with uncommitted changes; not fast-forwarded"
        code, out = _git(folder, "merge", "--ff-only", "-q", f"origin/{base}")
    else:
        code, out = _git(folder, "fetch", "-q", "origin", f"{base}:{base}")   # fast-forward only; tree untouched
    if code != 0:
        return SKIPPED, f"{base} cannot be fast-forwarded (diverged or checked out elsewhere): {out.splitlines()[-1] if out else ''}"
    return SYNCED, f"{base} fast-forwarded by {before} commit(s)" if before else f"{base} fast-forwarded"
