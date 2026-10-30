"""One agent's work queue: re-read the issue, run Claude, apply the outcome to the tracker."""

import html
import json
import queue
import threading
import uuid
from datetime import datetime

from . import bot_review, memory, policy, pool, runner, runtime_check, sessions, workspaces
from .dependency_gate import DependencyGate
from .registry import Action
from .usage_pause import UsagePause
from . import runs as run_log
from .config import DEV_TOOLS, STATE_DIR, WORKSPACE, TalosError, log


def _now():
    return datetime.now().astimezone()


def _iso(moment):
    return moment.isoformat(timespec="seconds")


def lease_file(agent_name):
    """Where an agent's leases in flight are written, for `talos status`."""
    return STATE_DIR / agent_name / "leases.json"


def _runtime_copy():
    """The runtime copy Talos runs from (`p4m runtime`), or None when it runs from a checkout (P4M_RUNTIME=off)."""
    return DEV_TOOLS if DEV_TOOLS.name == workspaces.RUNTIME_NAME else None


SLOT_REPO = "gaia"      # the only repo whose test suites run in a slot


class Dispatcher:
    """Worker pool for one agent: `maxParallel` threads, each issue in its own workspace (or container) and test
    slot."""

    def __init__(self, agent, tracker, claude, dry_run=False, mcp=None, collector=None, merge_route=None):
        self.agent = agent
        self.tracker = tracker
        self.claude = claude
        self.dry_run = dry_run
        self.mcp = mcp          # the agent's MCP config file (mcp.mcp_config)
        self.collector = collector      # collector(issue or None): gc of finished issues, run by the release worker
        self.runner = runner.for_agent(agent)
        self.user_id = tracker.user_id()
        self._queue = pool.RunQueue()
        self.leases = pool.LeaseFile(lease_file(agent.name))
        # only gaia suites use test slots (adt-4159); a single worker needs none and keeps the shared pr4m_test
        slots = (agent.test_slots or agent.max_parallel) if agent.max_parallel > 1 else 0
        self._gate = pool.Gate(slots, agent.max_parallel_by_repo, agent.max_parallel_by_model,
                               on_change=self.leases.write, slot_repos={SLOT_REPO})
        self._pending = set()
        self._lock = threading.Lock()
        self._pause = UsagePause()  # no dispatching while a Claude usage limit holds
        self._deps = DependencyGate(agent.dependency_done_statuses)   # issues waiting on "Depends on" issues
        self._releases = queue.Queue()      # changed issue numbers (None = unknown) for the release worker
        # the CodeRabbit loop; merge_route(issue) names an orchestrator that merges a clean PR itself (or None)
        self.bot_review = bot_review.Watcher(agent, self.user_id, tracker, self.submit, route=merge_route)

    # -- queueing ----------------------------------------------------------------------------

    def on_event(self, envelope):
        """An issue assignment from Hermes: work it when it hands this agent work from an allowed user. A status change
        is only handed to the release worker: this runs on the listener thread, which must not wait on Gaia."""
        if policy.is_status_event(envelope):
            if self._deps.any_held() or self._collects():   # else no Gaia read is worth making
                self._releases.put(policy.event_issue_number(envelope))
            return
        number = policy.event_issue(envelope, self.agent, self.user_id)
        if number is not None:
            self.submit(number, f"assigned by user {envelope.get('actorId')}")

    def _release_loop(self):
        while True:
            changed = self._releases.get()
            try:
                self.release_held("status changed", changed)
            except Exception as e:  # never let one re-check stop the release worker
                log(f"held re-check failed: {e}", self.agent.name)
            self.collect_finished(changed)

    def _collects(self):
        return self.collector is not None and not self.dry_run

    def collect_finished(self, issue=None):
        """Remove the workspace and container leftovers of the issue that just changed status (all of them when it
        is None), if it is finished; a run in flight is never touched."""
        if not self._collects():
            return
        try:
            for number, action, reason in self.collector(issue):
                if action == "removed":
                    log(f"gc: removed #{number} ({reason})", self.agent.name)
        except Exception as e:      # housekeeping never stops the release worker
            log(f"gc failed: {e}", self.agent.name)

    def _recheck(self, dependencies):
        """A dependency may have turned done between reading its status and holding the issue; that status event found
        nothing held, so look again now that it is."""
        for dependency in dependencies:
            self._releases.put(dependency)

    def release_held(self, source, changed=None):
        """Queue held issues whose dependencies are now done; the rest stay held. Only the held issues that depend on
        `changed` (and those whose dependencies could not be read) are re-checked; all of them when it is None."""
        for number, reconcile, dependencies in self._deps.candidates(changed):
            try:
                waiting = self._deps.unmet(dependencies, self.tracker.statuses_of(dependencies))
            except TalosError as e:
                log(f"#{number} held re-check failed: {e}", self.agent.name)
                continue
            if not waiting:                 # handle() takes it out of the held set, and says if it isn't actionable
                self.submit(number, f"released ({source})", reconcile=reconcile)

    def submit(self, number, source, reconcile=False):
        """Queue an issue once; repeats while it is queued or running are dropped."""
        with self._lock:
            if number in self._pending:
                log(f"#{number} already queued or running ({source})", self.agent.name)
                return False
            self._pending.add(number)
        self._queue.put(None, (number, source, reconcile))      # ranked when a worker takes it, see _rank
        log(f"#{number} queued ({source})", self.agent.name)
        return True

    def _rank(self, job):
        """Where a queued job stands when a worker takes one: resumptions first, then by the issue's priority as it is
        now (see pool.order_key)."""
        number = job[0]
        try:
            issue = self.tracker.issue(number)
        except Exception:       # runs outside the worker's guard: an unreadable issue is ranked as new work
            return pool.order_key(False, None)
        action = self.agent.action_for(issue["status"])
        return pool.order_key(bool(action and action.resume), issue.get("priority"))

    def stop(self):
        """Release workers waiting out a usage-limit pause, and stop polling CodeRabbit."""
        self._pause.stop()
        self.bot_review.stop()

    def start(self):
        for n in range(self.agent.max_parallel):
            threading.Thread(target=self._loop, name=f"worker-{self.agent.name}-{n + 1}", daemon=True).start()
        threading.Thread(target=self._release_loop, name=f"release-{self.agent.name}", daemon=True).start()
        if not self.dry_run:
            self.bot_review.start()

    def _loop(self):
        while True:
            job = self._queue.get(self._rank)
            number, source, reconcile = job
            requeue = False
            try:
                if self._pause.wait():
                    log("usage-limit pause over, dispatching resumes", self.agent.name)
                requeue = self.handle(number, reconcile) == policy.LIMITED
            except Exception as e:  # never let one issue stop the worker
                log(f"#{number} failed in Talos: {e}", self.agent.name)
            finally:
                if requeue:       # a usage limit stopped the run: it runs first once the pause is over
                    self._queue.put(None, job, front=True)
                else:
                    with self._lock:
                        self._pending.discard(number)

    # -- one job -----------------------------------------------------------------------------

    def handle(self, number, reconcile=False):
        issue = self.tracker.issue(number)
        review = bot_review.get(self.agent.name, number)
        if review and not bot_review.still_waiting(review, issue, self.user_id):
            bot_review.drop(self.agent.name, number)    # someone moved it while it waited: work it as usual
            review = {}
        elif review.get("phase") == bot_review.WAITING:
            log(f"#{number} skipped: waiting for CodeRabbit on PR {review['pr']}", self.agent.name)
            return None
        dependencies = issue.get("dependsOn") or []
        try:
            statuses = self.tracker.statuses_of(dependencies)
        except TalosError as e:       # Gaia failed: that says nothing about the dependencies, so no comment
            self._deps.hold(number, reconcile, dependencies)
            self._recheck(dependencies)
            log(f"#{number} held: dependency status unreadable, retrying on the next status change ({e})",
                self.agent.name)
            return "held"
        override = sessions.get(self.agent.name, number).get("next_action") or None
        decision = policy.decide(issue, self.agent, self.user_id, require_allowed_modifier=reconcile,
                                 dependency_statuses=statuses, done_statuses=self._deps.done_statuses,
                                 action_override=override)
        if review and (decision.waiting_on or not decision.run):
            bot_review.drop(self.agent.name, number)    # held or not to be worked: out of the CodeRabbit loop
        if decision.waiting_on:
            return self._hold(number, decision, reconcile, dependencies)
        was_held = self._deps.clear(number)
        if override:            # an orchestrator's one-off choice (e.g. merge_conflict): this run or none, never a
            sessions.update(self.agent.name, number, next_action="")    # later, unrelated one
        if not decision.run:
            log(f"#{number} skipped: {decision.reason}" + (" (left the held set)" if was_held else ""),
                self.agent.name)
            return None
        if override and review:     # the orchestrator's action wins over a stale CodeRabbit round
            bot_review.drop(self.agent.name, number)
            review = {}
        bot_round = review.get("phase") == bot_review.ROUND
        if not bot_round:
            return self._work(number, issue, decision, decision.action)
        try:
            return self._work(number, issue, decision, Action(self.agent.bot_round_prompt, resume=True), review)
        except Exception:   # broke inside Talos: back to waiting (counted), so the watcher still ends the loop
            bot_review.undo_round(self.agent.name, number, self.bot_review.deadline(), uncount=False)
            raise

    def _work(self, number, issue, decision, action, review=None):
        """Prepare the workspace and run the action, or a bot round when `review` is the record it runs for."""
        bot_round = bool(review)
        state = sessions.get(self.agent.name, number)
        session_id, why = policy.resume_session(action, state, _now(), self.agent.resume_max_idle_minutes)
        if session_id and not self.runner.resumable:
            session_id, why = None, "new session (the runner keeps no sessions)"
        catch_up = action.resume and bool(state.get("session_id")) and not session_id
        model = policy.choose_model(self.agent, issue, escalated=bool(state.get("escalated")))
        if bot_round:
            prompt = policy.render_bot_round_prompt(self.agent, issue, decision.repo, review["pr"], review["rounds"],
                                                    catch_up=catch_up)
        else:
            prompt = policy.render_prompt(action, issue, decision.repo, catch_up=catch_up,
                                         reviewer=self.agent.reviewer_name)
        folder = workspace_error = None
        if not self.dry_run:                  # the workspace first: the pack is built from it
            try:
                folder = self.runner.prepare(number, decision.repo)
            except workspaces.BranchCheckedOut as e:
                if bot_round:
                    bot_review.drop(self.agent.name, number)    # handed to a person: out of the loop
                return self._branch_checked_out(number, e, decision.repo)
            except workspaces.WorkspaceError as e:     # the run itself reports it as a failed run
                log(f"#{number} workspace not ready before the run: {e}", self.agent.name)
                workspace_error = str(e)
        if decision.repo == "gaia" and not session_id and not bot_round:     # a resumed session already has it
            prompt = policy.with_pack(prompt, runner.gaia_pack(number, folder))
        return self._run(issue, prompt, model, session_id, why=why, repo=decision.repo,
                         workspace_error=workspace_error)

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
                       f"This did not count as an iteration.</p>", escaped=True, reason="branch_checked_out",
                       detail={"branch": error.branch, "folder": str(error.folder), "repo": repo})
        return "needs_input"

    def _hold(self, number, decision, reconcile, dependencies):
        """Keep the issue out of the queue until its dependencies are done; comment once per waiting set.
        Held time is not a run, so it counts toward neither iterations nor runs."""
        first = self._deps.hold(number, reconcile, dependencies, decision.waiting_on)
        self._recheck(dependencies)
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

    def _run(self, issue, prompt, model, session_id, escalated=False, why=None, repo=None, shipping=False,
             workspace_error=None):
        number = issue["number"]
        state = sessions.get(self.agent.name, number)
        done_runs = state.get("runs") or 0
        left = policy.iterations_left(self.agent, done_runs)
        if left == 0:
            return self._iteration_limit(number, done_runs)
        live_id = session_id or str(uuid.uuid4())
        limits = policy.run_limits(self.agent, model)
        guard = self.runner.runtime_guard(_runtime_copy())   # the copy the hook and the post-run check guard
        cmd = runner.build_command(self.runner.claude_bin(self.claude), self.agent, model, session_id,
                                   new_session_id=None if session_id else live_id, limits=limits,
                                   mcp=self.runner.mcp_config(self.mcp),
                                   memory=self.runner.memory_dir(memory.folder(WORKSPACE)),
                                   guard=guard)
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
            runtime_before = self._runtime_snapshot(number, guard)
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
        outcome, summary, comment, handoff, reset_at, reason = policy.read_outcome(result, ended)
        if workspace_error and reason == "run_error":      # the run failed because no workspace was ready
            reason = "talos_error"
        if outcome != policy.LIMITED:
            self._pause.succeeded()
        log_name = log_path.name if log_path else "none"
        run_log.finish(run_id, _iso(ended), round((ended - started).total_seconds(), 1), outcome, summary,
                       cost, reported, stats, log_path.name if log_path else None)
        cost_text = f"${cost:.2f}" if cost is not None else "cost unknown"
        log(f"#{number} {outcome}: {summary} ({cost_text}, {stats['turns']} turns, "
            f"peak context {stats['peak_context_tokens'] // 1000}K, log {log_name})", self.agent.name)
        changed, diff_log = self._runtime_changes(number, guard, runtime_before, log_path)
        if changed:
            if outcome == policy.LIMITED:
                self._pause_for_limit(number, reset_at)   # the usage limit still holds for the other issues
            return self._runtime_changed(number, changed, diff_log.name)

        if outcome == policy.LIMITED:
            sessions.update(self.agent.name, number, runs=done_runs)   # a limited run is not an iteration
            self._pause_for_limit(number, reset_at)
            return policy.LIMITED

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

        if outcome == policy.AWAITING_BOT_REVIEW:
            pr = policy.read_pr(result)
            if pr and repo:
                return self._await_bot_review(issue, repo, pr)
            outcome, summary = "failed", f"reported awaiting_bot_review without a PR number: {summary}"
        bot_review.drop(self.agent.name, number)       # any other end takes the issue out of the CodeRabbit loop

        if outcome == "done":
            self._check_handed_back(number, log_name)
        elif outcome in ("needs_input", "blocked"):
            title = "needs your input" if outcome == "needs_input" else "is blocked"
            self._hand_off(number, outcome, f"{self.agent.display_name} {title}", comment or summary,
                           reason=reason or ("agent_needs_input" if outcome == "needs_input" else "agent_blocked"))
        else:  # failed
            body = (f"<p>{html.escape(summary)}</p>"
                    f"<p>Talos log: <code>dev-tools/talos/state/logs/{html.escape(log_name)}</code></p>")
            if comment:
                body += f"<pre>{html.escape(comment)}</pre>"
            self._hand_off(number, "failed", f"{self.agent.display_name}: run failed", body, escaped=True,
                           reason=reason or "run_error")
        return outcome

    # -- the runtime copy (runtime_check.py) -----------------------------------------------------

    def _runtime_snapshot(self, number, guard):
        """The guarded runtime copy's state before a run, or None when there is none or it can't be read."""
        if not guard:
            return None
        try:
            return runtime_check.snapshot(guard)
        except runtime_check.RuntimeCheckError as e:
            log(f"#{number} runtime copy not checked: {e}", self.agent.name)
            return None

    def _runtime_changes(self, number, guard, before, log_path):
        """(what the run changed in the runtime copy, the log the diff went to); ([], None) for nothing."""
        if before is None:
            return [], None
        log_path = log_path or runner.new_log(self.agent.name, number)
        try:
            found, left = runtime_check.after_run(guard, before, log_path)
        except runtime_check.RuntimeCheckError as e:
            log(f"#{number} runtime copy not checked or restored after the run: {e}", self.agent.name)
            return [], None
        if found:
            log(f"#{number} changed the runtime copy ({'; '.join(found)}); "
                + (f"not fully restored: {'; '.join(left)}" if left else "restored"), self.agent.name)
        return found, log_path

    def _runtime_changed(self, number, found, log_name):
        """The run changed the tooling it ran on: whatever it reported, the issue goes to a person as blocked."""
        bot_review.drop(self.agent.name, number)
        items = "".join(f"<li><code>{html.escape(line)}</code></li>" for line in found)
        self._hand_off(number, "blocked", f"{self.agent.display_name}: changed the tooling in place",
                       f"<p>The run changed the runtime copy of dev-tools (<code>{workspaces.RUNTIME_NAME}</code>), "
                       f"the tooling Talos and its agents run on:</p><ul>{items}</ul>"
                       f"<p>Talos restored the copy and saved the diff in "
                       f"<code>dev-tools/talos/state/logs/{html.escape(log_name)}</code>. Tooling must not be edited "
                       f"or have branches checked out in place: it only changes through <code>p4m runtime "
                       f"update</code>, and a fault in dev-tools becomes a dev-tools issue. Runs in parallel share "
                       f"the copy, so check the diff belongs to this issue.</p>", escaped=True)
        return "blocked"

    def _await_bot_review(self, issue, repo, pr):
        """The PR is open with the hand-off deferred: the watcher polls CodeRabbit for it from here, without holding
        a run slot. The issue stays with the agent and nothing is posted to the tracker."""
        number = issue["number"]
        bot_review.wait(self.agent.name, number, repo, pr, issue["status"], self.bot_review.deadline())
        rounds = bot_review.get(self.agent.name, number).get("rounds") or 0
        log(f"#{number} waiting for CodeRabbit on {repo} PR {pr} ({rounds}/{self.bot_review.cap} bot rounds run)",
            self.agent.name)
        return policy.AWAITING_BOT_REVIEW

    def _pause_for_limit(self, number, reset_at):
        """Stop dispatching until the limit resets (or an exponential backoff); comment once if it is long. Workers
        that hit the same limit together find the pause already running and add nothing."""
        until = self._pause.hit(reset_at)
        if until is None:
            log(f"#{number} hit a Claude usage limit: queue already paused; re-queued without counting an "
                f"iteration", self.agent.name)
            return
        seconds = (until - _now()).total_seconds()
        until_text = until.strftime("%Y-%m-%d %H:%M")
        log(f"#{number} hit a Claude usage limit: queue paused until {until_text}; re-queued without counting an "
            f"iteration", self.agent.name)
        if seconds > self.agent.limit_comment_hours * 3600 and not self.dry_run:
            try:
                self._comment(number, f"{self.agent.display_name}: paused on Claude usage limit",
                              f"<p>Claude's usage limit was reached. Talos is paused until about {until_text} and will "
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
        bot_review.drop(self.agent.name, number)
        self._hand_off(number, "limit", f"{self.agent.display_name}: needs manual intervention",
                       f"<p>{self.agent.display_name} has had {runs} runs on this issue, the limit is "
                       f"{self.agent.max_iterations}. It needs a human look before more agent work. To give it "
                       f"another {self.agent.max_iterations}, run <code>dev-tools/talos/talos reset "
                       f"{html.escape(self.agent.name)} {number}</code> and assign the issue back.</p>",
                       escaped=True, reason="iteration_limit")
        return "limit"

    # -- tracker side effects ------------------------------------------------------------------

    def _comment(self, number, title, body, escaped=False):
        self.tracker.comment(number, title, body if escaped or body.lstrip().startswith("<")
                             else f"<p>{html.escape(body)}</p>")

    def _hand_off(self, number, outcome, title, body, escaped=False, reason=None, detail=None):
        """Comment, set the status and assignee, and store the reason code (and its detail) of the handback."""
        status = self.agent.statuses[outcome]
        if self.dry_run:
            log(f"#{number} dry run, would hand off: status '{status}', assign {self.agent.handoff_to}",
                self.agent.name)
            return
        sessions.update(self.agent.name, number, handoff_reason=reason, handoff_at=_iso(_now()),
                        handoff_detail=json.dumps(detail) if detail else None)
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
                           f"Talos log: <code>{html.escape(log_name)}</code></p>", escaped=True,
                           reason="not_handed_back")

    # -- startup -------------------------------------------------------------------------------

    def recover_interrupted(self):
        """Runs cut off by a Talos restart go back to the human, not silently restarted."""
        self.leases.write([])                      # nothing is in flight after a restart
        for number in sessions.interrupted(self.agent.name):
            sessions.update(self.agent.name, number, running=False, last_ended_at=_iso(_now()))
            run_log.close_unfinished(self.agent.name, number, _iso(_now()))
            log(f"#{number} was interrupted by a Talos restart", self.agent.name)
            bot_review.drop(self.agent.name, number)
            self._hand_off(number, "failed", f"{self.agent.display_name}: run interrupted",
                           "<p>Talos stopped while this issue was being worked on. Check the branch, "
                           "then assign it back to resume.</p>", escaped=True, reason="interrupted")
        self.bot_review.recover()           # issues waiting for CodeRabbit are polled again by the watcher

    def reconcile(self, project_ids):
        """Pick up issues assigned while the listener was down (Hermes keeps no history)."""
        for name, pid in project_ids.items():
            for number in self.tracker.assigned_issue_numbers(pid, self.user_id):
                self.submit(number, f"reconcile {name}", reconcile=True)
        self._releases.put(None)        # status changes may have been missed while the listener was down
