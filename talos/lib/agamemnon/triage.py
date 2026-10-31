"""Triage: where the orchestrator would hand an issue to the owner, an incident the classifier puts in the premise gets a
headless Claude Code session instead (the way Nestor runs), with the incident packet as its input and only `aga` to act
with. Outside the premise, past the limits, or when the session ends without resolving it, the issue goes to the owner
as before, with a comment.

The orchestrator's sweep only decides (`offer`); sessions run one at a time on their own thread, so a long session never
holds up merges or feeding the worker. The incident stays open while its session runs: the sweeps that see the issue
meanwhile leave it alone."""

import html
import json
import os
import queue
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..config import TALOS_DIR, TalosError, log
from . import classifier, journal, packet

OUTCOME_SCHEMA = TALOS_DIR / "schema" / "agamemnon-outcome.json"
ROLE_PROMPT = TALOS_DIR / "agents" / "agamemnon" / "role.md"
AGA = "dev-tools/agamemnon/aga"
INCIDENT_ENV = "AGA_INCIDENT"
REVIEW_STATUS = "in_review"
RESOLVED, OWNER, DECLINED = "resolved", "owner", "declined"
HANDBACK, MERGE, CONFLICT = "handback", "merge", "conflict"     # where an incident comes from


@dataclass
class Decision:
    """What `offer` did. taken: a triage session has (or already had) the issue, so the caller does nothing more.
    note: what to tell the owner when not taken, or None when this handback was already decided (say nothing)."""
    taken: bool
    note: str = None
    incident: int = None


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _at(stamp):
    """An ISO time as an aware datetime (the epoch when missing), so times with different offsets compare."""
    if not stamp:
        return datetime.min.replace(tzinfo=timezone.utc)
    moment = datetime.fromisoformat(str(stamp))
    return moment if moment.tzinfo else moment.astimezone()


def command(claude, model, max_turns, max_cost, role_prompt, schema):
    """The `claude -p` argv of a triage session: Bash with `aga` only, no MCP servers. The prompt goes on stdin."""
    return [claude, "-p", "--model", model, "--output-format", "json",
            "--json-schema", json.dumps(schema, separators=(",", ":")),
            "--append-system-prompt", role_prompt,
            "--permission-mode", "dontAsk",
            "--max-turns", str(max_turns), "--max-budget-usd", str(max_cost),
            "--tools", "Bash", "--allowedTools", f"Bash({AGA}:*)",
            "--strict-mcp-config"]


def run_text(path):
    """(summary, log) of a worker run from its log file: the run's own summary and comment, and Claude's result
    text; the raw tail when the log holds no result."""
    if not path or not Path(path).is_file():
        return "", ""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    for line in reversed(text.strip().splitlines()):
        try:
            result = json.loads(line)
        except ValueError:
            continue
        if isinstance(result, dict) and "session_id" in result:
            out = result.get("structured_output") or {}
            summary = " ".join(str(out.get(k) or "") for k in ("summary", "comment")).strip()
            return summary, str(result.get("result") or "")
    return "", packet.tail(text)


def prompt(incident_id, incident_type, spec, data):
    """The session's input: the incident, what the premise has Agamemnon do for it, and the packet."""
    return (f"Incident {incident_id} on issue #{data['issue']}, type `{incident_type}`.\n"
            f"The premise for this type: {spec.get('does') or '-'} (tiers: {', '.join(spec.get('tiers') or [])}).\n"
            f"Act only with `{AGA} <verb> --why \"...\"`; `{AGA} -h` lists the verbs, and the premise refuses the "
            f"ones this type does not allow.\n\nThe incident packet:\n```json\n"
            f"{json.dumps(data, indent=2, default=str)}\n```\n")


def parse(result):
    """(outcome, summary, comment, cost) of a session's JSON result; an error or a missing answer is OWNER."""
    result = result or {}
    cost = float(result.get("total_cost_usd") or 0)
    out = result.get("structured_output") or {}
    if result.get("is_error") or out.get("outcome") not in (RESOLVED, OWNER):
        why = str(result.get("result") or result.get("subtype") or "no answer")[-500:]
        return OWNER, f"the session ended without an answer: {why}", "", cost
    return out["outcome"], str(out.get("summary") or ""), str(out.get("comment") or ""), cost


