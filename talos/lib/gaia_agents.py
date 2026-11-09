"""Gaia-declared agents (D-069): Talos asks Gaia which agents it may run, signed in as its own service client
(service_identity.py), instead of naming them in agent.json. Each is built from its agentspec on top of the local
template (registry.load_gaia_template): Gaia owns the name, role prompt, models, limits, tool grants and skills; the
template holds the machine-local settings (projects, runner, parallelism, prompts). They take work from Gaia's
workflow run requests only (workflow_steps.py), each on its own delegated token (delegated_tokens.py).

Discovery runs every few minutes: an agent new in the list starts, one gone from it (disabled, its delegation
emptied) stops taking new runs while a run in progress finishes, and a spec edit applies from the next run. Tokens
are checked every minute; a refused token stops the agent until discovery lists it and Gaia grants it again."""

import dataclasses
import re
import sys
import threading
import time

from . import agent_spec, workflow_steps
from .config import GAIA_MCP, TalosError, log
from .delegated_tokens import Refused

sys.path.insert(0, str(GAIA_MCP))
import server as gaia_mcp  # noqa: E402  (dev-tools/gaia-mcp/server.py)
import service_client  # noqa: E402  (dev-tools/gaia-mcp/service_client.py)

POLL_SECONDS = 300
RENEW_SECONDS = 60


def agent_name(display_name, agent_id, taken):
    """Talos' name for the agent (state folders, logs): gaia-<name>, with the id's start when two names collide."""
    name = "gaia-" + (re.sub(r"[^a-z0-9]+", "-", str(display_name).lower()).strip("-") or "agent")
    return name if name not in taken else f"{name}-{agent_id[:8]}"


def settings(template, spec):
    """The Agent fields the spec sets (agent_spec.settings_from), with its skills in the role prompt and its tool
    grants added to the template's allowedTools. Raises TalosError for a spec Talos can't run."""
    values = agent_spec.settings_from(spec, {k: getattr(template, k) for k in agent_spec.FALLBACKS})
    values["role_prompt"] += agent_spec.skills_prompt(spec)
    granted = [t for t in workflow_steps.granted_tools(spec) if t not in template.allowed_tools]
    values["allowed_tools"] = [*template.allowed_tools, *granted]
    return values


def build(template, row, spec, name):
    """The Agent for a listed Gaia agent (`row`: id, name, userId) and its spec."""
    return dataclasses.replace(template, name=name, display_name=str(row.get("name") or name),
                               gaia_agent_id=str(row["id"]), gaia_user_id=str(row.get("userId") or ""),
                               **settings(template, spec))


@dataclasses.dataclass
class AgentRun:
    """A started Gaia agent: its Agent (shared with its workers, so spec edits reach the next run), its token and
    what stops its intake."""
    agent: object
    token: object                       # delegated_tokens.AgentToken
    stoppers: list

    def renew(self):
        self.token()

    def stop(self):
        """Stop every part of the agent's intake, even when one of them fails, then close its token."""
        for stop in self.stoppers:
            try:
                stop()
            except Exception as e:      # noqa: BLE001  (one failing stopper must not leave the others running)
                log(f"stop failed: {e}", self.agent.name)
        self.token.close()


class Discovery:
    """The Gaia agents this Talos runs, kept in step with Gaia's list."""

    def __init__(self, gaia, template, start, poll_seconds=POLL_SECONDS, renew_seconds=RENEW_SECONDS):
        self.gaia = gaia                # gaia() -> a gaia-mcp client signed in as Talos' service client
        self.template = template
        self.start = start              # start(agent) -> AgentRun; TalosError when it can't run
        self.poll_seconds = poll_seconds
        self.renew_seconds = renew_seconds
        self.running = {}               # Gaia agent id -> AgentRun
        self._lock = threading.RLock()
        self._closed = False            # set by shutdown(): no agent starts after it

    def sync(self):
        """Read Gaia's list and start, update or stop agents to match. Returns (started, stopped) Talos names. A list
        Gaia can't give changes nothing; a spec it can't give leaves that agent as it is."""
        try:
            g = self.gaia()
            listed = {str(row["id"]): row for row in service_client.delegable_agents(g) if row.get("id")}
        except gaia_mcp.GaiaError as e:
            raise TalosError(f"cannot list the Gaia agents: {e}")
        specs = {}                      # read before taking the lock: a slow Gaia never blocks stop_all()
        for agent_id, row in listed.items():
            try:
                specs[agent_id] = g.agent_spec_by_id(agent_id)
            except gaia_mcp.GaiaError as e:
                log(f"Gaia agent '{row.get('name')}': no spec ({e})")
        started, stopped = [], []
        with self._lock:
            if self._closed:
                return started, stopped
            for agent_id in [a for a in self.running if a not in listed]:
                stopped.append(self._stop(agent_id, "no longer delegable in Gaia"))
            for agent_id, row in listed.items():
                spec = specs.get(agent_id)
                if spec is None:
                    continue
                if agent_id in self.running:
                    self._update(self.running[agent_id].agent, spec)
                else:
                    name = self._start(agent_id, row, spec)
                    if name:
                        started.append(name)
        return started, stopped

    def renew(self):
        """Renew the tokens that are about to expire; an agent whose token Gaia refuses stops."""
        with self._lock:
            runs = list(self.running.items())
        for agent_id, run in runs:
            try:
                run.renew()
            except Refused as e:
                with self._lock:
                    if self.running.get(agent_id) is run:
                        self._stop(agent_id, f"Gaia refused its token: {e}")
            except TalosError as e:     # Gaia unreachable: the next check tries again
                log(f"token renewal failed: {e}", run.agent.name)

    def stop_all(self):
        with self._lock:
            for agent_id in list(self.running):
                self._stop(agent_id, "Talos is stopping")

    def shutdown(self):
        """Stop every agent for good: a sync in progress or still to come starts none again."""
        with self._lock:
            self._closed = True
            self.stop_all()

    def _start(self, agent_id, row, spec):
        try:
            taken = {r.agent.name for r in self.running.values()}
            agent = build(self.template, row, spec, agent_name(row.get("name") or agent_id, agent_id, taken))
            self.running[agent_id] = self.start(agent)
        except TalosError as e:         # includes a refused token: discovery tries again next time
            log(f"Gaia agent '{row.get('name')}' not started: {e}")
            return None
        log(f"started Gaia agent '{agent.display_name}'", agent.name)
        return agent.name

    def _update(self, agent, spec):
        try:
            values = settings(self.template, spec)
        except TalosError as e:
            log(f"spec unusable, keeping the last good one: {e}", agent.name)
            return
        for field, value in values.items():
            setattr(agent, field, value)

    def _stop(self, agent_id, why):
        run = self.running.pop(agent_id)
        run.stop()
        log(f"takes no new runs: {why}", run.agent.name)
        return run.agent.name

    def loop(self, stop, clock=time.monotonic):
        """Sync every `poll_seconds` and renew tokens every `renew_seconds` until `stop` is set. Failures are logged
        and retried; they never end the loop."""
        next_sync = 0
        while not stop.is_set() and not self._closed:
            if clock() >= next_sync:
                try:
                    self.sync()
                except Exception as e:  # noqa: BLE001  (one bad sync must not end discovery)
                    log(f"Gaia agents: {e}")
                next_sync = clock() + self.poll_seconds
            self.renew()
            stop.wait(self.renew_seconds)
        self.stop_all()


def start_discovery(discovery):
    stop = threading.Event()
    threading.Thread(target=discovery.loop, args=(stop,), name="gaia-agents", daemon=True).start()
    return stop
