"""Wires one events agent: its Gaia identity, the worker, and a Hermes listener for its trigger events in its
projects."""

from ..config import STATE_DIR
from ..hermes import Listener
from ..mcp import mcp_config
from ..tracker import Tracker
from .worker import EventWorker


def start(agent, cfg, claude, dry_run=False):
    """Start the agent's worker and listener; returns the worker. Hermes keeps no history, so an event sent while
    Talos was down is not seen: (re)connecting has nothing to reconcile."""
    tracker = Tracker(agent)
    codes = {}

    def project_codes():
        if not codes:
            codes.update(tracker.project_codes())
        return codes

    worker = EventWorker(agent, claude, {**tracker.child_env(), **agent.env}, tracker.project_ids(), project_codes,
                         mcp=mcp_config(STATE_DIR / agent.name / "mcp.json"), dry_run=dry_run)
    worker.start()
    Listener(agent, tracker, list(agent.triggers), worker.on_event, lambda _project_ids: None,
             cfg["hermesUrl"], cfg.get("nodeBin", "node"), int(cfg.get("reconnectSeconds", 10))).start()
    return worker
