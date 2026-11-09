"""One Hermes socket per Talos runtime for workflow run requests (hermes D-020): the runtime's service token listens
to `workflow.agent.run.requested` on the `runtimes` scope and hands each request to the discovered agents' step
runners, instead of one socket per agent.

The envelope carries ids only (the step, not the agent), so the request goes to every agent this runtime runs; each
runner reads the step and skips one that isn't its own, and the step's claim decides between runtimes. With no agent
running here the request is ignored."""

import threading

from . import workflow_steps
from .config import log
from .hermes import Listener

RUNTIMES_SCOPE = "runtimes"
NAME = "talos-runtime"


def intents():
    return [{"projectId": RUNTIMES_SCOPE, "eventName": workflow_steps.STEP_EVENT}]


class Router:
    """The step runners of the agents running here, and the listener events and reconnects go through."""

    def __init__(self):
        self._runners = {}
        self._lock = threading.Lock()

    def register(self, runner):
        """Take run requests for this agent's runner; returns the call that stops that."""
        with self._lock:
            self._runners[runner.user_id] = runner
        return lambda: self.unregister(runner)

    def unregister(self, runner):
        with self._lock:
            if self._runners.get(runner.user_id) is runner:
                del self._runners[runner.user_id]

    def runners(self):
        with self._lock:
            return list(self._runners.values())

    def on_event(self, envelope):
        sid = workflow_steps.step_id(envelope)
        if sid is None:
            return
        runners = self.runners()
        if not runners:
            log(f"step {sid} ignored: no agent runs here", NAME)
        for runner in runners:
            runner.submit(sid, "runtimes")

    def on_ready(self, project_ids=None):
        """(Re)connected: each agent picks up the steps requested while the socket was down."""
        for runner in self.runners():
            try:
                runner.reconcile()
            except Exception as e:      # noqa: BLE001  (one agent's reconcile never skips the others)
                log(f"reconcile failed: {e}", runner.agent.name)


class _Runtime:
    """What Listener needs of an agent and its tracker, for the runtime itself: its name and the service token."""
    name = NAME

    def __init__(self, identity):
        self.identity = identity

    def token(self):
        return self.identity.token()

    def project_ids(self):
        return {}


def listener(router, identity, hermes_url, node_bin="node", retry_seconds=10):
    """The runtime's Listener: the service token is fetched fresh on every reconnect."""
    runtime = _Runtime(identity)
    return Listener(runtime, runtime, [], router.on_event, router.on_ready, hermes_url, node_bin, retry_seconds,
                    extra_intents=intents())
