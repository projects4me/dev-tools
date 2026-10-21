"""The runtime copy of dev-tools: a git worktree of origin/main at <workspace>/.dev-tools-runtime.

Talos, the `ai` wrapper the agents call, gaia-mcp and the agent configs run from it, so a dev-tools issue being
worked in the dev-tools checkout (edits, branch switches) never changes the tooling under a running agent. The
launchers (`ai/ai`, `talos/talos`, `installer/p4m`, ...) hand over to it when it exists; P4M_RUNTIME=off runs the
copy you call instead. This machine's settings and state stay in the dev-tools checkout for both copies.

The copy only moves when asked: `p4m runtime update` fast-forwards it to origin/main, refusing while a Talos run is
active, so a merged dev-tools PR takes effect only then (and in `talos run` after a restart).
"""

import shutil
import sqlite3
import subprocess
from pathlib import Path

RUNTIME_NAME = ".dev-tools-runtime"
REMOTE = "origin"
BRANCH = "main"
UPSTREAM = f"{REMOTE}/{BRANCH}"
NPM_DIRS = ("talos",)        # folders whose node_modules the copy needs (Talos' Hermes listener)
MARKER = "installer/runtime.py"  # a main without it predates LOCAL_DIR and would keep its state in the copy


class RuntimeCopyError(Exception):
    """A runtime step failed or was refused; the message says why."""


def runtime_dir(root: Path) -> Path:
    return root / RUNTIME_NAME


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    cp = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", stdin=subprocess.DEVNULL)
    if check and cp.returncode != 0:
        raise RuntimeCopyError(f"git {' '.join(args)} failed in {cwd}: {(cp.stderr or cp.stdout).strip()}")
    return cp.stdout.strip()


def active_runs(local_dir: Path) -> list[str]:
    """Talos runs that have started and not ended ('<agent> #<issue>'), from the shared talos.db."""
    db = local_dir / "talos" / "state" / "talos.db"
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT agent, issue FROM runs WHERE ended_at IS NULL ORDER BY id").fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" not in str(e):     # unreadable state must block an update, not read as "no runs"
            raise RuntimeCopyError(f"cannot read Talos runs from {db}: {e}") from e
        rows = []
    finally:
        con.close()
    return [f"{agent} #{issue}" for agent, issue in rows]


def exclude_from_workspace(root: Path) -> bool:
    """Keep the copy out of the workspace repo's `git status` (its local info/exclude; nothing to commit)."""
    if not (root / ".git").exists():
        return False
    path = Path(_git(root, "rev-parse", "--git-path", "info/exclude"))
    path = path if path.is_absolute() else root / path
    line = f"/{RUNTIME_NAME}/"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if line in text.splitlines():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(("" if not text or text.endswith("\n") else "\n") + line + "\n")
    return True


def install_npm(runtime: Path, say=print) -> None:
    """npm ci in the copy's folders that need node_modules (skipped, with a note, when npm is missing)."""
    npm = shutil.which("npm")
    for name in NPM_DIRS:
        folder = runtime / name
        if not (folder / "package-lock.json").exists():
            continue
        if not npm:
            say(f"npm not found: run `npm ci` in {folder} before `talos run`")
            continue
        cp = subprocess.run([npm, "ci", "--no-audit", "--no-fund"], cwd=folder, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
        if cp.returncode != 0:
            raise RuntimeCopyError(f"npm ci failed in {folder}: {(cp.stderr or cp.stdout).strip()[-1500:]}")
        say(f"npm ci in {name}/")


def setup(tools_dir: Path, root: Path, say=print) -> Path:
    """Create the copy (a detached worktree of origin/main) if it isn't there yet. Returns its path."""
    runtime = runtime_dir(root)
    if (runtime / ".git").exists():
        say(f"runtime copy present: {runtime}")
        return runtime
    _git(tools_dir, "fetch", "--quiet", REMOTE, BRANCH)
    if subprocess.run(["git", "cat-file", "-e", f"{UPSTREAM}:{MARKER}"], cwd=tools_dir, capture_output=True,
                      stdin=subprocess.DEVNULL).returncode != 0:
        raise RuntimeCopyError(f"{UPSTREAM} has no {MARKER} yet (the runtime copy change isn't merged): "
                               f"not creating a copy that would keep its own state")
    _git(tools_dir, "worktree", "prune")
    _git(tools_dir, "worktree", "add", "--detach", str(runtime), UPSTREAM)
    if exclude_from_workspace(root):
        say(f"added /{RUNTIME_NAME}/ to the workspace's .git/info/exclude")
    install_npm(runtime, say)
    say(f"runtime copy created at {runtime} ({describe(runtime)})")
    return runtime


def update(root: Path, local_dir: Path, force: bool = False, say=print) -> bool:
    """Fast-forward the copy to origin/main. Refuses while a Talos run is active (unless force), when the copy
    has local changes, or when origin/main isn't ahead of it. Returns whether it moved."""
    runtime = runtime_dir(root)
    if not (runtime / ".git").exists():
        raise RuntimeCopyError(f"no runtime copy at {runtime}: run `p4m runtime setup`")
    running = active_runs(local_dir)
    if running and not force:
        raise RuntimeCopyError(f"Talos run(s) active: {', '.join(running)}. Update when they end "
                            f"(--force if they are stale: a run Talos never closed)")
    if _git(runtime, "status", "--porcelain"):
        raise RuntimeCopyError(f"the runtime copy has local changes; it should never be edited ({runtime})")
    _git(runtime, "fetch", "--quiet", REMOTE, BRANCH)
    head, target = _git(runtime, "rev-parse", "HEAD"), _git(runtime, "rev-parse", UPSTREAM)
    if head == target:
        say(f"runtime copy up to date ({describe(runtime)})")
        return False
    if subprocess.run(["git", "merge-base", "--is-ancestor", head, target], cwd=runtime,
                      capture_output=True, stdin=subprocess.DEVNULL).returncode != 0:
        raise RuntimeCopyError(f"{UPSTREAM} is not ahead of the runtime copy ({head[:8]}); not a fast-forward")
    lock_changed = any(_git(runtime, "diff", "--name-only", head, target, "--", f"{name}/package-lock.json")
                       for name in NPM_DIRS)
    _git(runtime, "checkout", "--quiet", "--detach", target)
    if lock_changed or any(not (runtime / name / "node_modules").exists() for name in NPM_DIRS
                           if (runtime / name / "package-lock.json").exists()):
        install_npm(runtime, say)
    say(f"runtime copy updated {head[:8]} -> {describe(runtime)}")
    say("restart `talos run` (and the sim/Nestor servers) to load it")
    return True


def describe(runtime: Path) -> str:
    return _git(runtime, "log", "-1", "--format=%h %s")


def status(root: Path, local_dir: Path, say=print) -> None:
    runtime = runtime_dir(root)
    if not (runtime / ".git").exists():
        say(f"no runtime copy at {runtime}: run `p4m runtime setup` (the tools run from the dev-tools checkout)")
        return
    say(f"runtime copy: {runtime}")
    say(f"at:           {describe(runtime)}")
    behind = _git(runtime, "rev-list", "--count", f"HEAD..{UPSTREAM}", check=False) or "?"
    say(f"behind {UPSTREAM}: {behind} commit(s) (as of the last fetch)")
    running = active_runs(local_dir)
    say(f"Talos runs active: {', '.join(running) if running else 'none'}")
