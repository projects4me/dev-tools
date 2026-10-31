"""Shared paths, process helpers and the Projects4Me tracker client for dev-tools/ai."""

import html
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path

AI_DIR = Path(__file__).resolve().parent.parent
DEV_TOOLS = AI_DIR.parent
WORKSPACE = DEV_TOOLS.parent
# This machine's settings and state live in the dev-tools checkout, also when the code runs from the runtime copy
# (<workspace>/.dev-tools-runtime, see `p4m runtime`), so their paths don't depend on which copy is running.
LOCAL_DIR = WORKSPACE / "dev-tools"
# the same name as talos/lib/workspaces.py RUNTIME_NAME
RUNTIME_NAME = ".dev-tools-runtime"     # the runtime copy of dev-tools (installer/runtime.py)
ODYSSEUS = DEV_TOOLS / "odysseus" / "odysseus"
AGAMEMNON = DEV_TOOLS / "agamemnon" / "agamemnon"
P4M = DEV_TOOLS / "installer" / "p4m.py"
GAIA_MCP = DEV_TOOLS / "gaia-mcp"
STATE_DIR = LOCAL_DIR / "ai" / "state"
CONFIG_FILE = AI_DIR / "config.json"

REPOS = {"gaia": "Projects4Me/gaia", "prometheus": "Projects4Me/prometheus",
         "hermes": "Projects4Me/hermes", "ares": "Projects4Me/ares",
         "dev-tools": "Projects4Me/dev-tools", "pr4m": "Projects4Me/Pr4M"}
PROJECT_FOR_REPO = {"gaia": "A Gaia", "prometheus": "A Prometheus", "hermes": "A Hermes", "ares": "A Ares",
                    "dev-tools": "A Dev Tool", "pr4m": "A Gaia"}
# The branch PRs target; the service repos use dev, the tooling and the workspace root use main.
BASE_BRANCH = {"dev-tools": "main", "pr4m": "main"}
REPO_PATH = {"pr4m": "."}


class AiError(Exception):
    """A step failed; the message says which and why."""


