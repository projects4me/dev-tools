"""Per-issue workspaces: each Talos run works in <workspace>/.runs/issue-N, so runs never switch branches under
each other.

The folder holds the issue's repo as a git worktree on claude-issue-N, the tooling linked in (a junction on
Windows, a symlink elsewhere) and the workspace CLAUDE.md and .claude, so `dev-tools/ai/ai`, the prompts and the
allowed tools work unchanged with the folder as cwd. Talos names it in P4M_WORKSPACE, which `ai` follows to find
the repo. A workspace is sticky: it stays (with any uncommitted work) until it is removed when the issue is done.
"""

import os
import shutil
import subprocess
from pathlib import Path

RUNS_DIR = ".runs"
RUNTIME_NAME = ".dev-tools-runtime"     # where the `ai` wrapper looks for the tooling (installer/runtime.py)
BASE_BRANCH = {"dev-tools": "main"}
REPO_PATH = {}                           # repo name -> folder in the workspace, when they differ
SHARED = ("CLAUDE.md",)                  # copied from the workspace
LINKED = (".claude",)                    # linked to the workspace's folder (skills, agents, settings)


class WorkspaceError(Exception):
    """A workspace step failed; the message says which and why."""


def branch_name(issue):
    return f"claude-issue-{issue}"


def path(root, issue):
    return Path(root) / RUNS_DIR / f"issue-{issue}"


def _git(cwd, *args, check=True):
    cp = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", stdin=subprocess.DEVNULL)
    if check and cp.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args)} failed in {cwd}: {(cp.stderr or cp.stdout).strip()}")
    return cp.stdout.strip()


def link(target, link_path):
    """A directory link: a junction on Windows (no admin or developer mode needed), a symlink elsewhere."""
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link_path))
    else:
        os.symlink(str(target), str(link_path), target_is_directory=True)


def is_link(p):
    p = Path(p)
    if p.is_symlink():
        return True
    if os.name == "nt":
        try:
            return bool(os.readlink(p))  # readlink reads junctions too (Python 3.8+)
        except (OSError, ValueError):
            return False
    return False


def unlink(p):
    """Remove a link without touching what it points to."""
    if os.name == "nt" and not Path(p).is_symlink():
        os.rmdir(p)                      # a junction: rmdir removes the link, not the target
    else:
        os.unlink(p)


def ensure(root, tools, issue, repo):
    """Create the issue's workspace if needed (idempotent) and return its path.

    root: the workspace (holds the repos); tools: the dev-tools copy Talos runs from (the runtime copy).
    The worktree is made on claude-issue-N: an existing branch is reused, a new one starts at origin/<base>.
    git refuses a branch checked out in another worktree, which keeps two runs off one branch.
    """
    root, tools = Path(root), Path(tools)
    folder = path(root, issue)
    folder.mkdir(parents=True, exist_ok=True)
    ignore = folder.parent / ".gitignore"
    if not ignore.exists():              # keep the runs out of the workspace repo's `git status`
        ignore.write_text("*\n", encoding="utf-8")
    name = REPO_PATH.get(repo, repo)
    if not (folder / name / ".git").exists():
        _add_worktree(root / name, folder / name, issue, BASE_BRANCH.get(repo, "dev"))
    # The `ai` wrapper hands over to <its folder>/../../.dev-tools-runtime when that exists, so this link sends
    # the run's `dev-tools/ai/ai` to the tooling Talos runs from, also when dev-tools/ is the issue's worktree.
    links = {RUNTIME_NAME: tools, **{n: root / n for n in LINKED}}
    if name != "dev-tools":
        links["dev-tools"] = tools
    for link_name, target in links.items():
        if target.exists() and not os.path.lexists(folder / link_name):
            link(target, folder / link_name)
    for file_name in SHARED:
        if (root / file_name).exists():
            shutil.copyfile(root / file_name, folder / file_name)
    return folder


def _add_worktree(main, dest, issue, base):
    if not (main / ".git").exists():
        raise WorkspaceError(f"no git checkout at {main}")
    branch = branch_name(issue)
    _git(main, "fetch", "-q", "origin")
    _git(main, "worktree", "prune")
    if _git(main, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False):
        _git(main, "worktree", "add", str(dest), branch)
    else:
        _git(main, "worktree", "add", "-b", branch, str(dest), f"origin/{base}")


def remove(root, issue, force=False):
    """Remove the issue's workspace: the links (never their targets), the worktrees (their branches stay) and the
    folder. Refuses when a worktree has uncommitted changes, unless force. Returns whether anything was there."""
    root = Path(root)
    folder = path(root, issue)
    if not folder.exists():
        return False
    worktrees = [c for c in folder.iterdir() if not is_link(c) and (c / ".git").is_file()]
    if not force:
        dirty = [w.name for w in worktrees if _git(w, "status", "--porcelain")]
        if dirty:
            raise WorkspaceError(f"uncommitted changes in {', '.join(dirty)} under {folder} (force to discard them)")
    for worktree in worktrees:           # worktrees first, so a failure leaves the workspace whole for a retry
        _git(root / worktree.name, "worktree", "remove", *(["--force"] if force else []), str(worktree))
    for child in folder.iterdir():       # links before the folder, so removing it can't reach into their targets
        if is_link(child):
            unlink(child)
    shutil.rmtree(folder)
    return True


def env(folder):
    """What a run in the workspace needs in its environment: `ai` resolves the repos from P4M_WORKSPACE."""
    return {"P4M_WORKSPACE": str(folder)}
