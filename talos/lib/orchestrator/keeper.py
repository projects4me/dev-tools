"""Keeping agent PRs mergeable: once one PR merges, another open agent PR can fall behind its base or conflict with
it. A branch that is only behind is brought up to date on GitHub (a merge of the base, never a rebase); a conflict
goes back to the worker to resolve (merge_conflict), a limited number of times, then to the owner.

Conflict attempts are counted per issue: an issue is one PR in one repo (the house rule Nestor plans by), so the
issue's count is its PR's."""

import html
import json
from datetime import datetime

from .. import sessions
from ..config import TalosError, log
from . import github, policy
from . import state as store

CONFLICT_ACTION = "merge_conflict"     # the worker's action for it (agents/odysseus/agent.json)


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


class BranchKeeper:
    """Acts for one orchestrator agent on the open agent PRs (claude-issue-N) in its worker's repos."""

    def __init__(self, agent, worker, tracker, gh, worker_id, my_id, owners, branch_holder,
                 worker_state=sessions.get, update_worker_state=sessions.update, now=_now, dry_run=False,
                 triage=None):
        self.agent, self.worker, self.tracker, self.gh = agent, worker, tracker, gh
        self.settings = agent.orchestration
        self.worker_id, self.my_id = str(worker_id), str(my_id) if my_id else None
        self.owners = list(owners)
        self.holders = self.owners + ([self.my_id] if self.my_id else [])   # whose in_review issues it may act on
        self.branch_holder = branch_holder      # branch_holder(repo, issue) -> a folder outside Talos holding it
        self.worker_state, self.update_worker_state = worker_state, update_worker_state
        self.now, self.dry_run = now, dry_run
        self.projects = list(worker.projects)
        self.triage = triage                    # agamemnon.triage.Triage: looks before the owner does (None = off)

    def sweep(self):
        """One pass over the worker's repos. Returns a short note ("" when nothing needed doing)."""
        notes = []
        for repo, slug in github.worker_repos(self.worker).items():
            try:
                prs = self._open_prs(slug)
            except TalosError as e:
                log(f"PRs in {repo} not read: {e}", self.agent.name)
                continue
            for pr in prs:
                number = policy.issue_of_branch(pr.get("headRefName"))
                state = policy.mergeability(pr)
                if not number or state in (policy.UP_TO_DATE, policy.UNKNOWN):
                    continue
                try:
                    note = self._keep(repo, slug, number, pr, state)
                except TalosError as e:     # GitHub or Gaia failed: the next sweep tries again
                    log(f"#{number} PR {repo}#{pr['number']} not kept mergeable: {e}", self.agent.name)
                    note = "error"
                if note:
                    notes.append(f"#{number} {note}")
        return ", ".join(notes)

    def _open_prs(self, slug):
        out = self.gh("pr", "list", "--repo", slug, "--state", "open", "--limit", "100",
                      "--json", "number,headRefName,headRefOid,mergeable,mergeStateStatus")
        try:
            return json.loads(out or "[]")
        except ValueError:
            raise TalosError(f"gh pr list --repo {slug}: {out[-300:]}")

    def _keep(self, repo, slug, number, pr, state):
        issue = self.tracker.issue(number)
        worker_state = self.worker_state(self.worker.name, number)
        if policy.keep_skip_reason(issue, self.projects, self.holders, worker_state):
            return ""
        holder = self.branch_holder(repo, number)
        if holder:                          # someone works on it by hand: never touch that branch
            return f"left alone: {pr['headRefName']} is checked out in {holder}"
        if state == policy.BEHIND:
            return self._update(repo, slug, number, pr)
        return self._conflict(issue, repo, pr, worker_state)

    def _update(self, repo, slug, number, pr):
        """Merge the base into the branch on GitHub; CI and CodeRabbit then run on the new head."""
        if self.dry_run:
            log(f"#{number} dry run, would bring PR {repo}#{pr['number']} up to date", self.agent.name)
            return "would update the branch (dry run)"
        self.gh("api", "-X", "PUT", f"repos/{slug}/pulls/{pr['number']}/update-branch",
                "-f", f"expected_head_sha={pr['headRefOid']}")
        log(f"#{number} PR {repo}#{pr['number']} was behind its base: brought up to date", self.agent.name)
        return f"PR {repo}#{pr['number']} brought up to date"

    def _conflict(self, issue, repo, pr, worker_state):
        number = issue["number"]
        attempts = int(worker_state.get("conflict_attempts") or 0)
        step = policy.conflict_step(attempts, self.settings.max_conflict_attempts,
                                    worker_state.get("conflict_noted"), pr["headRefOid"])
        if step == policy.NOTHING:
            return ""
        if self.dry_run:
            log(f"#{number} dry run, PR {repo}#{pr['number']} conflicts: would "
                + ("hand it to the worker" if step == policy.RESOLVE else "tell the owner"), self.agent.name)
            return f"conflict: would {step} (dry run)"
        if step == policy.RESOLVE:
            return self._resolve(issue, repo, pr, attempts + 1)
        return self._tell(issue, repo, pr, attempts)

    def _resolve(self, issue, repo, pr, attempt):
        number = issue["number"]
        # the action first: the assignment wakes the worker, which reads it for that run; undone if that fails, so
        # a Gaia error neither uses up the attempt nor leaves the action for a later, unrelated run
        self.update_worker_state(self.worker.name, number, next_action=CONFLICT_ACTION, conflict_attempts=attempt)
        try:
            self.tracker.assign(number, self.worker_id)
        except TalosError:
            self.update_worker_state(self.worker.name, number, next_action="", conflict_attempts=attempt - 1)
            raise
        waiting = store.find_open_merge(self.agent.name, number, pr["number"])
        if waiting:                         # an automatic merge waits on this PR: it starts over after the fix
            store.close_merge(waiting["id"], self.now(), "conflict", detail="merge conflict handed to the worker")
        log(f"#{number} PR {repo}#{pr['number']} conflicts with its base: handed to {self.worker.display_name} "
            f"(attempt {attempt} of {self.settings.max_conflict_attempts})", self.agent.name)
        self._comment(number, f"{self.agent.display_name}: merge conflict, handed to {self.worker.display_name}",
                      f"<p>PR {html.escape(repo)}#{pr['number']} conflicts with its base branch now that other work "
                      f"was merged. {html.escape(self.worker.display_name)} merges the base into "
                      f"<code>{html.escape(pr['headRefName'])}</code>, resolves the conflicts so both changes "
                      f"survive, runs the tests and sends the PR through CodeRabbit again (automatic attempt "
                      f"{attempt} of {self.settings.max_conflict_attempts}).</p>")
        return f"conflict: handed to {self.worker.display_name}"

    def _tell(self, issue, repo, pr, attempts):
        number = issue["number"]
        self.update_worker_state(self.worker.name, number, conflict_noted=pr["headRefOid"])
        triage_note = ""
        if self.triage and self.triage.enabled():
            decision = self.triage.offer(number, "conflict", None,
                                         f"merge conflict: PR {repo}#{pr['number']} conflicts with its base after "
                                         f"{attempts} automatic resolution attempt(s)", pr=pr["number"])
            if decision.taken:
                return f"conflict: {decision.note}"
            triage_note = f" (no triage: {decision.note})" if decision.note else ""
        owner = self.owners[0] if self.owners else None
        if issue.get("assignee") == self.my_id and owner:      # it was waiting to be merged automatically
            self.tracker.hand_off(number, policy.REVIEW_STATUS, owner)
        log(f"#{number} PR {repo}#{pr['number']} conflicts and the automatic attempts are used up: told the owner",
            self.agent.name)
        self._comment(number, f"{self.agent.display_name}: merge conflict needs a person",
                      f"<p>PR {html.escape(repo)}#{pr['number']} conflicts with its base branch, and "
                      f"{attempts} automatic resolution attempt(s) were already used. Please resolve it, or run "
                      f"<code>dev-tools/talos/talos reset {html.escape(self.worker.name)} {number}</code> to allow "
                      f"another automatic attempt.</p>"
                      + (f"<p>{html.escape(triage_note.strip())}</p>" if triage_note else ""))
        return "conflict: told the owner" + triage_note

    def _comment(self, number, title, body):
        try:
            self.tracker.comment(number, title, body)
        except TalosError as e:
            log(f"#{number} comment failed: {e}", self.agent.name)
