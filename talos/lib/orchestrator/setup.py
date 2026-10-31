"""Wires one orchestrator agent: its worker, its Gaia identity, the Orchestrator and its event source."""

import dataclasses

from .. import policy, workspaces
from ..agamemnon.holds import release_due
from ..config import WORKSPACE, TalosError, log
from ..hermes import Listener
from ..registry import load_agents
from ..tracker import Tracker
from . import github
from . import policy as orchestration
from . import state as store
from .keeper import BranchKeeper
from .merge import Merger
from .merging import MergeSweeper
from .overlap import IssueFiles, OverlapGate
from .worker import Orchestrator, _now


class MergeRoute:
    """Where the worker's CodeRabbit loop sends a clean issue when an orchestrator merges its milestone itself:
    route(issue) -> (the orchestrator's Gaia user id, its name), or None for the reviewer (other milestones, the
    orchestrator paused, or its Gaia user not readable yet: that is retried on the next clean issue)."""

    def __init__(self, agent, user_id, is_paused, find_pr=None):
        self.agent = agent
        self.find_pr = find_pr          # find_pr(repo, issue) -> the issue's PR (merge.Merger.find_pr)
        self.user_id = user_id          # user_id() -> the orchestrator's Gaia user id (TalosError when unreadable)
        self.is_paused = is_paused      # is_paused() -> True while the orchestrator is paused
        self._id = None

    def __call__(self, issue):
        if not self._id:
            try:
                self._id = self.user_id()
            except TalosError as e:
                log(f"#{issue.get('number')} goes to the reviewer: {self.agent.display_name}'s Gaia user is not "
                    f"readable ({e})", self.agent.name)
                return None
        target = orchestration.merge_target(issue, self.agent.orchestration.auto_merge_milestones, self._id,
                                            self.is_paused())
        return (target, self.agent.display_name) if target else None


def merge_routes(agents):
    """{worker name: MergeRoute} for the orchestrators that merge CodeRabbit-clean PRs themselves."""
    return {a.orchestration.worker: MergeRoute(a, Tracker(a).user_id, lambda a=a: bool(store.get(a.name).get("paused")),
                                               Merger(Tracker(a).child_env, github.agamemnon_gh).find_pr)
            for a in agents if a.kind == "orchestrator" and a.orchestration.auto_merge_milestones}


def start(agent, cfg, dry_run):
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
    merges = MergeSweeper(agent, worker, tracker, Merger(tracker.child_env, github.agamemnon_gh), me, owners,
                          dry_run=dry_run)
    keeper = BranchKeeper(agent, worker, tracker, github.agamemnon_gh, worker_id, me, owners,
                          lambda repo, issue: workspaces.branch_holder(WORKSPACE, repo, issue), dry_run=dry_run)
    overlap = OverlapGate(agent, worker, tracker, IssueFiles(tracker.child_env), dry_run=dry_run)
    holds = lambda: release_due(tracker, worker_id, me, _now(), agent.name, dry_run)    # noqa: E731
    orchestrator = Orchestrator(agent, worker, tracker, worker_id, dry_run=dry_run, merges=merges, keeper=keeper,
                                overlap=overlap, holds=holds)
    orchestrator.start()
    Listener(agent, tracker, [policy.ASSIGNEE_EVENT, policy.STATUS_EVENT], orchestrator.on_event,
             orchestrator.on_ready, cfg["hermesUrl"], cfg.get("nodeBin", "node"),
             cfg.get("reconnectSeconds", 10)).start()
    milestones = agent.orchestration.auto_merge_milestones
    log(f"feeding {worker.display_name}: milestones {agent.orchestration.milestone_prefix}*, "
        f"at most {orchestrator.max_in_flight} in flight"
        + (f"; merging CodeRabbit-clean PRs in {', '.join(milestones)}" if milestones else "")
        + (" (dry run)" if dry_run else ""), agent.name)
    return orchestrator
