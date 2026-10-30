"""Wires one orchestrator agent: its worker, its Gaia identity, the Orchestrator and its event source."""

import dataclasses

from .. import policy
from ..config import TalosError, log
from ..hermes import Listener
from ..registry import load_agents
from ..tracker import Tracker
from .worker import Orchestrator


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
    orchestrator = Orchestrator(agent, worker, tracker, worker_id, dry_run=dry_run)
    orchestrator.start()
    Listener(agent, tracker, [policy.ASSIGNEE_EVENT, policy.STATUS_EVENT], orchestrator.on_event,
             orchestrator.on_ready, cfg["hermesUrl"], cfg.get("nodeBin", "node"),
             cfg.get("reconnectSeconds", 10)).start()
    log(f"feeding {worker.display_name}: milestones {agent.orchestration.milestone_prefix}*, "
        f"at most {orchestrator.max_in_flight} in flight" + (" (dry run)" if dry_run else ""), agent.name)
    return orchestrator
