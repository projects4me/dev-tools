"""One agent's work queue: re-read the issue, run Claude, apply the outcome to the tracker."""

import html
import queue
import threading
import uuid
from datetime import datetime

from . import policy, runner, sessions
from . import runs as run_log
from .config import TalosError, log


def _now():
    return datetime.now().astimezone()


def _iso(moment):
    return moment.isoformat(timespec="seconds")


class Dispatcher:
    """Serial worker for one agent (its runs share working trees and the local stack)."""

    def __init__(self, agent, tracker, claude, dry_run=False):
        self.agent = agent
        self.tracker = tracker
        self.claude = claude
        self.dry_run = dry_run
        self.user_id = tracker.user_id()
        self._queue = queue.Queue()
        self._pending = set()
        self._lock = threading.Lock()

    # -- queueing ----------------------------------------------------------------------------

    def submit(self, number, source, reconcile=False):
        """Queue an issue once; repeats while it is queued or running are dropped."""
        with self._lock:
            if number in self._pending:
                log(f"#{number} already queued or running ({source})", self.agent.name)
                return False
            self._pending.add(number)
        self._queue.put((number, source, reconcile))
        log(f"#{number} queued ({source})", self.agent.name)
        return True

    def start(self):
        threading.Thread(target=self._loop, name=f"worker-{self.agent.name}", daemon=True).start()

    def _loop(self):
        while True:
            number, source, reconcile = self._queue.get()
            try:
                self.handle(number, reconcile)
            except Exception as e:  # never let one issue stop the worker
                log(f"#{number} failed in Talos: {e}", self.agent.name)
            finally:
                with self._lock:
                    self._pending.discard(number)

    # -- one job -----------------------------------------------------------------------------

    def handle(self, number, reconcile=False):
        issue = self.tracker.issue(number)
        decision = policy.decide(issue, self.agent, self.user_id, require_allowed_modifier=reconcile)
        if not decision.run:
            log(f"#{number} skipped: {decision.reason}", self.agent.name)
            return None
        state = sessions.get(self.agent.name, number)
        session_id, why = policy.resume_session(decision.action, state, _now(), self.agent.resume_max_idle_minutes)
        catch_up = decision.action.resume and bool(state.get("session_id")) and not session_id
        model = policy.choose_model(self.agent, issue, escalated=bool(state.get("escalated")))
        prompt = policy.render_prompt(decision.action, issue, decision.repo, catch_up=catch_up,
                                     reviewer=self.agent.reviewer_name)
        return self._run(issue, prompt, model, session_id, why=why)

    def _run(self, issue, prompt, model, session_id, escalated=False, why=None):
        number = issue["number"]
        state = sessions.get(self.agent.name, number)
        done_runs = state.get("runs") or 0
        left = policy.iterations_left(self.agent, done_runs)
        if left == 0:
            return self._iteration_limit(number, done_runs)
        live_id = session_id or str(uuid.uuid4())
        cmd = runner.build_command(self.claude, self.agent, prompt, model, session_id,
                                   new_session_id=None if session_id else live_id)
        mode = why or (f"resume {session_id[:8]}" if session_id else "new session")
        if not session_id:
            mode += f", session {live_id[:8]}"
        limit = f"{done_runs + 1}/{self.agent.max_iterations}" if left is not None else f"{done_runs + 1}"
        log(f"#{number} iteration {limit} on {model} ({mode}; status '{issue['status']}', "
            f"complexity {issue.get('complexity') or 'not set'})", self.agent.name)
        if self.dry_run:
            log(f"#{number} dry run, not starting Claude: {prompt}", self.agent.name)
            return "dry_run"

        started = _now()
        run_id = run_log.start(self.agent.name, number, done_runs + 1, model, live_id, bool(session_id),
                               issue["status"], _iso(started))
        sessions.update(self.agent.name, number, running=True, runs=done_runs + 1, last_model=model,
                        live_session_id=live_id, started_at=_iso(started))
        self._mark_picked_up(issue)
        try:
            result, log_path = runner.run(cmd, self.tracker.child_env(), self.agent.name, number)
        except Exception as e:  # e.g. the Claude binary vanished; still close the run and hand off
            result, log_path = {"is_error": True, "subtype": "talos error", "result": str(e)}, None
        ended = _now()
        new_session = result.get("session_id") or live_id
        reported = result.get("total_cost_usd")
        previous = state.get("session_cost_usd") if session_id and state.get("session_id") == session_id else 0
        cost = run_log.incremental_cost(reported, previous, bool(session_id))
        stats = runner.usage_since(runner.transcript(new_session), started)
        sessions.update(self.agent.name, number, running=False, last_ended_at=_iso(ended),
                        session_id=new_session, session_cost_usd=reported)
        outcome, summary, comment = policy.read_outcome(result)
        log_name = log_path.name if log_path else "none"
        run_log.finish(run_id, _iso(ended), round((ended - started).total_seconds(), 1), outcome, summary,
                       cost, reported, stats, log_path.name if log_path else None)
        cost_text = f"${cost:.2f}" if cost is not None else "cost unknown"
        log(f"#{number} {outcome}: {summary} ({cost_text}, {stats['turns']} turns, "
            f"peak context {stats['peak_context_tokens'] // 1000}K, log {log_name})", self.agent.name)

        if outcome == "escalate" and model != self.agent.model_escalate and not escalated:
            sessions.update(self.agent.name, number, escalated=True)
            reason = comment or summary
            self._comment(number, f"{self.agent.display_name}: escalating to {self.agent.model_escalate}", reason)
            follow_up = (f"You are continuing issue #{number} on a stronger model because the previous run "
                         f"escalated. Re-check where the work stands, then carry on. "
                         f"End with the same structured outcome.")
            return self._run(issue, follow_up, self.agent.model_escalate,
                             (result or {}).get("session_id"), escalated=True)
        if outcome == "escalate":
            outcome = "needs_input"
            comment = comment or f"<p>Escalation was requested on {model}, which is already the top model.</p>"

        if outcome == "done":
            self._check_handed_back(number, log_name)
        elif outcome in ("needs_input", "blocked"):
            title = "needs your input" if outcome == "needs_input" else "is blocked"
            self._hand_off(number, outcome, f"{self.agent.display_name} {title}", comment or summary)
        else:  # failed
            body = (f"<p>{html.escape(summary)}</p>"
                    f"<p>Talos log: <code>dev-tools/talos/state/logs/{html.escape(log_name)}</code></p>")
            if comment:
                body += f"<pre>{html.escape(comment)}</pre>"
            self._hand_off(number, "failed", f"{self.agent.display_name}: run failed", body, escaped=True)
        return outcome

    def _mark_picked_up(self, issue):
        """Show on the board that the agent is working it, before Claude starts."""
        status = self.agent.pickup_status
        if not status or issue["status"] == status:
            return
        try:
            self.tracker.set_status(issue["number"], status)
            log(f"#{issue['number']} status '{issue['status']}' -> '{status}'", self.agent.name)
            issue["status"] = status
        except TalosError as e:
            log(f"#{issue['number']} could not set status '{status}': {e}", self.agent.name)

    def _iteration_limit(self, number, runs):
        """Out of iterations: a human has to look before the agent spends more runs on it."""
        log(f"#{number} iteration limit reached ({runs}/{self.agent.max_iterations})", self.agent.name)
        self._hand_off(number, "limit", f"{self.agent.display_name}: needs manual intervention",
                       f"<p>{self.agent.display_name} has had {runs} runs on this issue, the limit is "
                       f"{self.agent.max_iterations}. It needs a human look before more agent work. To give it "
                       f"another {self.agent.max_iterations}, run <code>dev-tools/talos/talos reset "
                       f"{html.escape(self.agent.name)} {number}</code> and assign the issue back.</p>",
                       escaped=True)
        return "limit"

    # -- tracker side effects ------------------------------------------------------------------

    def _comment(self, number, title, body, escaped=False):
        self.tracker.comment(number, title, body if escaped or body.lstrip().startswith("<")
                             else f"<p>{html.escape(body)}</p>")

    def _hand_off(self, number, outcome, title, body, escaped=False):
        status = self.agent.statuses[outcome]
        if self.dry_run:
            log(f"#{number} dry run, would hand off: status '{status}', assign {self.agent.handoff_to}",
                self.agent.name)
            return
        try:
            self._comment(number, title, body, escaped)
            self.tracker.hand_off(number, status, self.agent.handoff_to)
            log(f"#{number} handed off: status '{status}', assigned to {self.agent.handoff_to}", self.agent.name)
        except TalosError as e:
            log(f"#{number} hand-off failed: {e}", self.agent.name)

    def _check_handed_back(self, number, log_name):
        """'done' must leave the issue with someone else; otherwise the baton is lost."""
        issue = self.tracker.issue(number)
        if issue["assignee"] == self.user_id:
            self._hand_off(number, "failed", f"{self.agent.display_name}: finished but did not hand back",
                           f"<p>The run reported done but left the issue assigned to the agent "
                           f"(status '{html.escape(str(issue['status']))}'). Please check it. "
                           f"Talos log: <code>{html.escape(log_name)}</code></p>", escaped=True)

    # -- startup -------------------------------------------------------------------------------

    def recover_interrupted(self):
        """Runs cut off by a Talos restart go back to the human, not silently restarted."""
        for number in sessions.interrupted(self.agent.name):
            sessions.update(self.agent.name, number, running=False, last_ended_at=_iso(_now()))
            run_log.close_unfinished(self.agent.name, number, _iso(_now()))
            log(f"#{number} was interrupted by a Talos restart", self.agent.name)
            self._hand_off(number, "failed", f"{self.agent.display_name}: run interrupted",
                           "<p>Talos stopped while this issue was being worked on. Check the branch, "
                           "then assign it back to resume.</p>", escaped=True)

    def reconcile(self, project_ids):
        """Pick up issues assigned while the listener was down (Hermes keeps no history)."""
        for name, pid in project_ids.items():
            for number in self.tracker.assigned_issue_numbers(pid, self.user_id):
                self.submit(number, f"reconcile {name}", reconcile=True)
