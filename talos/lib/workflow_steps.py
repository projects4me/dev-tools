"""Workflow 'Run agent' steps: Gaia's workflow asks for a run (Hermes `workflow.agent.run.requested`, scoped to the
agent's own user), Talos claims the step, runs Claude on the step's issue with the step's prompt, session and model
and the agent's spec, and reports the outcome. The workflow, not Talos, then decides what happens to the issue.

The run itself is the dispatcher's (workspace, test slot, escalation, fresh ship session, CodeRabbit loop); only the
five outcomes Gaia knows leave Talos (see report). A step on any other record (a milestone, ...) is a record run
(record_runs.py): a scratch folder instead of a repo, and none of the issue flow."""

import dataclasses
import html
import queue
import threading
import time

from . import agent_spec, policy, record_runs
from .config import TalosError, log

STEP_EVENT = "workflow.agent.run.requested"
PENDING, CLAIMED = "pending", "claimed"
ISSUE_ENTITY = "issue"
BUSY_POLL_SECONDS = 15      # how often a claimed step looks again for its issue to be free


@dataclasses.dataclass
class StepRun:
    """One step as the dispatcher runs it; the dispatcher records a hand-off on it instead of writing the tracker."""
    step_id: str
    agent: object               # registry.Agent with the agent spec applied (with_spec)
    max_turns: int = 0          # the step's turn cap; 0 = the agent's
    outcome: str = None         # set by a hand-off: needs_input, blocked, failed or limit
    comment: str = ""
    summary: str = ""


def step_id(envelope):
    """The step a run request names, or None for any other envelope."""
    if not isinstance(envelope, dict) or envelope.get("eventName") != STEP_EVENT:
        return None
    return str((envelope.get("meta") or {}).get("stepId") or "") or None


def intents(user_id):
    """The Hermes intent for the agent's run requests: user-scoped, so only a socket signed in as the agent gets
    them."""
    return [{"projectId": f"user:{user_id}", "eventName": STEP_EVENT}]


def granted_tools(spec):
    """Claude Code tool names for the spec's tool grants: an MCP server (mcp__gaia) or one of its tools
    (mcp__gaia__get_issue); a runtime tool by its permission rule (Bash(git status:*)) or its name. Grants Claude
    Code can't use here (OpenAPI services, a whole builtin server) add nothing."""
    tools = []
    for grant in spec.get("toolGrants") or []:
        server, tool = grant.get("server") or {}, grant.get("tool") or {}
        if server.get("kind") == "mcp" and server.get("name"):
            name = f"mcp__{server['name']}" + (f"__{tool['name']}" if tool.get("name") else "")
        elif server.get("kind") == "builtin" and tool.get("name"):
            name = grant.get("permissionRule") or tool["name"]
        else:
            continue
        if name not in tools:
            tools.append(name)
    return tools


def with_spec(agent, spec):
    """The agent as its spec says: role prompt (with its granted skills), turn cap, runs per issue and permission
    mode where the spec sets them, and the granted tools on top of agent.json's allowedTools."""
    config = spec.get("config") or {}
    granted = [t for t in granted_tools(spec) if t not in agent.allowed_tools]
    return dataclasses.replace(
        agent,
        role_prompt=(config["rolePrompt"] + agent_spec.skills_prompt(spec) if config.get("rolePrompt")
                     else agent.role_prompt),
        max_turns=int(config.get("maxTurns") or agent.max_turns),
        max_iterations=int(config.get("maxRunsPerIssue") or agent.max_iterations),
        permission_mode=config.get("permissionMode") or agent.permission_mode,
        allowed_tools=[*agent.allowed_tools, *granted])


def report(dispatched, run):
    """(Gaia outcome, comment) for a finished run. A hand-off recorded on the run wins; a PR waiting for CodeRabbit
    is done (the CodeRabbit loop carries on and hands the issue to the reviewer itself)."""
    if run.outcome:
        return run.outcome, run.comment
    if dispatched in ("done", policy.AWAITING_BOT_REVIEW):
        return "done", f"<p>{html.escape(run.summary)}</p>" if run.summary else ""
    return "failed", f"<p>Talos ended the run with '{html.escape(str(dispatched))}'.</p>"


