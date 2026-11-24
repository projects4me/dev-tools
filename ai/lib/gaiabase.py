"""Which failing gaia-dev checks of `ai ship` fail the same way on the base commit.

A check that fails on the base too (a stale vendor volume makes every branch's graph look stale) is not this
branch's drift: the gate reports it as known and goes on. The base runs on the baseline worktree of `ai test`
(`baseline.worktree`), under the same lock, so the branch being shipped is never touched. When the base can't be
checked (no merge-base, the lock held too long, the two would run on different vendor folders) every failure
stays the branch's.
"""

from . import baseline
from .common import AiError, base_branch, git, repo_dir, say, state_locked
from .gaiapack import gaia_dev, gaia_dev_command

LOCK_WAIT = 600   # a baseline suite run holds the lock for long; past this the failures stay the branch's


def failing_on_base(repo, names, run=gaia_dev, checkout=None):
    """{check: output} for the checks among `names` that fail on the branch's merge-base with its base.
    A check that can't run there is left out, so it stays a failure of the branch."""
    sha = git(repo, "merge-base", "HEAD", f"origin/{base_branch(repo)}")
    with state_locked(f"baseline-{repo}", wait_seconds=LOCK_WAIT,
                      waiting=f"waiting for another run's {repo} baseline"):
        root = (checkout or baseline.worktree)(repo, sha)
        if _runner(root) != _runner(repo_dir(repo)):
            raise AiError("the base checkout would run gaia-dev with another vendor folder than the branch")
        say(f"gaia-dev: running {', '.join(names)} on the base {sha[:8]} to tell known failures from new drift")
        found = {}
        for name in names:
            code, out = run(root, name.split(), timeout=300)
            if code:
                found[name] = normalized(out, root)
    return found


def _runner(root):
    """Local php or the container on the shared vendor volume (gaiapack.gaia_dev_command)."""
    cmd = gaia_dev_command(root, [])
    return cmd and cmd[0]


def normalized(output, root):
    """The output's non-empty lines, with the checkout's path taken out (the base runs in another folder)."""
    return {line.strip().replace(str(root), "<root>") for line in output.splitlines() if line.strip()}


def same_failure(output, root, base_lines):
    """True when the branch's failure has the same lines as the base's failure of that check. False when the check
    passed (or couldn't run) on the base, or the branch's failure says nothing to compare."""
    lines = normalized(output, root)
    return bool(lines) and base_lines is not None and lines == base_lines


def known_failures(repo, failed, run=gaia_dev, checkout=None):
    """The names among `failed` ({check: branch output}) that fail with the same output on the base."""
    if not failed:
        return []
    try:
        base = failing_on_base(repo, list(failed), run, checkout)
    except (AiError, OSError) as e:
        say(f"gaia-dev: could not run the failing checks on the base, so they count as this branch's: {e}")
        return []
    root = repo_dir(repo)
    return [name for name, out in failed.items() if same_failure(out, root, base.get(name))]
