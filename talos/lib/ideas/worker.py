"""An ideas agent's work queue: one job at a time (runs share the workspace), the way the Dispatcher works issues.

Jobs come from events (policy.job_for, policy.gaia_job_for) and from reconciling after each (re)connect, since Hermes keeps no history:
engineer messages no turn has taken in, and plans approved or left creating.
"""

import queue
import threading
from datetime import datetime

from .. import runs as run_log
from ..config import log
from . import policy, state


class IdeaWorker:
    def __init__(self, agent, client, progress, conversation, approval, user_id, job_for=None):
        self.agent, self.client, self.progress = agent, client, progress
        self.conversation, self.approval = conversation, approval
        self.user_id = str(user_id)
        # envelope -> job or None: the sim's events by default, policy.gaia_job_for for Gaia's
        self.job_for = job_for or (lambda envelope: policy.job_for(envelope, agent, self.user_id))
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self._pending = set()        # (kind, idea) queued or running
        self._current = None         # (kind, idea, run or None)
        self._stop = set()           # ideas whose turn should stop (before or while it runs)
        self._again = set()          # ideas that got a new message while their turn ran

    # -- intake -------------------------------------------------------------------------------------------------

    def on_event(self, envelope):
        job = self.job_for(envelope)
        if job and job[0] == "stop":
            self.stop(job[1])
        elif job:
            self.submit(job, f"event {envelope.get('eventName')} from user {envelope.get('actorId')}")

    def submit(self, job, source):
        key = job[:2]
        with self._lock:
            if key in self._pending:
                if job[0] == "turn" and self._current and self._current[:2] == key:
                    self._again.add(job[1])   # answered after this turn, which took its messages in already
                return
            self._pending.add(key)
        if job[0] == "turn":
            self._set_state(job[1], "queued")
        self._queue.put(job)
        log(f"idea #{job[1]} {job[0]} queued ({source})", self.agent.name)

    def stop(self, idea_id):
        with self._lock:
            current = self._current
            if current and current[0] == "turn" and current[1] == idea_id:
                run = current[2]
                self._stop.add(idea_id)
            elif ("turn", idea_id) in self._pending:
                run = None
                self._stop.add(idea_id)     # dropped when it comes up
            else:
                run = False
        if run is False:                    # nothing to stop: don't leave the idea "stopping"
            self._set_state(idea_id, "idle")
            return
        if run:
            run.stop()
        log(f"idea #{idea_id}: stop requested", self.agent.name)

    def reconcile(self):
        """Queue what is waiting for the agent; clean up after an interrupted Talos."""
        try:
            ideas = [i for i in self.client.ideas() if i["agent"] == self.user_id and i["status"] != "archived"]
        except Exception as e:
            log(f"reconcile: {e}", self.agent.name)
            return
        for summary in ideas:
            idea_id = summary["id"]
            with self._lock:
                if any(k[1] == idea_id for k in self._pending):
                    continue
            try:
                self._reconcile_one(idea_id)
            except Exception as e:
                log(f"reconcile idea #{idea_id}: {e}", self.agent.name)

    def _reconcile_one(self, idea_id):
        data = self.client.idea(idea_id)
        if data["idea"]["owner"] not in self.agent.accept_from:
            return
        for plan in policy.resumable_plans(data["plans"]):   # creating: Talos stopped mid-approval, carry on
            self.submit(("approve", idea_id, plan["id"]), "reconcile")
        if policy.unanswered(data["messages"], state.get(self.agent.name, idea_id).get("answered_upto")):
            self.submit(("turn", idea_id), "reconcile")
        elif data["idea"]["agent_state"] != "idle":
            self._set_state(idea_id, "idle")

    # -- work ---------------------------------------------------------------------------------------------------

    def start(self):
        # turns a stopped Talos left open in talos.db would show as running for good
        run_log.close_unfinished_ideas(self.agent.name, datetime.now().astimezone().isoformat(timespec="seconds"))
        threading.Thread(target=self._loop, name=f"ideas-{self.agent.name}", daemon=True).start()

    def _loop(self):
        while True:
            job = self._queue.get()
            try:
                self.handle(job)
            except Exception as e:
                log(f"idea #{job[1]} {job[0]} failed: {e}", self.agent.name)
            finally:
                with self._lock:
                    self._pending.discard(job[:2])
                    self._current = None
                    self._stop.discard(job[1])
                    again = job[1] in self._again
                    self._again.discard(job[1])
                if again:
                    self.submit(("turn", job[1]), "message sent during the last turn")

    def handle(self, job):
        kind, idea_id = job[0], job[1]
        with self._lock:
            self._current = (kind, idea_id, None)
            stopped = kind == "turn" and idea_id in self._stop
        data = self.client.idea(idea_id)
        idea = data["idea"]
        if idea["agent"] != self.user_id or idea["owner"] not in self.agent.accept_from:
            log(f"idea #{idea_id}: not this agent's, or its owner can't hand it work; skipped", self.agent.name)
            return
        if stopped:
            self.client.post(idea_id, "error", "Stopped.")
            state.update(self.agent.name, idea_id, answered_upto=self._latest_user_message(idea_id))
            self._set_state(idea_id, "idle")
            return
        if kind == "approve":
            self.approval.run(idea_id, job[2])
            return
        if not policy.unanswered(data["messages"], state.get(self.agent.name, idea_id).get("answered_upto")):
            log(f"idea #{idea_id}: nothing new to answer", self.agent.name)   # before "working": no notice left behind
            return
        self._set_state(idea_id, "working")
        self.progress.publish(idea, {"phase": "started"})
        try:
            self.conversation.turn(idea_id, self._running)
        except Exception as e:
            self.client.post(idea_id, "error", f"The agent could not answer: {e}")
            raise
        finally:
            self.progress.publish(idea, {"phase": "done"})
            self._set_state(idea_id, "idle")

    def _latest_user_message(self, idea_id):
        return next((m["id"] for m in reversed(self.client.idea(idea_id)["messages"]) if m["role"] == "user"), 0)

    def _running(self, run):
        """Called by the conversation once its process runs: Stop can now reach it."""
        with self._lock:
            kind, idea_id, _ = self._current
            self._current = (kind, idea_id, run)
            stop = idea_id in self._stop
        if stop:
            run.stop()

    def _set_state(self, idea_id, agent_state):
        try:
            self.client.set_state(idea_id, agent_state)
        except Exception as e:
            log(f"idea #{idea_id}: can't set agent_state {agent_state}: {e}", self.agent.name)