class StepRunner:
    """One agent's 'Run agent' steps: a queue of step ids and `maxParallel` workers, each running a step to its
    report. The issue is reserved on the dispatcher while a step runs, so a CodeRabbit round never runs beside it."""

    def __init__(self, agent, tracker, dispatcher, dry_run=False):
        self.agent = agent
        self.tracker = tracker
        self.dispatcher = dispatcher
        self.dry_run = dry_run
        self.user_id = tracker.user_id()
        self._queue = queue.Queue()
        self._pending = set()
        self._lock = threading.Lock()
        self._stopped = threading.Event()

    def stop(self):
        """Take no new steps (the agent left Gaia's list): they stay pending in Gaia; a step running finishes."""
        self._stopped.set()

    def on_event(self, envelope):
        """A run request from Hermes; other envelopes go to the dispatcher (status changes)."""
        sid = step_id(envelope)
        if sid is None:
            self.dispatcher.on_event(envelope)
            return
        self.submit(sid, "requested")

    def submit(self, sid, source):
        if self._stopped.is_set():
            return False
        with self._lock:
            if sid in self._pending:
                return False
            self._pending.add(sid)
        self._queue.put(sid)
        log(f"step {sid} queued ({source})", self.agent.name)
        return True

    def reconcile(self, project_ids=None):
        """(Re)connected: pick up the steps requested while the listener was down (Hermes keeps no history)."""
        for step in self.tracker.actor_steps(self.user_id, PENDING):
            self.submit(str(step["id"]), "reconcile")
        self.dispatcher.resync()

    def recover_interrupted(self):
        """Steps this agent had claimed when Talos stopped never get a report otherwise: report them failed."""
        if self.dry_run:
            return
        for step in self.tracker.actor_steps(self.user_id, CLAIMED):
            try:
                self.tracker.report_step(str(step["id"]), "failed",
                                         "<p>Talos stopped while this step was running. Check the branch before "
                                         "running it again.</p>")
                log(f"step {step['id']} was interrupted by a Talos restart: reported failed", self.agent.name)
            except TalosError as e:
                log(f"step {step['id']} interrupted, report failed: {e}", self.agent.name)

    def start(self):
        for n in range(self.agent.max_parallel):
            threading.Thread(target=self._loop, name=f"steps-{self.agent.name}-{n + 1}", daemon=True).start()

    def _loop(self):
        while True:
            sid = self._queue.get()
            try:
                self.handle(sid)
            except Exception as e:  # never let one step stop the worker
                log(f"step {sid} failed in Talos: {e}", self.agent.name)
            finally:
                with self._lock:
                    self._pending.discard(sid)

    # -- one step ------------------------------------------------------------------------------

    def handle(self, sid):
        """Claim the step, run it and report its outcome; returns the outcome reported, or None when skipped."""
        if self._stopped.is_set():
            log(f"step {sid} left pending: the agent takes no new runs", self.agent.name)
            return None
        step = self.tracker.actor_step(sid)
        if step.get("status") != PENDING or str(step.get("actorUserId") or "") != self.user_id:
            log(f"step {sid} skipped: {step.get('status')}, for user {step.get('actorUserId')}", self.agent.name)
            return None
        issue, repo, record, refusal = self._subject(step)
        if self.dry_run:
            what = f"#{issue['number']} in {repo}" if issue else record.key if record else f"nothing: {refusal}"
            log(f"step {sid} dry run, would claim and run {what}", self.agent.name)
            return None
        try:
            self.tracker.claim_step(sid)
        except TalosError as e:     # claimed already, or timed out: someone else's now
            log(f"step {sid} not claimed: {e}", self.agent.name)
            return None
        if refusal:
            return self._report(sid, "failed", f"<p>{html.escape(refusal)}</p>")
        try:
            spec = self.tracker.agent_spec(str(step.get("agentId") or ""))
        except TalosError as e:
            return self._report(sid, "failed", f"<p>Talos could not read the agent spec: {html.escape(str(e))}</p>")
        run = StepRun(sid, with_spec(self.agent, spec), max_turns=int(step.get("maxTurns") or 0))
        prompt, resume, model = str(step.get("prompt") or ""), step.get("session") == "resume", step.get("model")
        if record:
            try:
                record = record_runs.read(self.tracker, record.type, record.id)
            except TalosError as e:
                return self._report(sid, "failed", f"<p>Talos could not read the step's {html.escape(record.type)}: "
                                                   f"{html.escape(str(e))}</p>")
            return self._run(sid, step, run, record.key, f"{record.key} in a scratch folder",
                             lambda: self.dispatcher.run_record(record, prompt, resume, model, run))
        return self._run(sid, step, run, issue["number"], f"#{issue['number']} in {repo}",
                         lambda: self.dispatcher.run_step(issue, repo, prompt, resume, model, run))

    def _run(self, sid, step, run, key, what, attempt):
        """Run the step once its subject (issue number or record key) is free, again after a usage limit, and
        report it."""
        while not self.dispatcher.reserve(key):
            time.sleep(BUSY_POLL_SECONDS)       # e.g. a CodeRabbit round on the issue: the step runs after it
        try:
            log(f"step {sid} runs {what} (session {step.get('session') or 'new'})", self.agent.name)
            while True:
                dispatched = attempt()
                if dispatched != policy.LIMITED:
                    break
                if not self.dispatcher.wait_for_usage():    # a usage limit: the same step runs again once it is over
                    return self._report(sid, "limit", "<p>Talos stopped while waiting out a Claude usage limit.</p>")
        except Exception as e:      # broke inside Talos: report it now rather than leave the step to time out
            return self._report(sid, "failed", f"<p>The run failed in Talos: {html.escape(str(e))}</p>")
        finally:
            self.dispatcher.release(key)
        return self._report(sid, *report(dispatched, run))

    def _subject(self, step):
        """What the step is about: (issue, repo, None, None) for an issue in one of the agent's projects, (None, None,
        Record, None) for any other record (its details are read once the step is claimed), else (None, None, None,
        why not)."""
        entity_type, entity_id = str(step.get("entityType") or ""), str(step.get("entityId") or "")
        if not entity_type or not entity_id:
            return None, None, None, "This step names no record to run the agent on."
        if entity_type != ISSUE_ENTITY:
            return None, None, record_runs.Record(entity_type, entity_id), None
        try:
            issue = self.tracker.issue_by_id(entity_id)
        except TalosError as e:
            return None, None, None, f"Talos could not read the step's issue: {e}"
        repo = self.agent.repo_for(issue.get("project"))
        if not repo:
            return None, None, None, (f"Issue #{issue['number']} is in {issue.get('project')}, which is not one of "
                                      f"{self.agent.display_name}'s projects.")
        return issue, repo, None, None

    def _report(self, sid, outcome, comment):
        try:
            self.tracker.report_step(sid, outcome, comment)
            log(f"step {sid} reported {outcome}", self.agent.name)
        except TalosError as e:     # Gaia times the step out and takes its 'failed' port
            log(f"step {sid} report {outcome} failed: {e}", self.agent.name)
        return outcome
