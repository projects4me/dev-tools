"""The real Settings and Ports for `aga` and `talos agamemnon undo`: Gaia as Agent Agamemnon, GitHub through
dev-tools/agamemnon, git in the issue's workspace, pushes through dev-tools/odysseus (Agamemnon has no git
identity of its own)."""

import dataclasses
import os
import subprocess
from datetime import datetime, timedelta

from .. import bot_review as review_loop
from .. import db, sessions, workspaces
from ..ai_cli import run_ai
from ..config import LOCAL_DIR, WORKSPACE, TalosError
from ..orchestrator import github
from ..orchestrator.merge import Merger
from ..registry import load_agents
from ..tracker import Tracker, read_env_file
from . import premise
from .tool import PUSH_REMOTE, Ports, Settings

AGENT = "agamemnon"


TEST_TIMEOUT = 1800     # seconds for `ai test <repo> --changed` before resolve-conflict gives up


def _run(argv, cwd, args, env=None, check=True):
    cp = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                        stdin=subprocess.DEVNULL, env={**os.environ, **(env or {})})
    if cp.returncode != 0:
        if not check:
            return None
        raise TalosError(f"git {' '.join(args)} failed in {cwd}: {(cp.stderr or cp.stdout).strip()}")
    return cp.stdout.strip()


def git(cwd, *args, env=None, check=True):
    """`git <args>` in cwd; stdout stripped. With check=False a failure gives None; otherwise TalosError."""
    return _run(["git", *args], cwd, args, env, check)


def ody_git(cwd, *args, check=True):
    """`git <args>` as Agent Odysseus (dev-tools/odysseus: its name, email and signing key), like git()."""
    common = workspaces.ai_common()
    bash = common.find_bash()
    if not bash:
        raise TalosError("bash not found (the odysseus wrapper needs it; on Windows, Git for Windows' Git Bash)")
    return _run([bash, str(common.ODYSSEUS), "git", *args], cwd, args, check=check)


def test_changed(folder, repo):
    """(passed, output) of `ai test <repo> --changed` on the issue's workspace (P4M_WORKSPACE, as in its runs)."""
    r = run_ai({**os.environ, **workspaces.env(folder.parent)}, "test", repo, "--changed", timeout=TEST_TIMEOUT)
    return r.returncode == 0, f"{r.stdout or ''}{r.stderr or ''}"


def bot_review(worker, worker_id, tracker):
    """The PR back into the worker's CodeRabbit loop after a push, as a re-review does (bot_review.Watcher.reenter):
    waiting for CodeRabbit on the new head, then the clean PR is routed to the orchestrator to merge."""
    def reenter(issue, repo, pr):
        deadline = datetime.now().astimezone() + timedelta(minutes=worker.bot_review_wait_minutes)
        review_loop.reenter(worker.name, tracker, worker_id, issue, repo, pr, tracker.issue(issue).get("status"),
                            deadline)
    return reenter


def push(cwd, *refspecs):
    """git push to the odysseus remote as Agent Odysseus (dev-tools/odysseus)."""
    common = workspaces.ai_common()
    bash = common.find_bash()
    if not bash:
        raise TalosError("bash not found (the odysseus wrapper needs it; on Windows, Git for Windows' Git Bash)")
    try:
        common.run([bash, str(common.ODYSSEUS), "git", "push", PUSH_REMOTE, *refspecs], cwd=cwd)
    except common.AiError as e:
        raise TalosError(str(e))


def run_log(worker_name):
    def latest(issue):
        conn = db.connect()
        try:
            row = conn.execute("SELECT log_file FROM runs WHERE agent = ? AND issue = ? AND log_file IS NOT NULL "
                               "ORDER BY id DESC LIMIT 1", (worker_name, int(issue))).fetchone()
            return row["log_file"] if row else None
        finally:
            conn.close()
    return latest


def in_flight(worker_name):
    return lambda: sorted(issue for (agent, issue), st in sessions.all_entries().items()
                          if agent == worker_name and st.get("running"))


def free_slot():
    """A Gaia test slot nobody holds now, or None (p4m's slot locks, see installer/testslots.py)."""
    import filelock
    import testslots
    count = testslots.slot_count(read_env_file(LOCAL_DIR / "p4m.env"))
    locks = LOCAL_DIR / ".p4m"
    return next((k for k in range(1, count + 1) if not filelock.is_held(locks / testslots.lock_name(k))), None)


def build(agent_name=AGENT):
    """(Settings, Ports) for the orchestrator agent."""
    agent = load_agents(agent_name)[0]
    if agent.kind != "orchestrator":
        raise TalosError(f"'{agent_name}' is a {agent.kind} agent, not an orchestrator")
    worker = load_agents(agent.orchestration.worker)[0]
    agent = dataclasses.replace(agent, projects=dict(worker.projects))
    tracker = Tracker(agent)
    worker_id = Tracker(worker).user_id()
    settings = Settings(worker=worker, worker_id=worker_id, my_id=tracker.user_id(),
                        agent_name=agent.name, display_name=agent.display_name, orchestration=agent.orchestration,
                        premise=premise.load(), workspace_root=WORKSPACE)
    ports = Ports(tracker=tracker, merger=Merger(tracker.child_env, github.agamemnon_gh), gh=github.agamemnon_gh,
                  git=git, push=push, worker_state=lambda issue: sessions.get(worker.name, issue),
                  run_log=run_log(worker.name), in_flight=in_flight(worker.name), free_slot=free_slot,
                  ody_git=ody_git, test_changed=test_changed, bot_review=bot_review(worker, worker_id, tracker),
                  update_worker_state=lambda issue, **fields: sessions.update(worker.name, issue, **fields))
    return settings, ports
