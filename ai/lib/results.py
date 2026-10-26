"""The recorded result of the last `ai test` per repo, and the tree fingerprint that ties it to the code.

`ai ship` trusts a result only if it was a full, passing run of exactly the current tree.
"""

import hashlib
import json
import subprocess

from .common import STATE_DIR, git, repo_dir


def tree_fingerprint(repo, rev="HEAD"):
    """Identifies the exact working tree: `rev`'s tree plus uncommitted and untracked changes. It changes
    when the tree is committed, so `ai ship` checks it once, before committing, and resumes from its own
    progress after that."""
    d = repo_dir(repo)
    h = hashlib.sha1(git(repo, "rev-parse", f"{rev}^{{tree}}").encode())
    h.update(subprocess.run(["git", "-C", str(d), "diff", rev, "--binary"], capture_output=True).stdout)
    for f in sorted(git(repo, "ls-files", "-o", "--exclude-standard").splitlines()):
        p = d / f
        if p.is_file():
            h.update(f.encode() + b"\0" + p.read_bytes())
    return h.hexdigest()


def _file(repo):
    return STATE_DIR / f"test-last-{repo}.json"


def last(repo):
    path = _file(repo)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def record(repo, ok, full, lines, started_fingerprint=None):
    """Store a run. `started_fingerprint` is the tree as the run began: the test container bind-mounts the
    live checkout, so if the tree differs now, the run tested something else and is recorded as stale
    (not ok), which `ai ship` refuses. Returns True if the result is trustworthy."""
    now = tree_fingerprint(repo)
    stale = started_fingerprint is not None and started_fingerprint != now
    if stale:
        ok = False
        lines = lines + ["STALE: tree changed during the run; re-run"]
    STATE_DIR.mkdir(exist_ok=True)
    _file(repo).write_text(json.dumps({"fingerprint": started_fingerprint or now, "ok": ok, "full": full,
                                       "stale": stale, "lines": lines}, indent=2), encoding="utf-8")
    return not stale