class Triage:
    """Runs triage sessions for one orchestrator agent (Agamemnon). Ports: launch(prompt, model, incident id, issue)
    -> (Claude's JSON result or None, log path); run_log(issue) -> the worker's latest run log; in_flight() -> the
    issue numbers the worker is running."""

    def __init__(self, agent, tracker, my_id, owners, premise, launch, run_log, in_flight, now=_now,
                 dry_run=False, background=True):
        self.agent, self.tracker, self.premise = agent, tracker, premise
        self.settings = agent.orchestration
        self.my_id = str(my_id) if my_id else None
        self.owner = owners[0] if owners else None
        self.launch, self.run_log, self.in_flight = launch, run_log, in_flight
        self.now, self.dry_run, self.background = now, dry_run, background
        self._jobs = queue.Queue()
        self._thread = None
        self._lock = threading.Lock()

    def enabled(self):
        return bool(self.settings.aga_triage)

    # -- deciding (on the sweep's thread) --------------------------------------------------

    def offer(self, number, source, reason, detail, pr=None, since=None):
        """Take the incident into a triage session, or say why not. since: the handback's time, so a handback
        already decided is not decided again."""
        if not self.enabled():
            return Decision(False, "")
        earlier = journal.incidents(issue=number)
        running = [i for i in earlier if not i.get("closed_at")]
        if running:
            return Decision(True, f"triage in progress (incident {running[-1]['id']})", running[-1]["id"])
        if since and any(_at(i.get("opened_at")) >= _at(since) for i in earlier):
            return Decision(False, None)
        summary, log_text = run_text(self.run_log(number))
        incident_type = classifier.classify(reason, f"{detail or ''}\n{summary}", packet.tail(log_text))
        at = self.now()
        why_not = self._why_not(number, incident_type, at)
        if why_not:
            if not self.dry_run:
                incident = journal.open_incident(number, incident_type, at, source)
                journal.close_incident(incident, at, why_not, outcome=DECLINED)
            return Decision(False, why_not)
        model = self.model_for(incident_type)
        if self.dry_run:
            log(f"#{number} dry run, would triage a {incident_type} incident on {model}", self.agent.name)
            return Decision(False, f"would triage a {incident_type} incident (dry run)")
        incident = journal.open_incident(number, incident_type, at, source, model)
        log(f"#{number} {incident_type} incident {incident}: triage session on {model}", self.agent.name)
        self._comment(number, f"{self.agent.display_name}: triaging a {incident_type} incident",
                      f"<p>{html.escape(str(detail or reason or incident_type))}</p><p>This is in "
                      f"{html.escape(self.agent.display_name)}'s premise ({html.escape(self._spec(incident_type).get('does') or '')}), "
                      f"so a triage session (incident {incident}, {html.escape(model)}) looks at it before the owner "
                      f"does. Each action it takes is commented here; <code>talos agamemnon log --issue {number}"
                      f"</code> lists them.</p>")
        job = {"incident": incident, "issue": number, "type": incident_type, "model": model, "source": source,
               "pr": pr, "handback": {"reason": reason, "detail": detail},
               "run": {"summary": summary, "log": log_text}}
        if self.background:
            self._enqueue(job)
        else:
            self.run(job)
        return Decision(True, f"triage: {incident_type} incident {incident}", incident)

    def _why_not(self, number, incident_type, at):
        """Why the incident goes to the owner instead, or None."""
        if not self.premise.handles(incident_type):
            return f"incident type '{incident_type}' is outside {self.agent.display_name}'s premise"
        if journal.sessions_of(number, incident_type) >= self.settings.aga_sessions_per_type:
            return (f"a {incident_type} incident on this issue again, after {self.settings.aga_sessions_per_type} "
                    f"triage session(s)")
        spent = journal.cost_on(at[:10])
        if spent >= self.settings.aga_daily_cost_usd:
            return (f"today's triage cost ${spent:.2f} reached the daily cap "
                    f"${self.settings.aga_daily_cost_usd:.2f}")
        return None

    def model_for(self, incident_type):
        models = self.settings.aga_models
        return models.get(incident_type) or models.get("default") or "sonnet"

    def _spec(self, incident_type):
        return self.premise.incidents.get(incident_type, {})

    # -- running (on the triage thread) ----------------------------------------------------

    def _enqueue(self, job):
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name=f"triage-{self.agent.name}", daemon=True)
                self._thread.start()
        self._jobs.put(job)

    def _loop(self):
        while True:
            job = self._jobs.get()
            try:
                self.run(job)
            except Exception as e:      # one failed session never stops the next
                log(f"#{job['issue']} triage of incident {job['incident']} failed: {e}", self.agent.name)

    def run(self, job):
        """Run one session and close its incident; unresolved, the issue goes to the owner."""
        number, incident = job["issue"], job["incident"]
        data = packet.build(number, job.get("pr"), job["handback"], job["run"], None, None, None,
                            {n: [] for n in self.in_flight()}, [], incident=job["type"])
        try:
            result, log_path = self.launch(prompt(incident, job["type"], self._spec(job["type"]), data),
                                           job["model"], incident, number)
        except Exception as e:          # noqa: BLE001  the launch failed: the owner looks instead
            result, log_path = {"is_error": True, "result": str(e)}, None
        outcome, summary, comment, cost = parse(result)
        journal.close_incident(incident, self.now(), summary, outcome=outcome, cost_usd=cost)
        log(f"#{number} incident {incident} {outcome} (${cost:.2f}): {summary}"
            + (f"; log {log_path}" if log_path else ""), self.agent.name)
        if outcome == RESOLVED:
            self._comment(number, f"{self.agent.display_name}: incident {incident} resolved",
                          f"<p>{html.escape(summary)}</p>")
            return outcome
        self.to_owner(number, job["source"], f"incident {incident} not resolved",
                      comment or f"<p>{html.escape(summary)}</p>")
        return outcome

    def to_owner(self, number, source, title, body_html):
        """As without triage: an issue Agamemnon holds goes to the owner (in_review for merges and conflicts, its
        status kept for a handback); one already with the owner or the worker stays, and the comment says why."""
        try:
            issue = self.tracker.issue(number)
            if self.owner and str(issue.get("assignee") or "") == self.my_id:
                if source == HANDBACK:
                    self.tracker.assign(number, self.owner)
                else:
                    self.tracker.hand_off(number, REVIEW_STATUS, self.owner)
        except TalosError as e:
            log(f"#{number} not handed to the owner: {e}", self.agent.name)
        self._comment(number, f"{self.agent.display_name}: {title}, over to the owner", body_html)

    def recover(self):
        """Close the incidents whose session a Talos restart cut off; their issues go to the owner."""
        for incident in journal.incidents(open_only=True):
            if not incident.get("model"):
                continue
            journal.close_incident(incident["id"], self.now(), "interrupted by a Talos restart", outcome=OWNER)
            self.to_owner(incident["issue"], incident.get("source"), f"incident {incident['id']} interrupted",
                          "<p>Talos stopped while the triage session ran; please take a look.</p>")

    def _comment(self, number, title, body):
        try:
            self.tracker.comment(number, title, body)
        except TalosError as e:
            log(f"#{number} comment failed: {e}", self.agent.name)


def launcher(claude, agent_name, settings, workspace, run, env=None):
    """launch(prompt, model, incident id, issue) for real sessions: claude in the workspace root (where
    dev-tools/agamemnon/aga is), AGA_INCIDENT naming the incident, the raw output in state/logs (runner.run)."""
    role = ROLE_PROMPT.read_text(encoding="utf-8")
    schema = json.loads(OUTCOME_SCHEMA.read_text(encoding="utf-8"))

    def launch(text, model, incident, issue):
        cmd = command(claude, model, settings.aga_max_turns, settings.aga_max_cost_usd, role, schema)
        return run(cmd, {**os.environ, **(env or {}), INCIDENT_ENV: str(incident)}, agent_name, issue, text,
                   cwd=workspace)
    return launch
