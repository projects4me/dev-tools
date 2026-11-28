"""The real Settings and Ports for `aga` and `talos agamemnon undo`: Gaia as Agent Agamemnon, GitHub through
dev-tools/agamemnon, git in the issue's workspace, pushes through dev-tools/odysseus (Agamemnon has no git
identity of its own)."""

import dataclasses
import os
import re
import subprocess

from .. import db, docker_engine, docker_free, docker_state, pr_handoff, sessions, workspaces
from ..ai_cli import run_ai
from ..config import DEV_TOOLS, LOCAL_DIR, WORKSPACE, TalosError
from ..orchestrator import github
from ..orchestrator import state as store
from ..orchestrator.merge import Merger
from ..orchestrator.routes import merge_routes
from ..registry import load_agents
from ..tracker import Tracker, read_env_file
from . import branch_update, premise
from .tool import PUSH_REMOTE, Ports, Settings

AGENT = "agamemnon"


TEST_TIMEOUT = 1800     # seconds for `ai test <repo> --changed|--filter|--suite` before resolve-conflict, test-base or
                        # lint-base gives up


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


def route_pr(agent, tracker):
    """After a pushed merge, the issue on (pr_handoff.Router.route_pr): to this orchestrator to merge in one of its
    auto-merge milestones (orchestrator.routes.MergeRoute), else to the reviewer."""
    route = merge_routes([agent]).get(agent.orchestration.worker)
    router = pr_handoff.Router(agent.name, tracker, route=route)
    return lambda issue, repo, pr: router.route_pr(tracker.issue(issue), repo, pr)


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


def stale_containers(agent, tracker):
    """docker_free.plan for the worker's workspaces; OSError when Docker can't be asked."""
    import testenv      # installer/, put on the path by lib.registry
    return lambda: docker_free.plan(WORKSPACE, sessions.all_entries(), tracker, testenv, agent.docker_stale_hours)


def _repo_of(worker, tracker, issue):
    repo = worker.repo_for(tracker.issue(issue).get("project"))
    if not repo:
        raise TalosError(f"#{issue} is not in one of the worker's projects")
    return repo


def _open_pr(merger, repo, issue):
    pr = merger.find_pr(repo, issue)
    return pr if pr and not pr.get("merged") else None


def has_pr(worker, tracker, merger):
    """has_pr(issue): whether the issue has an open PR (the done-hold release updates it, else the workspace)."""
    return lambda issue: _open_pr(merger, _repo_of(worker, tracker, issue), issue) is not None


def update_pr(worker, tracker, merger, gh, agent_name):
    """update_pr(issue): merge the base into the issue's open PR on GitHub and start its CI re-run counts over;
    branch_update.UPDATED or CONFLICT. TalosError when it has no open PR or GitHub fails."""
    def update(issue):
        repo = worker.repo_for(tracker.issue(issue).get("project"))
        pr = _open_pr(merger, repo, issue) if repo else None
        if not pr:
            raise TalosError(f"#{issue} has no open PR to update")
        slug = workspaces.ai_common().REPOS[repo]
        head = gh("pr", "view", str(pr["number"]), "--repo", slug, "--json", "headRefOid",
                  "--jq", ".headRefOid").strip()
        result = branch_update.request(gh, slug, pr["number"], head)
        if result == branch_update.UPDATED:
            store.reset_ci_rerun(agent_name, issue, pr["number"])
        return result
    return update


def update_workspace(worker, tracker):
    """update_workspace(issue): merge origin/<base> into the issue's workspace branch, nothing pushed (a held issue
    with no PR yet); branch_update.UPDATED, CURRENT or CONFLICT. TalosError when it has no workspace."""
    def update(issue):
        repo = _repo_of(worker, tracker, issue)
        folder = workspaces.repo_folder(WORKSPACE, issue, repo)
        if not (folder / ".git").exists():
            raise TalosError(f"#{issue} has no workspace to update")
        return branch_update.merge_local(git, ody_git, folder, workspaces.base_branch(repo))[0]
    return update


FAILED_TEST = re.compile(r"^\s+FAIL \S", re.M)     # a failing test in `ai test`'s verdict lines ("  FAIL <name> ...")


def test_verdict(returncode, output):
    """Whether an `ai test --filter` run passed (True) or a test failed (False); None when it did not get to run the
    tests (a setup error, Docker down, no test slot): exit 2, or exit 1 with no failing test named."""
    if returncode == 0:
        return True
    if returncode == 1 and FAILED_TEST.search(output or ""):
        return False
    return None


def test_filter(folder, repo, pattern):
    """(passed, output) of `ai test <repo> --filter <pattern>` in the workspace that holds folder; TalosError when
    `ai test` did not get to run the tests, so that is never taken for a failing test."""
    return _ai_test(folder, repo, "--filter", pattern)


def lint(folder, repo):
    """(passed, output) of `ai test <repo> --suite lint` in the workspace that holds folder, like test_filter (each
    lint error is a FAIL line)."""
    return _ai_test(folder, repo, "--suite", "lint")


def _ai_test(folder, repo, *args):
    """(passed, output) of `ai test <repo> <args>`; raises TalosError when it did not run, never taken for a fail."""
    r = run_ai({**os.environ, **workspaces.env(folder.parent)}, "test", repo, *args, timeout=TEST_TIMEOUT)
    output = f"{r.stdout or ''}{r.stderr or ''}"
    passed = test_verdict(r.returncode, output)
    if passed is None:
        tail = " | ".join(output.splitlines()[-5:])
        raise TalosError(f"ai test did not run the tests (exit {r.returncode}): {tail}")
    return passed, output


def docker_ok(agent):
    """Whether Docker's verdict is ok now (the Docker holds' release condition)."""
    state = docker_state.read()
    return docker_state.verdict(state, agent.docker_memory_high_percent, agent.docker_memory_ok_percent) == docker_state.OK


def docker_packet(agent):
    """Docker's state with its verdict, for the triage packet."""
    state = docker_state.read()
    return {"verdict": docker_state.verdict(state, agent.docker_memory_high_percent, agent.docker_memory_ok_percent),
            "state": state}


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
                        premise=premise.load(), workspace_root=WORKSPACE,
                        docker_thresholds=(agent.docker_memory_high_percent, agent.docker_memory_ok_percent),
                        docker_hold_max_hours=agent.docker_hold_max_hours, hold_max_hours=agent.hold_max_hours)
    ports = Ports(tracker=tracker, merger=Merger(tracker.child_env, github.agamemnon_gh), gh=github.agamemnon_gh,
                  git=git, push=push, worker_state=lambda issue: sessions.get(worker.name, issue),
                  run_log=run_log(worker.name), in_flight=in_flight(worker.name), free_slot=free_slot,
                  ody_git=ody_git, test_changed=test_changed, route_pr=route_pr(agent, tracker),
                  update_worker_state=lambda issue, **fields: sessions.update(worker.name, issue, **fields),
                  docker_read=docker_state.read, docker_engine=lambda action: docker_engine.for_agent(agent, action),
                  stale_containers=stale_containers(agent, tracker), stop_containers=docker_free.stop,
                  base_checkout=lambda issue, repo: workspaces.base_checkout(WORKSPACE, DEV_TOOLS, issue, repo),
                  drop_checkout=lambda issue: workspaces.remove_checkout(WORKSPACE,
                                                                         workspaces.scratch_path(WORKSPACE, issue)),
                  test_filter=test_filter, lint=lint)
    return settings, ports