def config():
    """config.json, overlaid by the engineer's config.local.json (git-ignored, written by `p4m configure`)."""
    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    local = LOCAL_DIR / "ai" / "config.local.json"
    if local.exists():
        with open(local, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def run_workspace():
    """The per-issue workspace Talos runs this session in (P4M_WORKSPACE, <workspace>/.runs/issue-N with the
    issue's repo as a git worktree), or None for interactive use in the workspace itself."""
    path = os.environ.get("P4M_WORKSPACE")
    return Path(path) if path else None


def checkout_state_file(stem, suffix):
    """A file in STATE_DIR owned by this checkout: `<stem>-issue-N<suffix>` in a Talos run (named after its
    run workspace), else `<stem><suffix>`. Talos runs issues in parallel, so test logs and results keyed only
    by repo would overwrite each other."""
    root = run_workspace()
    return STATE_DIR / f"{stem}{'-' + root.name if root else ''}{suffix}"


def repo_dir(repo):
    """The repo's folder: in the run's workspace when P4M_WORKSPACE is set. The workspace root repo (pr4m) is
    never a worktree, so it always resolves to WORKSPACE."""
    if repo not in REPOS:
        raise AiError(f"unknown repo '{repo}' (one of: {', '.join(REPOS)})")
    root = run_workspace() if repo != "pr4m" else None
    folder = ((root or WORKSPACE) / REPO_PATH.get(repo, repo)).resolve()
    if folder.name == RUNTIME_NAME:
        raise AiError(f"{repo} in {root or WORKSPACE} is the runtime copy ({folder}), the tooling this run uses; it "
                      f"only changes through `p4m runtime update`. Work on dev-tools needs its own dev-tools issue "
                      f"(Talos gives it its own worktree): propose one instead of changing the tooling in place.")
    return folder


def base_branch(repo):
    return BASE_BRANCH.get(repo, "dev")


def git_dir(repo):
    """The repo's real git directory: in a submodule `.git` is a file pointing elsewhere."""
    return Path(run(["git", "rev-parse", "--absolute-git-dir"], cwd=repo_dir(repo)))


def run(cmd, cwd=None, check=True, capture=True, env=None):
    """Run a command; return stdout (stripped). Raises AiError with stderr on failure."""
    result = subprocess.run(cmd, cwd=cwd, capture_output=capture, text=True, encoding="utf-8",
                            errors="replace", env=env)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise AiError(f"command failed ({result.returncode}): {' '.join(map(str, cmd))}\n{detail[-2000:]}")
    return (result.stdout or "").strip() if capture else ""


def succeeds(cmd, cwd=None):
    """Whether a command exits 0 (for yes/no checks like `git merge-base --is-ancestor`)."""
    return subprocess.run(cmd, cwd=cwd, capture_output=True).returncode == 0


def git(repo, *args, check=True):
    return run(["git", *args], cwd=repo_dir(repo), check=check)


def p4m(*args):
    """The p4m command line, run with this Python (no shell wrapper, so it works the same on Windows and Linux)."""
    return [sys.executable, str(P4M), *args]


def find_bash():
    """bash for the odysseus wrapper (a bash script; `sh` is dash on Ubuntu). On Windows that's Git Bash:
    System32\\bash.exe is WSL, often without a distro. Same lookup as installer/configure.py."""
    if os.name != "nt":
        return shutil.which("bash")
    candidates = []
    exec_path = subprocess.run(["git", "--exec-path"], capture_output=True, text=True).stdout.strip()
    if exec_path:
        git_root = Path(exec_path).resolve().parents[2]
        candidates += [git_root / "bin" / "bash.exe", git_root / "usr" / "bin" / "bash.exe"]
    candidates.append(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe")
    return next((str(c) for c in candidates if c.exists()), None)


def ody(repo, tool, *args, check=True):
    """Run git/gh as Agent Odysseus through dev-tools/odysseus (never the engineer's own identity)."""
    bash = find_bash()
    if not bash:
        raise AiError("bash not found (the odysseus wrapper needs it; on Windows, Git for Windows' Git Bash)")
    return run([bash, str(ODYSSEUS), tool, *args], cwd=repo_dir(repo), check=check)


def agamemnon(*args, check=True):
    """Run gh as Agent Agamemnon through dev-tools/agamemnon: it approves and merges agent PRs, never commits."""
    bash = find_bash()
    if not bash:
        raise AiError("bash not found (the agamemnon wrapper needs it; on Windows, Git for Windows' Git Bash)")
    return run([bash, str(AGAMEMNON), "gh", *args], cwd=WORKSPACE, check=check)


def now():
    return datetime.now().astimezone()


def say(msg=""):
    print(msg, flush=True)


def strip_html(text, limit=None):
    """Tracker HTML (descriptions, comments) as plain text: paragraphs and list items on their own lines."""
    text = re.sub(r"<(br|/p|/li|/h\d)\s*/?>", "\n", text or "", flags=re.I)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if limit and len(text) > limit:
        text = text[:limit].rstrip() + " …"
    return text


# --- Tracker (reuses the gaia MCP server's client, same credentials and logic) ---------------

_tools = None


def tracker():
    global _tools
    if _tools is None:
        sys.path.insert(0, str(GAIA_MCP))
        import server  # noqa: E402  (dev-tools/gaia-mcp/server.py)
        server.load_env_file(os.environ.get("GAIA_MCP_ENV", str(LOCAL_DIR / "gaia-mcp" / "gaia-mcp.env")))
        _tools = server.Tools(server.Gaia())
    return _tools


def tracker_call(method, **args):
    try:
        return getattr(tracker(), method)(args)
    except Exception as e:  # GaiaError and HTTP errors carry a readable message
        raise AiError(f"tracker {method} failed: {e}")


# --- Issue timers: actual elapsed time between checkpoints --------------------------------

def _timer_file():
    STATE_DIR.mkdir(exist_ok=True)
    return STATE_DIR / "timers.json"


def _filelock():
    """installer/filelock.py, the one lock module every dev tool shares."""
    sys.path.insert(0, str(DEV_TOOLS / "installer"))
    try:
        import filelock
    finally:
        sys.path.remove(str(DEV_TOOLS / "installer"))
    return filelock


def _devstack_lock():
    """A long browser check holds it; the OS drops it if the holder is killed."""
    filelock = _filelock()
    STATE_DIR.mkdir(exist_ok=True)
    return filelock.held(STATE_DIR / "devstack.lock", wait_seconds=1800,
                         on_wait=lambda p: say(f"waiting for the dev stack ({filelock.describe_holder(p)})"))


@contextmanager
def timers_locked():
    """Around every read-modify-write of timers.json: parallel Talos runs share the file."""
    filelock = _filelock()
    with ExitStack() as stack:
        try:
            stack.enter_context(filelock.held(STATE_DIR / "timers.lock"))
        except filelock.LockTimeout as e:
            raise AiError(str(e))
        yield


@contextmanager
def state_locked(name, wait_seconds=30, waiting=None):
    """Hold STATE_DIR/<name>.lock for the block, shared by parallel Talos runs. `waiting` is said once if
    another process holds it; a timeout raises AiError."""
    filelock = _filelock()
    with ExitStack() as stack:
        try:
            stack.enter_context(filelock.held(
                STATE_DIR / f"{name}.lock", wait_seconds=wait_seconds,
                on_wait=waiting and (lambda p: say(f"{waiting} ({filelock.describe_holder(p)})"))))
        except filelock.LockTimeout as e:
            raise AiError(str(e))
        yield


@contextmanager
def devstack_locked():
    """One browser check at a time against the shared dev stack (gaia-dev, Prometheus on :4200)."""
    with ExitStack() as stack:
        try:
            stack.enter_context(_devstack_lock())
        except _filelock().LockTimeout as e:
            raise AiError(str(e))
        yield


def load_timers():
    path = _timer_file()
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_timers(timers):
    tmp = _timer_file().with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(timers, f, indent=2)
    os.replace(tmp, _timer_file())          # readers never see a half-written file


def start_timer(issue, repo):
    with timers_locked():
        timers = load_timers()
        timers[str(issue)] = {"repo": repo, "checkpoint": now().isoformat(timespec="seconds")}
        save_timers(timers)


def issue_repo(issue):
    entry = load_timers().get(str(issue))
    return entry.get("repo") if entry else None
