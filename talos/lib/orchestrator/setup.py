"""Wires one orchestrator agent: its worker, its Gaia identity, the Orchestrator and its event source."""

import dataclasses

from .. import policy, runner, workspaces
from ..agamemnon import journal, premise, wiring
from ..agamemnon.holds import release_due
from ..agamemnon.triage import Triage, launcher
from ..config import WORKSPACE, TalosError, log
from ..hermes import Listener
from ..registry import load_agents
from ..tracker import Tracker
from . import github
from .adopt import Adopter
from .human_merges import HumanMergeSweeper
from .keeper import BranchKeeper
from .merge import Merger
from .merging import MergeSweeper
from .milestones import live
from .overlap import IssueFiles, OverlapGate
from .routes import merge_routes  # noqa: F401  (talos.py starts the workers with it)
from .worker import Orchestrator, _now


def _triage(agent, worker, tracker, me, owners, claude, dry_run):
    """Agamemnon's triage sessions (agaTriage), or None when they are off. Sessions a restart cut off go to the owner."""
    if not agent.orchestration.aga_triage:
        return None
    if not claude:
        raise TalosError("agaTriage needs the claude binary (claudeBin in talos/config.json)")
    triage = Triage(agent, tracker, me, owners, premise.load(),
                    launcher(claude, agent.name, agent.orchestration, WORKSPACE, runner.run),
                    run_log=wiring.run_log(worker.name), in_flight=wiring.in_flight(worker.name), dry_run=dry_run,
                    docker=lambda: wiring.docker_packet(agent))
    if not dry_run:
        triage.recover()
    return triage


def start(agent, cfg, dry_run, claude=None):
    """An orchestrator works the worker's projects as its own Gaia user; it listens to their status and assignee
    changes and sweeps on each (re)connect and every sweepMinutes."""
    worker = load_agents(agent.orchestration.worker)[0]
    if worker.kind != "issues":
        raise TalosError(f"worker '{worker.name}' is a {worker.kind} agent; an orchestrator feeds an issues agent")
    agent = dataclasses.replace(agent, projects=dict(worker.projects))
    tracker = Tracker(agent)
    worker_id = Tracker(worker).user_id()
    me = tracker.user_id()
    if me not in worker.accept_from:
        log(f"warning: {worker.name} does not accept assignments from {agent.display_name} (Gaia user {me}); "
            f"add it to acceptAssignmentsFrom in its agent.local.json", agent.name)
    owners = [str(o) for o in (agent.orchestration.owners or [worker.handoff_to]) if o]
    auto_merge = live(agent)
    merger = Merger(tracker.child_env, github.agamemnon_gh)
    merges = MergeSweeper(agent, worker, tracker, merger, me, owners,
                          dry_run=dry_run, is_held=lambda number: bool(journal.active_hold(number)),
                          milestones=auto_merge, worker_id=worker_id)
    adopter = Adopter(agent, worker, tracker, merger, worker_id, me, owners, auto_merge, dry_run=dry_run)
    ai_config = workspaces.ai_common().config()
    human = HumanMergeSweeper(agent, worker, tracker, github.agamemnon_gh, ai_config["agentLogin"],
                              ai_config.get("mergerLogin", "agent-agamemnon"), owners, dry_run=dry_run)
    keeper = BranchKeeper(agent, worker, tracker, github.agamemnon_gh, worker_id, me, owners,
                          lambda repo, issue: workspaces.branch_holder(WORKSPACE, repo, issue), dry_run=dry_run)
    overlap = OverlapGate(agent, worker, tracker, IssueFiles(tracker.child_env), worker_id, owners,
                         dry_run=dry_run)
    aga_merger = Merger(tracker.child_env, github.agamemnon_gh)
    holds = lambda: release_due(tracker, worker_id, me, _now(), agent.name, dry_run,    # noqa: E731
                                lambda: wiring.docker_ok(agent), owners[0] if owners else None,
                                wiring.update_pr(worker, tracker, aga_merger, github.agamemnon_gh, agent.name),
                                worker.dependency_done_statuses, wiring.has_pr(worker, tracker, aga_merger),
                                wiring.update_workspace(worker, tracker))
    triage = _triage(agent, worker, tracker, me, owners, claude, dry_run)
    merges.triage = keeper.triage = triage
    orchestrator = Orchestrator(agent, worker, tracker, worker_id, dry_run=dry_run, merges=merges, keeper=keeper,
                                human_merges=human, adopter=adopter, overlap=overlap, holds=holds, triage=triage)
    orchestrator.start()
    Listener(agent, tracker, [policy.ASSIGNEE_EVENT, policy.STATUS_EVENT], orchestrator.on_event,
             orchestrator.on_ready, cfg["hermesUrl"], cfg.get("nodeBin", "node"),
             cfg.get("reconnectSeconds", 10)).start()
    milestones = auto_merge()
    log(f"feeding {worker.display_name}: milestones {agent.orchestration.milestone_prefix}*, "
        f"at most {orchestrator.max_in_flight} in flight"
        + (f"; merging reviewed PRs in {', '.join(milestones)}" if milestones else "")
        + (" (dry run)" if dry_run else ""), agent.name)
    return orchestrator
