"""One agent's work queue: re-read the issue, run Claude, apply the outcome to the tracker."""

import html
import threading
import time
import uuid
from datetime import datetime, timedelta

from . import policy, pool, runner, sessions, workspaces
from . import runs as run_log
from .config import STATE_DIR, TalosError, log


def _now():
    return datetime.now().astimezone()


def _iso(moment):
    return moment.isoformat(timespec="seconds")


def lease_file(agent_name):
    """Where an agent's leases in flight are written, for `talos status`."""
    return STATE_DIR / agent_name / "leases.json"


class Dispatcher:
    """Worker pool for one agent: `maxParallel` threads, each issue in its own workspace (or container) and test
    slot."""

    def __init__(self, agent, tracker, claude, dry_run=False, mcp=None):
        self.agent = agent
        self.tracker = tracker
        self.claude = claude
        self.dry_run = dry_run
        self.mcp = mcp          # the agent's MCP config file (mcp.mcp_config)
        self.runner = runner.for_agent(agent)
        self.user_id = tracker.user_id()
        self._queue = pool.RunQueue()
        self.leases = pool.LeaseFile(lease_file(agent.name))
        self._gate = pool.Gate(agent.test_slots or agent.max_parallel, agent.max_parallel_by_repo,
                               agent.max_parallel_by_model, on_change=self.leases.write)
        self._pending = set()
        self._lock = threading.Lock()
        self._paused_until = None   # datetime: no dispatching until then (Claude usage limit)
        self._limit_streak = 0
        self._held = {}             # issue number -> reconcile flag, for issues waiting on "Depends on" issues
        self._notified = {}         # issue number -> waiting-on numbers last commented, so it isn't repeated

    # -- queueing ----------------------------------------------------------------------------

    def on_event(self, envelope):
        """An issue assignment from Hermes: work it when it hands this agent work from an allowed user."""
        if policy.is_status_event(envelope):
            self.release_held("status changed")
            return
        number = policy.event_issue(envelope, self.agent, self.user_id)
        if number is not None:
            self.submit(number, f"assigned by user {envelope.get('actorId')}")

    def release_held(self, source):
        """Queue held issues whose dependencies are now done; the rest stay held."""
        with self._lock:
            held = dict(self._held)
        for number, reconcile in held.items():
            try:
                issue = self.tracker.issue(number)
                waiting = policy.unmet_dependencies(issue["dependsOn"], self.tracker.statuses_of(issue["dependsOn"]))
            except TalosError as e:
                log(f"#{number} held re-check failed: {e}", self.agent.name)
                continue
            if not waiting:
                with self._lock:
                    self._held.pop(number, None)
                    self._notified.pop(number, None)
                self.submit(number, f"released ({source})", reconcile=reconcile)

    def submit(self, number, source, reconcile=False):
        """Queue an issue once; repeats while it is queued or running are dropped."""
        with self._lock:
            if number in self._pending:
                log(f"#{number} already queued or running ({source})", self.agent.name)
                return False
            self._pending.add(number)
        self._queue.put(self._order(number), (number, source, reconcile))
        log(f"#{number} queued ({source})", self.agent.name)
        return True

    def _order(self, number):
        """Where the issue goes in the queue: resumptions first, then by priority (see pool.order_key)."""
        try:
            issue = self.tracker.issue(number)
        except TalosError:
            return pool.order_key(False, None)
        action = self.agent.action_for(issue["status"])
        return pool.order_key(bool(action and action.resume), issue.get("priority"))

    def start(self):
        for n in range(self.agent.max_parallel):
            threading.Thread(target=self._loop, name=f"worker-{self.agent.name}-{n + 1}", daemon=True).start()

    def _loop(self):
        while True:
            number, source, reconcile = self._queue.get()
            requeue = False
            try:
                self._wait_while_paused()
                requeue = self.handle(number, reconcile) == "limited"
            except Exception as e:  # never let one issue stop the worker
                log(f"#{number} failed in Talos: {e}", self.agent.name)
            finally:
                if requeue:       # a usage limit stopped the run: it stays queued, the pause holds the queue
                    self._queue.put(self._order(number), (number, source, reconcile))
                else:
                    with self._lock:
                        self._pending.discard(number)

    def _wait_while_paused(self):
        while self._paused_until is not None:
            remaining = (self._paused_until - _now()).total_seconds()
            if remaining <= 0:
                self._paused_until = None
                log("usage-limit pause over, dispatching resumes", self.agent.name)
                return
            time.sleep(min(remaining, 30))

    # -- one job -----------------------------------------------------------------------------

    def handle(self, number, reconcile=False):
        issue = self.tracker.issue(number)
        decision = policy.decide(issue, self.agent, self.user_id, require_allowed_modifier=reconcile,
                                 dependency_statuses=self.tracker.statuses_of(issue.get("dependsOn") or []))
        if decision.waiting_on:
            return self._hold(number, decision, reconcile)
        with self._lock:
            self._held.pop(number, None)
            self._notified.pop(number, None)
        if not decision.run:
            log(f"#{number} skipped: {decision.reason}", self.agent.name)
            return None
        state = sessions.get(self.agent.name, number)
        session_id, why = policy.resume_session(decision.action, state, _now(), self.agent.resume_max_idle_minutes)
        if session_id and not self.runner.resumable:
            session_id, why = None, "new session (the runner keeps no sessions)"
        catch_up = decision.action.resume and bool(state.get("session_id")) and not session_id
        model = policy.choose_model(self.agent, issue, escalated=bool(state.get("escalated")))
        prompt = policy.render_prompt(decision.action, issue, decision.repo, catch_up=catch_up,
                                     reviewer=self.agent.reviewer_name)
        folder = None
        if not self.dry_run:                  # the workspace first: the pack is built from it
            try:
                folder = self.runner.prepare(number, decision.repo)
            except workspaces.BranchCheckedOut as e:
                return self._branch_checked_out(number, e, decision.repo)
            except workspaces.WorkspaceError as e:     # the run itself reports it as a failed run
                log(f"#{number} workspace not ready before the run: {e}", self.agent.name)
        if decision.repo == "gaia" and not session_id:        # a resumed session already has the pack
            prompt = policy.with_pack(prompt, runner.gaia_pack(number, folder))
        return self._run(issue, prompt, model, session_id, why=why, repo=decision.repo)

    def _branch_checked_out(self, number, error, repo):
        """The issue's branch is checked out in another folder, so no worktree can be made. A person has to free it;
        no run started, so no iteration is counted."""
        log(f"#{number} not started: {error}", self.agent.name)
        folder = html.escape(str(error.folder))
        self._hand_off(number, "needs_input", f"{self.agent.display_name}: branch is checked out elsewhere",
                       f"<p>Talos could not start this issue: <code>{html.escape(error.branch)}</code> is checked "
                       f"out in <code>{folder}</code>, and git allows a branch in one folder only.</p>"
                       f"<p>To free it, commit or stash what is there and switch that folder to another branch "
                       f"(<code>git -C {folder} switch {html.escape(workspaces.base_branch(repo))}</code>), then assign the issue back. "
                       f"This did not count as an iteration.</p>", escaped=True)
        return "needs_input"

    def _hold(self, number, decision, reconcile):
        """Keep the issue out of the queue until its dependencies are done; comment once per waiting set.
        Held time is not a run, so it counts toward neither iterations nor runs."""
        with self._lock:
            self._held[number] = reconcile
            first = self._notified.get(number) != decision.waiting_on
            self._notified[number] = decision.waiting_on
        log(f"#{number} held: {decision.reason}", self.agent.name)
        if first and not self.dry_run:
            try:
                self._comment(number, f"{self.agent.display_name}: {decision.reason}",
                              "<p>This issue depends on " + ", ".join(f"#{n}" for n in decision.waiting_on) +
                              ", which is not done yet. Talos will start it once that is done; nothing is "
                              "needed from you.</p>", escaped=True)
            except TalosError as e:
                log(f"#{number} waiting comment failed: {e}", self.agent.name)
        return "held"

    def _run(self, issue, prompt, model, session_id, escalated=False, why=None, repo=None, shipping=False):
        number = issue["number"]
        state = sessions.get(self.agent.name, number)
        done_runs = state.get("runs") or 0
        left = policy.iterations_left(self.agent, done_runs)
        if left == 0:
            return self._iteration_limit(number, done_runs)
        live_id = session_id or str(uuid.uuid4())
        limits = policy.run_limits(self.agent, model)
        cmd = runner.build_command(self.runner.claude_bin(self.claude), self.agent, model, session_id,
                                   new_session_id=None if session_id else live_id, limits=limits,
                                   mcp=self.runner.mcp_config(self.mcp))
        mode = why or (f"resume {session_id[:8]}" if session_id else "new session")
        if not session_id:
            mode += f", session {live_id[:8]}"
        limit = f"{done_runs + 1}/{self.agent.max_iterations}" if left is not None else f"{done_runs + 1}"
        log(f"#{number} iteration {limit} on {model} ({mode}; {limits.describe()}; status '{issue['status']}', "
            f"complexity {issue.get('complexity') or 'not set'})", self.agent.name)
        if self.dry_run:
            log(f"#{number} dry run, not starting Claude: {prompt}", self.agent.name)
            return "dry_run"

        lease = self._gate.acquire(number, repo, model)      # waits for the caps and a free test slot
        try:                                                  # the lease is released even if the bookkeeping raises
            started = _now()
            run_id = run_log.start(self.agent.name, number, done_runs + 1, model, live_id, bool(session_id),
                                   issue["status"], _iso(started))
            sessions.update(self.agent.name, number, running=True, runs=done_runs + 1, last_model=model,
                            live_session_id=live_id, started_at=_iso(started))
            self._mark_picked_up(issue)
            try:
                env = {**self.tracker.child_env(), **self.agent.env}
                if lease.slot:
                    env["P4M_TEST_SLOT"] = str(lease.slot)        # `ai test` runs gaia suites in this slot
                result, log_path = self.runner.execute(runner.Job(self.agent.name, number, repo, cmd, env, prompt))
            except Exception as e:  # e.g. the Claude binary or Docker vanished, or no worktree; close the run, hand off
                result, log_path = {"is_error": True, "subtype": "talos error", "result": str(e)}, None
        finally:
            self._gate.release(lease)
        ended = _now()
        new_session = result.get("session_id") or live_id
        reported = result.get("total_cost_usd")
        previous = state.get("session_cost_usd") if session_id and state.get("session_id") == session_id else 0
        cost = run_log.incremental_cost(reported, previous, bool(session_id))
        stats = runner.usage_since(runner.transcript(new_session), started)
        sessions.update(self.agent.name, number, running=False, last_ended_at=_iso(ended),
                        session_id=new_session, session_cost_usd=reported)
        limited, reset_at = policy.read_limit(result, ended)
        if limited:
            outcome, summary, comment, handoff = "limited", "Claude usage or rate limit reached", "", ""
        else:
            self._limit_streak = 0
            outcome, summary, comment, handoff = policy.read_outcome(result)
        log_name = log_path.name if log_path else "none"
        run_log.finish(run_id, _iso(ended), round((ended - started).total_seconds(), 1), outcome, summary,
                       cost, reported, stats, log_path.name if log_path else None)
        cost_text = f"${cost:.2f}" if cost is not None else "cost unknown"
        log(f"#{number} {outcome}: {summary} ({cost_text}, {stats['turns']} turns, "
            f"peak context {stats['peak_context_tokens'] // 1000}K, log {log_name})", self.agent.name)

        if outcome == "limited":
            sessions.update(self.agent.name, number, runs=done_runs)   # a limited run is not an iteration
            self._pause_for_limit(number, reset_at, ended)
            return "limited"

        if outcome == "escalate" and model != self.agent.model_escalate and not escalated:
            sessions.update(self.agent.name, number, escalated=True)
            reason = comment or summary
            self._comment(number, f"{self.agent.display_name}: escalating to {self.agent.model_escalate}", reason)
            follow_up = (f"You are continuing issue #{number} on a stronger model because the previous run "
                         f"escalated. Re-check where the work stands, then carry on. "
                         f"End with the same structured outcome.")
            return self._run(issue, follow_up, self.agent.model_escalate,
                             (result or {}).get("session_id"), escalated=True, repo=repo, shipping=shipping)
        if outcome == "escalate":
            outcome = "needs_input"
            comment = comment or f"<p>Escalation was requested on {model}, which is already the top model.</p>"

        if outcome == "ready_to_ship" and self.agent.ship_prompt and repo and not shipping:
            # Ship in a fresh session: the implementing session's context would be re-read on every ship call.
            log(f"#{number} ready to ship, starting a fresh ship session", self.agent.name)
            return self._run(issue, policy.render_ship_prompt(self.agent, issue, repo, handoff), model, None,
                             escalated=escalated, why="fresh ship session", repo=repo, shipping=True)
        if outcome == "ready_to_ship":
            outcome = "failed"
            summary = f"reported ready_to_ship where it can't be used: {summary}"

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

    def _pause_for_limit(self, number, reset_at, now):
        """Stop dispatching until the limit resets (or an exponential backoff); comment once if it is long."""
        self._limit_streak += 1
        seconds = policy.limit_pause_seconds(reset_at, self._limit_streak, now)
        self._paused_until = now + timedelta(seconds=seconds)
        until = self._paused_until.strftime("%Y-%m-%d %H:%M")
        log(f"#{number} hit a Claude usage limit: queue paused until {until}; re-queued without counting an "
            f"iteration", self.agent.name)
        if seconds > self.agent.limit_comment_hours * 3600 and not self.dry_run:
            try:
                self._comment(number, f"{self.agent.display_name}: paused on Claude usage limit",
                              f"<p>Claude's usage limit was reached. Talos is paused until about {until} and will "
                              f"then run this issue again; nothing is needed from you.</p>", escaped=True)
            except TalosError as e:
                log(f"#{number} limit comment failed: {e}", self.agent.name)

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
        self.leases.write([])                      # nothing is in flight after a restart
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
        self.release_held("reconnect")
