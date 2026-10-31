"""The Orchestrator: sweeps the worker's aga- milestones and assigns it ready issues, restarts first.

Sweeps run on one thread: after a Hermes event (debounced), on every (re)connect and every sweepMinutes. Gaia is
read fresh each time; Hermes events are only a nudge."""

import html
import json
import threading
from datetime import datetime

from .. import sessions, workspaces
from ..config import WORKSPACE, TalosError, log
from ..policy import ASSIGNEE_EVENT, STATUS_EVENT
from . import policy
from . import state as store


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _branch_holder(repo, issue):
    return workspaces.branch_holder(WORKSPACE, repo, issue)


class Orchestrator:
    """Feeds one worker agent (registry.Agent of kind issues) as one orchestrator agent (kind orchestrator)."""

    def __init__(self, agent, worker, tracker, worker_id, dry_run=False, branch_holder=_branch_holder, now=_now,
                 worker_state=sessions.get, merges=None, keeper=None):
        self.agent, self.worker, self.tracker = agent, worker, tracker
        self.settings = agent.orchestration
        self.worker_id = str(worker_id)
        self.merges = merges            # merging.MergeSweeper, or None without automatic merges
        self.keeper = keeper            # keeper.BranchKeeper: keeps agent PRs mergeable (None = off)
        self.owners = [str(o) for o in (self.settings.owners or [worker.handoff_to]) if o]
        self.max_in_flight = self.settings.max_in_flight or worker.max_parallel
        self.dry_run = dry_run
        self.branch_holder, self.now, self.worker_state = branch_holder, now, worker_state
        self.projects = list(worker.projects)
        self._project_ids = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # -- triggers --------------------------------------------------------------------------

    def on_event(self, envelope):
        """An issue status or assignee change: sweep soon (debounced, so a burst of events gives one sweep)."""
        if isinstance(envelope, dict) and envelope.get("eventName") in (ASSIGNEE_EVENT, STATUS_EVENT):
            self._wake.set()

    def on_ready(self, project_ids):
        """(Re)connected to Hermes, which keeps no history: sweep."""
        self._project_ids = dict(project_ids)
        self._wake.set()

    def start(self):
        threading.Thread(target=self._loop, name=f"orchestrator-{self.agent.name}", daemon=True).start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            if self._wake.wait(timeout=self.settings.sweep_minutes * 60):
                self._stop.wait(self.settings.debounce_seconds)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                self.sweep()
            except Exception as e:      # one failed sweep never stops the orchestrator; the next one retries
                log(f"sweep failed: {e}", self.agent.name)

    # -- the sweep -------------------------------------------------------------------------

    def sweep(self):
        """One pass: update the breaker, then fill the worker's free slots. Returns [(issue, 'restart'|'new')]
        assigned (or, in a dry run, that would be)."""
        with self._lock:
            note, done = self._sweep()
            log(f"sweep: {note}", self.agent.name)
            if not self.dry_run:
                store.update(self.agent.name, last_sweep_at=self.now(), last_sweep=note)
            return done

    def _sweep(self):
        st = store.get(self.agent.name)
        tripped = self._update_breaker(st)
        # merges first and even while paused: a paused orchestrator hands them to the reviewer instead of holding them
        merged = self.merges.sweep(paused=tripped or bool(st.get("paused"))) if self.merges else ""
        merged = f"merges: {merged}; " if merged else ""
        if tripped:
            return f"{merged}paused by the circuit breaker", []
        if st.get("paused"):
            return f"{merged}paused ({st.get('pause_reason') or 'talos pause'})", []
        kept = self.keeper.sweep() if self.keeper else ""
        merged += f"branches: {kept}; " if kept else ""
        in_flight = self._in_flight()
        slots = policy.free_slots(self.max_in_flight, in_flight)
        if not slots:
            return f"{merged}no free slot ({in_flight} of {self.max_in_flight} in flight)", []
        issues = [i for project in self.projects
                  for i in self.tracker.issues_in(project, self.settings.milestone_prefix)]
        restarts = policy.rank([i for i in issues if self._restart_skip(i) is None])
        news = policy.rank([i for i in issues if self._new_skip(i) is None])
        done = []
        for issue, kind in [(i, "restart") for i in restarts] + [(i, "new") for i in news]:
            if len(done) >= slots:
                break
            if self._assign(issue["number"], kind):
                done.append((issue["number"], kind))
        what = ", ".join(f"#{n} ({kind})" for n, kind in done) or "nothing ready"
        return (f"{merged}{len(done)} of {slots} free slot(s) filled: {what}" + (" (dry run)" if self.dry_run else ""),
                done)

    def _in_flight(self):
        """Issues assigned to the worker that it still has to work: finished ones (the worker's done statuses and
        wont_fix) and ones waiting only on a handback Gaia refused (Talos retries it) don't hold a slot."""
        if self._project_ids is None:
            self._project_ids = self.tracker.project_ids()
        numbers = [n for pid in self._project_ids.values()
                   for n in self.tracker.assigned_issue_numbers(pid, self.worker_id)]
        pending = sessions.pending_handoffs(self.worker.name)
        numbers = [n for n in numbers if n not in pending]
        finished = {str(s).lower() for s in (*self.worker.dependency_done_statuses, "wont_fix")}
        statuses = self.tracker.statuses_of(numbers) if numbers else {}
        return sum(1 for n in numbers if str(statuses.get(n) or "").lower() not in finished)

    def _update_breaker(self, st):
        """Count the worker's handbacks since the last sweep; pause when the failure streak reaches the limit.
        True when this call tripped the breaker."""
        now = self.now()
        since = st.get("seen_upto")
        if since is None:           # the first sweep starts counting from now, not from history
            if not self.dry_run:
                store.update(self.agent.name, seen_upto=now)
            return False
        events = store.events_since(self.agent.name, since)
        if not events:
            return False
        s = self.settings
        streak = policy.fail_streak(int(st.get("fail_streak") or 0), events, s.auto_retry_reasons,
                                    s.max_auto_retries)
        tripped = not st.get("paused") and policy.breaker_tripped(streak, s.fail_streak_limit)
        if self.dry_run:
            if tripped:
                log(f"dry run: would pause, {streak} failed or blocked handbacks in a row", self.agent.name)
            return False
        fields = {"fail_streak": streak, "seen_upto": max(e["at"] for e in events)}
        if tripped:
            fields.update(paused=True, paused_at=now,
                          pause_reason=f"circuit breaker: {streak} failed or blocked handbacks in a row")
            log(f"paused: {streak} failed or blocked handbacks in a row; `talos resume {self.agent.name}` "
                f"after a look", self.agent.name)
        store.update(self.agent.name, **fields)
        return tripped

    # -- candidates ------------------------------------------------------------------------

    def _new_skip(self, issue):
        """policy.new_skip_reason, reading the dependencies' statuses only for issues that pass the rest."""
        s = self.settings
        reason = policy.new_skip_reason({**issue, "dependsOn": []}, s.milestone_prefix, self.projects, self.owners,
                                        {}, self.worker.dependency_done_statuses)
        if reason or not issue.get("dependsOn"):
            return reason
        try:
            statuses = self.tracker.statuses_of(issue["dependsOn"])
        except TalosError as e:
            return f"dependencies unreadable: {e}"
        return policy.new_skip_reason(issue, s.milestone_prefix, self.projects, self.owners, statuses,
                                      self.worker.dependency_done_statuses)

    def _restart_skip(self, issue):
        """policy.restart_skip_reason with the worker's state for the issue and, for branch_checked_out, where the
        branch is still checked out."""
        s = self.settings
        if not policy.in_scope(issue, s.milestone_prefix, self.projects) or \
                str(issue.get("status") or "").lower() not in policy.RESTART_STATUSES:
            return "not a handback"      # most issues: skip the state read
        st = self.worker_state(self.worker.name, issue["number"])
        holder = None
        if st.get("handoff_reason") == "branch_checked_out":
            holder = self._holder(issue, st)
        return policy.restart_skip_reason(issue, st, s.milestone_prefix, self.projects, self.owners,
                                          self.worker_id, s.auto_retry_reasons, s.max_auto_retries, holder)

    def _holder(self, issue, st):
        try:
            detail = json.loads(st.get("handoff_detail") or "{}")
        except ValueError:
            detail = {}
        repo = detail.get("repo") or self.worker.repo_for(issue.get("project"))
        if not repo:
            return None
        try:
            return self.branch_holder(repo, issue["number"])
        except Exception as e:      # git could not say: wait rather than collide with the checkout
            return f"(unknown: {e})"

    # -- assigning -------------------------------------------------------------------------

    def _assign(self, number, kind):
        """Re-read and re-check the issue, then assign only the assignee to the worker and say why."""
        try:
            issue = self.tracker.issue(number)
        except TalosError as e:
            log(f"#{number} not assigned: {e}", self.agent.name)
            return False
        skip = self._restart_skip(issue) if kind == "restart" else self._new_skip(issue)
        if skip:
            log(f"#{number} not assigned on re-check: {skip}", self.agent.name)
            return False
        retries = int(self.worker_state(self.worker.name, number).get("auto_retries") or 0) + 1 \
            if kind == "restart" else 0
        if self.dry_run:
            log(f"#{number} dry run, would assign to {self.worker.display_name} ({kind})", self.agent.name)
            return True
        try:
            self.tracker.assign(number, self.worker_id)
        except TalosError as e:
            log(f"#{number} assignment failed: {e}", self.agent.name)
            return False
        if kind == "restart":
            sessions.update(self.worker.name, number, auto_retries=retries)
        store.record_assignment(self.agent.name, number, self.worker.name, self.now())
        log(f"#{number} assigned to {self.worker.display_name} ({kind})", self.agent.name)
        try:
            self.tracker.comment(number, f"{self.agent.display_name}: assigned to {self.worker.display_name}",
                                 self._why(issue, kind, retries))
        except TalosError as e:
            log(f"#{number} comment failed: {e}", self.agent.name)
        return True

    def _why(self, issue, kind, retries):
        milestone = html.escape(str(issue.get("milestone") or ""))
        if kind == "restart":
            st = self.worker_state(self.worker.name, issue["number"])
            reason = html.escape(str(st.get("handoff_reason") or ""))
            return (f"<p>Automatic restart {retries} of {self.settings.max_auto_retries}: Talos handed this issue "
                    f"back for a mechanical reason (<code>{reason}</code>), not a question. There is nothing new "
                    f"from the reviewer; continue the work where it stopped.</p>")
        return (f"<p>Next ready issue in milestone {milestone}: status new, priority "
                f"{html.escape(str(issue.get('priority') or '-'))}, and every \"Depends on\" issue is done.</p>")
