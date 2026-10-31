"""Automatic merges: the CodeRabbit-clean PRs the worker's CodeRabbit loop handed to the orchestrator are merged, or
handed to the reviewer with the reason. Separate from feeding the worker (worker.Orchestrator), which calls it once
per sweep."""

import html
from datetime import datetime

from .. import sessions
from ..config import TalosError, log
from . import policy
from . import state as store


HELD = "held for the daily limit"


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


class MergeSweeper:
    """Merges as one orchestrator agent (kind orchestrator) for its worker (kind issues)."""

    def __init__(self, agent, worker, tracker, merger, my_id, owners, now=_now, dry_run=False,
                 worker_state=sessions.get, triage=None):
        self.agent, self.worker, self.tracker = agent, worker, tracker
        self.settings = agent.orchestration
        self.merger = merger            # merge.Merger: find PRs, run the merge gate, merge
        self.my_id = str(my_id) if my_id else None   # the orchestrator's Gaia user: issues handed to it to merge
        self.owner = owners[0] if owners else None   # who gets an issue that isn't merged
        self.now, self.dry_run = now, dry_run
        self.worker_state = worker_state    # the worker's Talos state per issue (conflict_noted: the keeper gave up)
        self.projects = list(worker.projects)
        self.triage = triage                # agamemnon.triage.Triage: looks before the owner does (None = off)

    def enabled(self):
        return bool(self.settings.auto_merge_milestones and self.merger and self.my_id)

    def sweep(self, paused=False):
        """One pass over the issues handed to the orchestrator to merge. While paused nothing is merged: they go to
        the reviewer instead of waiting on a paused orchestrator. Returns a short note ("" when there were none)."""
        if not self.enabled():
            return ""
        milestones = self.settings.auto_merge_milestones
        found = {}
        for milestone in milestones:
            for project in self.projects:
                for issue in self.tracker.issues_in(project, milestone):
                    if policy.is_merge_candidate(issue, milestones, self.projects, self.my_id):
                        found[issue["number"]] = issue
        notes = []
        for number in sorted(found):
            try:
                notes.append(f"#{number} {self._one(found[number], paused)}")
            except TalosError as e:     # GitHub, Gaia or ai failed: try again on the next sweep
                log(f"#{number} merge failed: {e}", self.agent.name)
                notes.append(f"#{number} error")
        return ", ".join(notes)

    def _one(self, issue, paused):
        number, s = issue["number"], self.settings
        repo = self.worker.repo_for(issue.get("project"))
        pr = self.merger.find_pr(repo, number) if repo else None
        if not pr:
            return self._hand_back(issue, None, f"no open PR from claude-issue-{number} in {repo}")
        if pr["merged"]:                # merged on an earlier pass whose bookkeeping failed, or by someone else
            return self._finish(issue, repo, pr["number"], pr.get("merge_sha") or "", None)
        if paused:
            return self._hand_back(issue, pr["number"], f"{self.agent.display_name} is paused", triage=False)
        record, waited = self._wait_record(number, repo, pr["number"])
        merged_today = len(store.merges_on(self.agent.name, self.now()[:10]))
        gate = self.merger.check(repo, pr["number"])
        # behind or conflicting only: the branch keeper brings it up to date or has it resolved, so wait for that,
        # unless it already gave up on a conflict at this head
        state = gate.get("state")
        if policy.only_ci_fails(gate):
            return self._ci_failed(issue, repo, pr["number"], gate, record)
        if policy.only_mergeability_fails(gate) and                 self.worker_state(self.worker.name, number).get("conflict_noted") != gate.get("sha"):
            state = "pending"
        step, why = policy.merge_step(state, waited, s.auto_merge_wait_minutes, merged_today,
                                      s.max_auto_merges_per_day)
        if step == policy.WAIT and state == "ok":     # the gate passed but the daily limit holds it until tomorrow
            return self._hold_for_limit(issue, pr["number"], why, record)
        if step == policy.WAIT:
            return "waiting (" + failing(gate, "pending" if state == gate.get("state") else "fail") + ")"
        if step == policy.HAND_BACK:
            detail = why if gate.get("state") == "ok" else f"{why}: {failing(gate)}"
            return self._hand_back(issue, pr["number"], detail, record)
        if self.dry_run:
            log(f"#{number} dry run, would merge PR {repo}#{pr['number']}", self.agent.name)
            return "would merge (dry run)"
        result = self.merger.merge(repo, pr["number"])
        if not result.get("merged"):          # the gate changed between the check and the merge
            if result.get("state") == "pending":
                return "waiting (" + failing(result, "pending") + ")"
            return self._hand_back(issue, pr["number"], f"the merge gate failed at merge time: {failing(result)}",
                                   record)
        return self._finish(issue, repo, pr["number"], result["merged"], record, result.get("sha") or gate.get("sha"))

    def _hold_for_limit(self, issue, pr, why, record):
        """Over the day's allowance: the issue stays with the merger for the next day's sweep. Noted and commented
        once per record."""
        number = issue["number"]
        if self.dry_run or not record or HELD in (record.get("note") or ""):
            return f"held ({why})"
        store.note_merge(record["id"], HELD)
        log(f"#{number} PR {pr} held: {why}", self.agent.name)
        self._comment(number, f"{self.agent.display_name}: merge held until tomorrow",
                      f"<p>The merge gate passed, but {html.escape(why)}. {html.escape(self.agent.display_name)} "
                      f"keeps the issue and merges PR #{pr} on the next day's allowance.</p>")
        return f"held ({why})"

    def _ci_failed(self, issue, repo, pr, gate, record):
        """The gate failed only on CI: re-run the failed job(s) once per PR head and wait; a second failure at the
        same head goes to the owner with the failing job and its log link."""
        number, sha = issue["number"], gate.get("sha") or ""
        minutes = 0
        if record and record.get("ci_rerun_at"):
            minutes = (datetime.fromisoformat(self.now()) - datetime.fromisoformat(record["ci_rerun_at"])
                       ).total_seconds() / 60
        step = policy.ci_step((record or {}).get("ci_rerun_sha"), minutes, sha)
        if step == policy.WAIT:
            return "waiting (CI re-run)"
        jobs = self.merger.failed_jobs(repo, sha)
        listed = ", ".join(f"{j['name']} ({j['url']})" if j["url"] else j["name"] for j in jobs) or failing(gate)
        runs = [j["run"] for j in jobs if j["run"]]
        if step == policy.HAND_BACK or not runs:
            why = "CI failed again after one re-run" if step == policy.HAND_BACK else "CI failed and has no job to re-run"
            return self._hand_back(issue, pr, f"{why}: {listed}", record)
        if self.dry_run:
            log(f"#{number} dry run, would re-run failed CI of PR {repo}#{pr}: {listed}", self.agent.name)
            return "would re-run CI (dry run)"
        self.merger.rerun(repo, runs)
        store.note_merge(record["id"], f"re-ran failed CI on {sha[:7]}: {listed}", ci_rerun_sha=sha, at=self.now())
        log(f"#{number} re-ran failed CI of PR {repo}#{pr}: {listed}", self.agent.name)
        self._comment(number, f"{self.agent.display_name}: re-running failed CI",
                      f"<p>The merge gate failed only on CI for <code>{html.escape(sha[:7])}</code>: "
                      f"{html.escape(listed)}. Re-ran the failed job(s) once; if they fail again the issue goes "
                      f"to the owner.</p>")
        return "re-ran failed CI"

    def _wait_record(self, number, repo, pr):
        """(the open auto_merges record or None, minutes it has waited). A dry run reads it without creating one, so
        it never starts the wait clock of the real run."""
        record = store.find_open_merge(self.agent.name, number, pr) if self.dry_run else \
            store.open_merge(self.agent.name, number, repo, pr, self.now())
        if not record:
            return None, 0
        started = datetime.fromisoformat(record["first_seen_at"])
        return record, (datetime.fromisoformat(self.now()) - started).total_seconds() / 60

    def _finish(self, issue, repo, pr, merge_sha, record, head_sha=None):
        """The PR is merged: close the record, set the issue done and say so (each step safe to repeat)."""
        number = issue["number"]
        if self.dry_run:
            log(f"#{number} dry run, PR {repo}#{pr} is merged: would set done", self.agent.name)
            return "would set done (dry run)"
        record = record or store.find_open_merge(self.agent.name, number, pr)
        if record:
            store.close_merge(record["id"], self.now(), "merged", merge_sha=merge_sha)
        self.tracker.set_status(number, "done")
        log(f"#{number} PR {repo}#{pr} merged as {merge_sha[:7]}; set done", self.agent.name)
        gate = f" and the merge gate passed on <code>{html.escape(head_sha[:7])}</code>" if head_sha else ""
        self._comment(number, f"{self.agent.display_name}: merged automatically",
                      f"<p>CodeRabbit was clean{gate}, so {html.escape(self.agent.display_name)} approved and merged "
                      f"PR {html.escape(repo)}#{pr} (merge commit <code>{html.escape(merge_sha[:7])}</code>). "
                      f"Milestone {html.escape(str(issue.get('milestone') or ''))} merges CodeRabbit-clean PRs "
                      f"without a review; revert the merge commit if this needs another look.</p>")
        return f"merged PR {repo}#{pr}"

    def _hand_back(self, issue, pr, detail, record=None, triage=True):
        """The owner reviews it as usual: in_review, assigned to them, with the reason; first a triage session when
        the incident is in Agamemnon's premise (the merge record stays open meanwhile)."""
        number = issue["number"]
        if triage and self.triage and self.triage.enabled():
            decision = self.triage.offer(number, "merge", None, detail, pr=pr)
            if decision.taken:
                return decision.note
            detail += f" (no triage: {decision.note})" if decision.note else ""
        if self.dry_run:
            log(f"#{number} dry run, would hand to the owner: {detail}", self.agent.name)
            return "would hand to the owner (dry run)"
        if record:
            store.close_merge(record["id"], self.now(), "handed_back", detail=detail)
        self.tracker.hand_off(number, policy.REVIEW_STATUS, self.owner)
        log(f"#{number} handed to the owner, not merged: {detail}", self.agent.name)
        self._comment(number, f"{self.agent.display_name}: not merged automatically, please review",
                      f"<p>{html.escape(detail)}.</p>" + (f"<p>PR #{pr}</p>" if pr else ""))
        return "handed to the owner"

    def _comment(self, number, title, body):
        try:
            self.tracker.comment(number, title, body)
        except TalosError as e:
            log(f"#{number} comment failed: {e}", self.agent.name)


def failing(gate, state="fail"):
    """The gate's checks in `state` as 'name: detail; …' (all not-ok checks when none is in that state)."""
    checks = gate.get("checks") or []
    picked = [c for c in checks if c.get("state") == state] or [c for c in checks if c.get("state") != "ok"]
    return "; ".join(f"{c['name']}: {c['detail']}" for c in picked) or gate.get("state") or "no result"
