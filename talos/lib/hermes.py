"""Keeps one agent's Hermes listener (listener/listen.js) running and feeds its events to a dispatcher."""

import json
import os
import subprocess
import threading
import time

from . import policy
from .config import LISTENER, TALOS_DIR, log


class Listener:
    """Supervises the Node Socket.IO client: fresh token on every (re)connect, reconcile once ready."""

    def __init__(self, agent, tracker, dispatcher, hermes_url, node_bin="node", retry_seconds=10):
        self.agent = agent
        self.tracker = tracker
        self.dispatcher = dispatcher
        self.hermes_url = hermes_url
        self.node_bin = node_bin
        self.retry_seconds = retry_seconds
        self.project_ids = tracker.project_ids()
        self.intents = [{"projectId": pid, "eventName": policy.ASSIGNEE_EVENT} for pid in self.project_ids.values()]

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
                self.dispatcher.reconcile(self.project_ids)
            elif kind == "event":
                self._on_event(msg.get("envelope"))
            elif kind == "error":
                log(f"listener: {msg.get('message')}", self.agent.name)
        proc.wait()
        log(f"listener exited ({proc.returncode}); reconnecting in {self.retry_seconds}s", self.agent.name)

    def _on_event(self, envelope):
        number = policy.event_issue(envelope, self.agent, self.dispatcher.user_id)
        if number is not None:
            self.dispatcher.submit(number, f"assigned by user {envelope.get('actorId')}")
