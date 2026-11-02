"""An events agent's work queue: each new trigger event becomes one Claude run, one at a time (runs share the
workspace root, there is no issue to give them a worktree).

An event is claimed in talos.db (store.claim) as it arrives, so the same (event, resource) never runs twice:
Hermes re-sends on reconnects, and an agent in several projects may hear one event more than once. A run stopped
by a Claude usage limit pauses the queue and runs again after it; any other ending is final and kept in
event_runs for `talos status`. Nothing is handed off: the agent has no reviewer to hand to.
"""

import queue
import threading
import uuid
from datetime import datetime

from .. import policy as run_policy
from .. import runner, workspaces
from .. import runs as run_log
from ..config import DEV_TOOLS, log
from ..usage_pause import UsagePause
from . import policy, store


def _now():
    return datetime.now().astimezone()


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _runtime_copy():
    """The runtime copy Talos runs from (`p4m runtime`), or None when it runs from a checkout (P4M_RUNTIME=off)."""
    return DEV_TOOLS if DEV_TOOLS.name == workspaces.RUNTIME_NAME else None


class EventWorker:
    def __init__(self, agent, claude, env, project_ids, project_codes, mcp=None, run_on=None, dry_run=False,
                 store_path=None):
        self.agent, self.claude, self.env, self.mcp = agent, claude, env, mcp
        self.project_ids = project_ids          # project name -> id
        self.project_codes = project_codes      # () -> project name -> short code (read on first use)
        self.runner = run_on or runner.LocalRunner()
        self.dry_run = dry_run
        self.store_path = store_path            # None = state/talos.db
        self._queue = queue.Queue()
        self._pause = UsagePause()

    # -- intake -------------------------------------------------------------------------------------------------

    def on_event(self, envelope):
        key = policy.key_for(envelope, self.agent, self.project_ids)
        if not key:
            return
        event, resource_id, _ = key
        if self.dry_run:
            log(f"dry run: {event} {resource_id} would run: {self.prompt(key, envelope)}", self.agent.name)
            return
        if not store.claim(self.agent.name, key, envelope, _iso(_now()), self.store_path):
            log(f"{event} {resource_id}: already taken, not run again", self.agent.name)
            return
        log(f"{event} {resource_id} queued", self.agent.name)
        self._queue.put((key, envelope))

    def prompt(self, key, envelope):
        values = policy.variables(envelope, self.project_ids, self.project_codes())
        return policy.render(self.agent.triggers[key[0]], values)

    # -- work ---------------------------------------------------------------------------------------------------

    def start(self):
        if self.dry_run:                             # a dry run only logs what would run, so it re-runs nothing
            return
        for key, envelope in store.recover(self.agent.name, _iso(_now()), self.store_path):
            log(f"{key[0]} {key[1]} queued (left queued by the last Talos)", self.agent.name)
            self._queue.put((key, envelope))
        threading.Thread(target=self._loop, name=f"events-{self.agent.name}", daemon=True).start()

    def stop(self):
        self._pause.stop()

    def _loop(self):
        while True:
            key, envelope = self._queue.get()
            self._pause.wait()
            try:
                self.handle(key, envelope)
            except Exception as e:      # one bad run never stops the queue
                log(f"{key[0]} {key[1]} failed: {e}", self.agent.name)
                store.update(self.agent.name, key, self.store_path, status=store.FAILED, ended_at=_iso(_now()),
                             outcome="failed", detail=str(e)[:1500])

    def handle(self, key, envelope):
        """Run Claude once for the event. Returns the outcome (run_policy.LIMITED when it is to run again)."""
        event, resource_id, _ = key
        name = self.agent.name
        model = self.agent.model_default
        prompt = self.prompt(key, envelope)
        live_id = str(uuid.uuid4())
        limits = run_policy.run_limits(self.agent, model)
        cmd = runner.build_command(self.runner.claude_bin(self.claude), self.agent, model, new_session_id=live_id,
                                   limits=limits, mcp=self.runner.mcp_config(self.mcp),
                                   guard=self.runner.runtime_guard(_runtime_copy()))
        started = _now()
        run_id = run_log.start(name, resource_id, 1, model, live_id, False, run_log.EVENT, _iso(started),
                               summary=event)
        store.update(name, key, self.store_path, status=store.RUNNING, started_at=_iso(started), run_id=run_id)
        log(f"{event} {resource_id}: running on {model} ({limits.describe()}, session {live_id[:8]})", name)
        try:
            result, log_path = self.runner.execute(runner.Job(name, resource_id, "", cmd, self.env, prompt))
        except Exception as e:          # e.g. the Claude binary vanished: close the run, keep the failure
            result, log_path = {"is_error": True, "subtype": "talos error", "result": str(e)}, None
        result = result or {}
        ended = _now()
        outcome, summary, comment, _, reset_at, _ = run_policy.read_outcome(result, ended)
        cost = run_log.incremental_cost(result.get("total_cost_usd"), 0, False)
        stats = runner.usage_since(runner.transcript(result.get("session_id") or live_id), started)
        run_log.finish(run_id, _iso(ended), round((ended - started).total_seconds(), 1), outcome, summary, cost,
                       result.get("total_cost_usd"), stats, log_path.name if log_path else None)
        if outcome == run_policy.LIMITED:
            until = self._pause.hit(reset_at)
            store.update(name, key, self.store_path, status=store.QUEUED, outcome=outcome, summary=summary)
            log(f"{event} {resource_id} hit a Claude usage limit; runs again "
                f"{'after ' + until.strftime('%Y-%m-%d %H:%M') if until else 'once the pause is over'}", name)
            self._queue.put((key, envelope))
            return outcome
        self._pause.succeeded()
        status = store.DONE if outcome == "done" else store.FAILED
        store.update(name, key, self.store_path, status=status, ended_at=_iso(ended), outcome=outcome,
                     summary=summary, detail=comment[:4000] or None)
        cost_text = f"${cost:.2f}" if cost is not None else "cost unknown"
        log(f"{event} {resource_id} {outcome}: {summary} ({cost_text}, {stats['turns']} turns, "
            f"log {log_path.name if log_path else 'none'})", name)
        return outcome
