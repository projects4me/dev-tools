"""Which tests already fail on the base branch, by name, cached per base commit.

`ai test` uses this to tell a new failure from an old one without stashing and re-running by hand. The
baseline runs on a detached worktree of the base commit (dev-tools/ai/state/baseline/<repo>), so the
branch being worked on is never touched.
"""

import json
import shutil

from .common import AiError, STATE_DIR, git, p4m, run, say

BASELINE_DIR = STATE_DIR / "baseline"


def _file(repo):
    return STATE_DIR / f"baseline-{repo}.json"


def load(repo, sha):
    """{suite: [failing test names]} recorded for base commit `sha` (may hold only some suites)."""
    path = _file(repo)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("suites", {}) if data.get("sha") == sha else {}


def save(repo, sha, suite, failures):
    path = _file(repo)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if data.get("sha") != sha:
        data = {"sha": sha, "suites": {}}
    data["suites"][suite] = sorted(set(failures))
    STATE_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def worktree(repo, sha):
    """A detached worktree of the repo at `sha`, created or moved there; returns its path."""
    path = BASELINE_DIR / repo
    if path.exists() and not (path / ".git").exists():
        shutil.rmtree(path)
    if not path.exists():
        BASELINE_DIR.mkdir(parents=True, exist_ok=True)
        git(repo, "worktree", "prune")
        git(repo, "worktree", "add", "--detach", "-f", str(path), sha)
    else:
        run(["git", "-C", str(path), "checkout", "-q", "--detach", "-f", sha])
        run(["git", "-C", str(path), "clean", "-qfd"])
    say(f"baseline worktree {path} at {sha[:8]}")
    return path


def require(repo, sha, suites, runner):
    """Make sure the baseline has `suites` for `sha`, running the missing ones with runner(suite, src)."""
    have = load(repo, sha)
    missing = [s for s in suites if s not in have]
    if not missing:
        drop_container(repo)
        return have
    say(f"\nbaseline: running {', '.join(missing)} on {sha[:8]} to classify failures (one-off per base commit)")
    src = worktree(repo, sha)
    try:
        for suite in missing:
            result = runner(suite, src)
            if result.summary.startswith("no summary"):
                raise AiError(f"baseline run of {suite} produced no summary; see {result.log}")
            save(repo, sha, suite, result.failures)
            say(f"baseline {suite}: {len(result.failures)} failing on {sha[:8]}")
    finally:
        drop_container(repo)
    return load(repo, sha)


def drop_container(repo):
    """Remove the baseline worktree's test container: a baseline run is one-off per base commit. Also
    called when nothing is missing, because a hard-killed run (taskkill /F, a timed-out tool call) never
    reaches require's `finally` and leaves it running."""
    if (BASELINE_DIR / repo).exists():
        run(p4m("test", "clean", "--src", str(BASELINE_DIR / repo)), check=False)
