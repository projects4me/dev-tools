"""Keeps one agent's Hermes listener (listener/listen.js) running and feeds its events to the agent's handler.

Each agent declares what it listens to: the event names (subscribed in every project of the agent), a handler for
each envelope, and what to do once (re)connected (Hermes keeps no history, so the owner reconciles then).
"""

import json
import os
import subprocess
import threading
import time

from .config import LISTENER, TALOS_DIR, log


class Listener:
    """Supervises the Node Socket.IO client: fresh token on every (re)connect, `on_ready(project_ids)` once ready,
    `on_event(envelope)` for each event."""

    def __init__(self, agent, tracker, events, on_event, on_ready, hermes_url, node_bin="node", retry_seconds=10):
        self.agent = agent
        self.tracker = tracker
        self.on_event, self.on_ready = on_event, on_ready
        self.hermes_url = hermes_url
        self.node_bin = node_bin
        self.retry_seconds = retry_seconds
        self.project_ids = tracker.project_ids()
        self.intents = intents(self.project_ids, events)

    def start(self):
        threading.Thread(target=self._loop, name=f"hermes-{self.agent.name}", daemon=True).start()

    def _loop(self):
        while True:
            try:
                self._listen_once()
            except Exception as e:
                log(f"listener error: {e}", self.agent.name)
            time.sleep(self.retry_seconds)

    def _listen_once(self):
        env = {**os.environ, "TALOS_TOKEN": self.tracker.token()}
        cmd = [self.node_bin, "--use-system-ca", str(LISTENER),
               "--hermes-url", self.hermes_url, "--intents", json.dumps(self.intents)]
        proc = subprocess.Popen(cmd, cwd=TALOS_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                log(f"listener: {line.strip()[:300]}", self.agent.name)
                continue
            kind = msg.get("type")
            if kind == "ready":
                log(f"listening to Hermes ({len(msg.get('accepted') or [])} intents, "
                    f"{len(msg.get('rejected') or [])} rejected)", self.agent.name)
                self.on_ready(self.project_ids)
            elif kind == "event":
                self._dispatch(msg.get("envelope"))
            elif kind == "error":
                log(f"listener: {msg.get('message')}", self.agent.name)
        proc.wait()
        log(f"listener exited ({proc.returncode}); reconnecting in {self.retry_seconds}s", self.agent.name)

    def _dispatch(self, envelope):
        try:
            self.on_event(envelope)
        except Exception as e:      # one bad event never stops the listener
            log(f"event {(envelope or {}).get('eventName')}: {e}", self.agent.name)


def intents(project_ids, events):
    """Hermes intents: every event name in every project (project name -> id)."""
    return [{"projectId": pid, "eventName": name} for pid in project_ids.values() for name in events]
