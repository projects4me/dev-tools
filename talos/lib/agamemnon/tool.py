"""The `aga` verbs: the only way an Agamemnon session acts. Every verb passes verbs.check (premise tier, the issue
in an aga- milestone, the per-incident action cap, a reason) and its own preconditions; an acting verb is journaled
with the state before and after and how to undo it (see undo.py).

The outside world is reached through Ports, so the verbs can be tested without Gaia, GitHub or git."""

import html
import json
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .. import docker_state, workspaces
from ..config import TalosError
from ..policy import BASE_TEST_FAILED
from ..orchestrator import state as orchestrator_state
from . import branch_update, bugs, conflicts, journal, lint, narrative, verbs
from .classifier import BASE_LINT_FAILED
from .verbs import Refused

LOG_LINES = 80
DIFF_LINES = 400
BACKUP_REF = "refs/agamemnon/backup"
PUSH_REMOTE = "odysseus"
REVIEW_STATUS = "in_review"
CONFLICT_INCIDENT = "merge_conflict"
BASE_TEST_INCIDENT = BASE_TEST_FAILED      # the incident type is the worker's reason
BASE_LINT_INCIDENT = BASE_LINT_FAILED
HOLD_WITHOUT_PR = (BASE_TEST_INCIDENT, BASE_LINT_INCIDENT)     # handbacks before any PR
LINT_REPOS = ("prometheus",)            # the repos whose `ai test` has a lint suite
CONFLICT_ACTION = "merge_conflict"      # the worker's action for a conflict (orchestrator/keeper.py)
TEST_LINES = 40
CI_LOG_LINES = 150
# who the backup commits are by (Agamemnon has no git identity; the commit is never on a branch)
BACKUP_AUTHOR = {"GIT_AUTHOR_NAME": "Agent Agamemnon", "GIT_AUTHOR_EMAIL": "agent.agamemnon@projects4.me",
                 "GIT_COMMITTER_NAME": "Agent Agamemnon", "GIT_COMMITTER_EMAIL": "agent.agamemnon@projects4.me"}


@dataclass
class Settings:
    """What the verbs need to know about Agamemnon, its worker and their limits."""
    worker: object                      # registry.Agent of the worker (Odysseus)
    worker_id: str                      # the worker's Gaia user id
    my_id: str                          # Agamemnon's Gaia user id
    agent_name: str                     # "agamemnon": its automatic merges in talos.db count toward the daily cap
    display_name: str                   # "Agent Agamemnon"
    orchestration: object               # registry.Orchestration of Agamemnon
    premise: object                     # premise.Premise
    workspace_root: Path                # the workspace that holds .runs/issue-N
    docker_thresholds: tuple = (docker_state.DEFAULT_HIGH, docker_state.DEFAULT_OK)    # pressured at / ok below, %
    docker_hold_max_hours: float = 72   # `hold-until-docker` hands the issue to the owner after this
    hold_max_hours: float = 72          # `hold-until-done` hands the issue to the owner after this


@dataclass
class Ports:
    tracker: object                     # Gaia as Agamemnon: issue, assign, comment, status_of, create_issue, set_status
    merger: object                      # orchestrator.merge.Merger: find_pr, check, merge, failed_jobs, failed_logs, base_run, rerun
    gh: Callable                        # gh(*args) -> stdout, as Agamemnon
    git: Callable                       # git(cwd, *args, env=None, check=True) -> stdout (None when check=False fails)
    push: Callable                      # push(cwd, *refspecs): git push to the odysseus remote as Agent Odysseus
    worker_state: Callable              # worker_state(issue) -> the worker's talos.db issues row
    run_log: Callable                   # run_log(issue) -> path of the latest run's log, or None
    in_flight: Callable                 # in_flight() -> [issue numbers the worker is running]
    free_slot: Callable                 # free_slot() -> a free Gaia test slot number, or None
    now: Callable = field(default=lambda: datetime.now().astimezone())
    sleep: Callable = field(default=time.sleep)
    ody_git: Callable = None            # ody_git(cwd, *args, check=True): git as Agent Odysseus (commits it makes)
    test_changed: Callable = None       # test_changed(folder, repo) -> (passed, output): ai test <repo> --changed
    bot_review: Callable = None         # bot_review(issue, repo, pr): the PR back into the worker's CodeRabbit loop
    update_worker_state: Callable = None    # update_worker_state(issue, **fields): the worker's talos.db issues row
    docker_read: Callable = None        # docker_read() -> docker_state.read()'s dict
    docker_engine: Callable = None      # docker_engine("start"|"restart") -> docker_engine's result dict
    stale_containers: Callable = None   # stale_containers() -> [(issue, names, reason)] (docker_free.plan)
    stop_containers: Callable = None    # stop_containers(names): docker stop; raises OSError
    base_checkout: Callable = None      # base_checkout(issue, repo) -> the repo's folder in a throwaway checkout of
                                        # origin/<base> (workspaces.base_checkout)
    drop_checkout: Callable = None      # drop_checkout(issue): remove that throwaway checkout
    test_filter: Callable = None        # test_filter(folder, repo, filter) -> (passed, output): ai test --filter
    lint: Callable = None               # lint(folder, repo) -> (passed, output): ai test <repo> --suite lint


@dataclass
class Act:
    """What an acting verb did, for the journal."""
    result: dict
    before: dict
    after: dict
    undo: dict = None                   # None: cannot be undone; {"op": "none"}: nothing changed, nothing to undo
    undo_note: str = ""


def stamp(moment):
    return moment.isoformat(timespec="seconds")


class Aga:
    def __init__(self, settings, ports, incident_id):
        self.s, self.p = settings, ports
        self.incident_id = incident_id

    # -- entry -----------------------------------------------------------------------------

    def run(self, verb_name, args=None, why=None):
        """Run one verb for the session's incident; its result as a dict. Refused when a guard says no."""
        args = dict(args or {})
        incident = journal.incident(self.incident_id) if str(self.incident_id or "").isdigit() else None
        if not incident:
            verbs.check(verb_name, None, self.s.premise, {}, "", 0, 0, why)
        self.issue = self.p.tracker.issue(incident["issue"])
        self.incident = incident
        verb = verbs.check(verb_name, incident, self.s.premise, self.issue, self.s.orchestration.milestone_prefix,
                           journal.count(incident["id"]), self.s.orchestration.aga_max_actions, why)
        method = getattr(self, "_" + verb.name.replace("-", "_"))
        if not verb.acts:
            return method(args)
        act = method(args)
        at = stamp(self.p.now())
        action_id = journal.record(incident["id"], incident["issue"], verb.name, args, act.before, act.after,
                                   str(why).strip(), act.undo, act.undo_note, at)
        out = {**act.result, "action": action_id, "undo": act.undo_note}
        out["comment"] = self._post_comment(verb.name, args, str(why).strip(), act, action_id)
        return out

    def _post_comment(self, verb_name, args, why, act, action_id):
        """The issue's own record of the action. The action is already done and journaled, so a failed comment is
        reported in the result instead of raised."""
        try:
            self.p.tracker.comment(self.number, narrative.title(self.s, verb_name),
                                   narrative.body(self.s, self.incident, verb_name, args, why, act.result,
                                                  action_id, act.undo_note))
            return "posted"
        except Exception as e:      # noqa: BLE001
            return f"not posted: {e}"

    # -- shared reads ----------------------------------------------------------------------

    @property
    def number(self):
        return int(self.incident["issue"])

    def repo(self):
        repo = self.s.worker.repo_for(self.issue.get("project"))
        if not repo:
            raise Refused(f"#{self.number}: project '{self.issue.get('project')}' is not one of the worker's")
        return repo

    def folder(self):
        folder = workspaces.repo_folder(self.s.workspace_root, self.number, self.repo())
        if not folder.is_dir():
            raise Refused(f"#{self.number} has no workspace ({folder})")
        return folder

    def issue_state(self):
        """The issue's state undo compares against: status, assignee, and whether the worker is running it."""
        issue = self.p.tracker.issue(self.number)
        return {"status": issue.get("status"), "assignee": issue.get("assignee"),
                "running": bool(self.p.worker_state(self.number).get("running"))}

    def open_pr(self):
        pr = self.p.merger.find_pr(self.repo(), self.number)
        if not pr or pr.get("merged"):
            raise Refused(f"#{self.number} has no open PR")
        return pr

    def pr_head(self, pr):
        slug = workspaces.ai_common().REPOS[self.repo()]
        return self.p.gh("pr", "view", str(pr["number"]), "--repo", slug, "--json", "headRefOid",
                         "--jq", ".headRefOid").strip()

    def changed_files(self, folder):
        base = f"origin/{workspaces.base_branch(self.repo())}"
        out = self.p.git(folder, "diff", "--name-only", f"{base}...HEAD", check=False) or ""
        return sorted(set(out.split()))

    def hashes(self, folder, paths):
        return {p: self.p.git(folder, "hash-object", "--", p, check=False) if (folder / p).exists() else None
                for p in paths}

    # -- tier 0: read ----------------------------------------------------------------------

    def _status(self, args):
        st = self.p.worker_state(self.number)
        pr = self.p.merger.find_pr(self.repo(), self.number)
        return {"issue": self.number, "status": self.issue.get("status"), "assignee": self.issue.get("assignee"),
                "milestone": self.issue.get("milestone"), "incident": self.incident["type"],
                "talos": {k: st.get(k) for k in ("running", "runs", "handoff_reason", "handoff_detail",
                                                 "auto_retries", "next_action")},
                "hold": journal.active_hold(self.number), "pr": pr}

    def _logs(self, args):
        path = self.p.run_log(self.number)
        if not path or not Path(path).exists():
            return {"issue": self.number, "log": None}
        lines = int(args.get("lines") or LOG_LINES)
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        return {"issue": self.number, "log": str(path), "tail": "\n".join(text.splitlines()[-lines:])}

    def _diff(self, args):
        folder = self.folder()
        patch = self.p.git(folder, "diff", "HEAD") or ""
        lines = patch.splitlines()
        return {"issue": self.number, "stat": self.p.git(folder, "diff", "--stat", "HEAD"),
                "diff": "\n".join(lines[:DIFF_LINES]), "truncated": len(lines) > DIFF_LINES}

    def _mergecheck(self, args):
        pr = self.open_pr()
        return {"issue": self.number, "pr": pr["number"], "gate": self.p.merger.check(self.repo(), pr["number"])}

    def _ci_log(self, args):
        pr = self.open_pr()
        jobs = self.p.merger.failed_jobs(self.repo(), self.pr_head(pr))
        logs = {run: self.p.merger.failed_logs(self.repo(), run, CI_LOG_LINES)
                for run in sorted({j["run"] for j in jobs if j["run"]})}
        return {"issue": self.number, "pr": pr["number"],
                "jobs": [{"name": j["name"], "url": j["url"], "tail": logs.get(j["run"], {}).get(j["name"])}
                         for j in jobs]}

    def _base_ci(self, args):
        return self._read_base_ci()

    def _read_base_ci(self):
        pr = self.open_pr()
        failed = self.p.merger.failed_jobs(self.repo(), self.pr_head(pr))
        base = workspaces.base_branch(self.repo())
        runs = sorted({j["run"] for j in failed if j["run"]})
        latest = self.p.merger.base_run(self.repo(), runs[0], base) if runs else None
        if not latest:
            return {"issue": self.number, "pr": pr["number"], "base": base, "run": None,
                    "failing_on_head": [j["name"] for j in failed]}
        on_base = {j["name"]: j["conclusion"] for j in latest["jobs"]}
        broken = {name for name, c in on_base.items() if c not in ("success", "neutral", "skipped")}
        return {"issue": self.number, "pr": pr["number"], "base": base, "run": latest["run"], "sha": latest["sha"],
                "passed": latest["conclusion"] == "success", "failing_on_base": sorted(broken),
                "also_failing_on_base": sorted(broken & {j["name"] for j in failed})}

    def _workspace(self, args):
        folder = self.folder()
        upstream = self.p.git(folder, "rev-list", "--left-right", "--count", "HEAD...@{u}", check=False)
        return {"issue": self.number, "folder": str(folder),
                "branch": self.p.git(folder, "rev-parse", "--abbrev-ref", "HEAD"),
                "head": self.p.git(folder, "rev-parse", "HEAD"),
                "status": (self.p.git(folder, "status", "--porcelain") or "").splitlines(),
                "aheadBehind": upstream.split() if upstream else None}

    def _overlap(self, args):
        mine = set(self.changed_files(self.folder()))
        shared = {}
        for other in self.p.in_flight():
            if int(other) == self.number:
                continue
            folder = workspaces.path(self.s.workspace_root, other) / self.repo()
            if folder.is_dir():
                common = sorted(mine & set(self.changed_files(folder)))
                if common:
                    shared[str(other)] = common
        return {"issue": self.number, "files": sorted(mine), "overlap": shared}

    # -- tier 1: reversible and private ----------------------------------------------------

    def _backup(self, args):
        """Commit the workspace's tracked and untracked files (not ignored ones) to a backup ref through a scratch
        index, so HEAD, the index and the files are untouched; never a stash."""
        folder = self.folder()
        moment = self.p.now()
        ref = f"{BACKUP_REF}/{self.number}/{moment.strftime('%Y%m%d-%H%M%S')}"
        with tempfile.TemporaryDirectory() as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            self.p.git(folder, "read-tree", "HEAD", env=env)
            self.p.git(folder, "add", "-A", env=env)
            tree = self.p.git(folder, "write-tree", env=env)
        head = self.p.git(folder, "rev-parse", "HEAD")
        sha = self.p.git(folder, "commit-tree", tree, "-p", head, "-m",
                         f"Agamemnon backup of #{self.number} (incident {self.incident['id']})", env=BACKUP_AUTHOR)
        self.p.git(folder, "update-ref", ref, sha)
        try:
            self.p.push(folder, f"{ref}:{ref}")
        except Exception:               # not journaled, so leave nothing behind
            self.p.git(folder, "update-ref", "-d", ref, sha, check=False)
            raise
        return Act({"issue": self.number, "ref": ref, "sha": sha}, {"ref": None, "head": head},
                   {"ref": ref, "sha": sha, "head": head},
                   {"op": "delete_ref", "issue": self.number, "ref": ref, "sha": sha},
                   f"talos agamemnon undo <id> deletes {ref} here and on {PUSH_REMOTE}")

    def _restore(self, args):
        paths = list(args.get("paths") or [])
        allowed = set(self.s.premise.incidents.get(self.incident["type"], {}).get("generatedFiles") or [])
        if not paths:
            raise Refused("restore needs the generated files to restore")
        outside = [p for p in paths if p not in allowed]
        if outside:
            raise Refused(f"not known generated files for {self.incident['type']}: {', '.join(outside)} (the "
                          f"premise lists: {', '.join(sorted(allowed)) or 'none'})")
        backup = next((a for a in reversed(journal.actions(incident_id=self.incident["id"]))
                       if a["verb"] == "backup" and not a["undone_at"]), None)
        if not backup:
            raise Refused("take a backup first: restore needs one in this incident to undo from")
        folder = self.folder()
        before = self.hashes(folder, paths)
        self.p.git(folder, "checkout", "HEAD", "--", *paths)
        after = self.hashes(folder, paths)
        sha = backup["state_after"]["sha"]
        return Act({"issue": self.number, "restored": paths}, {"files": before}, {"files": after},
                   {"op": "checkout_files", "issue": self.number, "from": sha, "paths": paths},
                   f"talos agamemnon undo <id> puts the files back from backup {sha[:7]}")

    def _assign_worker(self, why_not_running):
        before = self.issue_state()
        if before["assignee"] == self.s.worker_id:
            raise Refused(f"#{self.number} is already assigned to {self.s.worker.display_name}")
        if before["running"]:
            raise Refused(f"#{self.number} has a run in flight ({why_not_running})")
        self.p.tracker.assign(self.number, self.s.worker_id)
        return before, self.issue_state()

    def _retry(self, args):
        before, after = self._assign_worker("nothing to retry")
        return Act({"issue": self.number, "assignee": self.s.worker_id}, before, after,
                   {"op": "assign", "issue": self.number, "assignee": before["assignee"]},
                   "talos agamemnon undo <id> gives the issue back to whom it was assigned to")

    def _send(self, args):
        notes = str(args.get("notes") or "").strip()
        if not notes:
            raise Refused(f"send needs --notes for {self.s.worker.display_name}")
        before = self.issue_state()
        if before["assignee"] == self.s.worker_id or before["running"]:
            raise Refused(f"#{self.number} is already with {self.s.worker.display_name}")
        self.p.tracker.comment(self.number, f"{self.s.display_name}: notes for {self.s.worker.display_name}",
                               "".join(f"<p>{html.escape(p)}</p>" for p in notes.split("\n\n")))
        undo = {"op": "assign", "issue": self.number}
        if self.incident["type"] == CONFLICT_INCIDENT:
            # the worker's run resolves the conflict (its merge_conflict action), and it counts as the one attempt
            # the orchestrator gives it, so the next conflict goes to the owner
            st = self.p.worker_state(self.number)
            undo["workerState"] = {"next_action": st.get("next_action") or "",
                                   "conflict_attempts": int(st.get("conflict_attempts") or 0)}
            self.p.update_worker_state(self.number, next_action=CONFLICT_ACTION,
                                       conflict_attempts=undo["workerState"]["conflict_attempts"] + 1)
        try:
            before, after = self._assign_worker("already with the worker")
        except Exception:
            if "workerState" in undo:
                self.p.update_worker_state(self.number, **undo["workerState"])
            raise
        return Act({"issue": self.number, "assignee": self.s.worker_id}, before, after,
                   {**undo, "assignee": before["assignee"]},
                   "talos agamemnon undo <id> gives the issue back to whom it was assigned to (the notes comment "
                   "stays)")

    def _rerun_ci(self, args):
        pr = self.open_pr()
        head = self.pr_head(pr)
        jobs = self.p.merger.failed_jobs(self.repo(), head)
        runs = [j["run"] for j in jobs if j.get("run")]
        if not runs:
            raise Refused(f"PR #{pr['number']} has no failed CI job on {head[:7]} to re-run")
        # the re-runs at this head share the merge sweep's attempts (its open auto_merges record counts them)
        record = orchestrator_state.find_open_merge(self.s.agent_name, self.number, pr["number"]) or {}
        spent, _ = orchestrator_state.ci_reruns_at(record, head)
        attempts = self.s.orchestration.ci_rerun_attempts
        if spent >= attempts:
            raise Refused(f"CI on {head[:7]} was already re-run {spent} time(s), the most allowed ({attempts}) "
                          f"per PR head")
        try:
            self.p.merger.rerun(self.repo(), runs)
        except Exception as e:
            if record:  # counted like the sweep's failed re-runs (ciRerunErrors)
                orchestrator_state.note_ci_rerun(record["id"], head, stamp(self.p.now()),
                                                 f"{self.s.display_name} could not re-run CI on {head[:7]} in "
                                                 f"triage: {e}", error=str(e))
            raise
        state = {"head": head, "failed": sorted(j["name"] for j in jobs), "reruns": spent + 1}
        if record:
            orchestrator_state.note_ci_rerun(record["id"], head, stamp(self.p.now()),
                                             f"{self.s.display_name} re-ran failed CI on {head[:7]} in triage: "
                                             f"{', '.join(state['failed'])}")
        return Act({"issue": self.number, "pr": pr["number"], "rerun": state["failed"]}, state, state,
                   {"op": "none"}, "nothing to undo: a CI re-run changes no code")

    def _wait_for(self, args):
        what = str(args.get("what") or "")
        limit = min(float(args.get("minutes") or self.s.orchestration.aga_wait_max_minutes),
                    self.s.orchestration.aga_wait_max_minutes)
        if what == "slot":
            ready = lambda: self.p.free_slot() is not None      # noqa: E731
        elif what.isdigit():
            done = {str(s).lower() for s in self.s.worker.dependency_done_statuses}
            ready = lambda: str(self.p.tracker.status_of(int(what)) or "").lower() in done     # noqa: E731
        else:
            raise Refused("wait-for needs an issue number or 'slot'")
        start = self.p.now()
        deadline = start + timedelta(minutes=limit)
        while not ready():
            if self.p.now() >= deadline:
                break
            self.p.sleep(self.s.orchestration.aga_poll_seconds)
        met = ready()
        waited = round((self.p.now() - start).total_seconds())
        return Act({"issue": self.number, "waitedFor": what, "ready": met, "seconds": waited}, {}, {},
                   {"op": "none"}, "nothing to undo: waiting changes nothing")

    def _holdable(self):
        """The issue's state when Agamemnon may hold it; Refused with a run in flight, in in_review or already held."""
        before = self.issue_state()
        if before["running"]:
            raise Refused(f"#{self.number} has a run in flight")
        if str(before["status"] or "").lower() == REVIEW_STATUS:
            raise Refused(f"#{self.number} is in_review: held by {self.s.display_name} it would look like a PR "
                          f"waiting to be merged")
        if journal.active_hold(self.number):
            raise Refused(f"#{self.number} is already held")
        return before

    def _hold_until_tomorrow(self, args):
        before = self._holdable()
        tomorrow = (self.p.now() + timedelta(days=1)).replace(
            hour=self.s.orchestration.aga_hold_until_hour, minute=0, second=0, microsecond=0)
        self.p.tracker.assign(self.number, self.s.my_id)
        journal.hold(self.number, stamp(tomorrow), before["assignee"])
        after = {**self.issue_state(), "heldUntil": stamp(tomorrow)}
        return Act({"issue": self.number, "heldUntil": stamp(tomorrow)}, before, after,
                   {"op": "release_hold", "issue": self.number, "assignee": before["assignee"]},
                   "talos agamemnon undo <id> releases the hold and gives the issue back to whom it was assigned to")

    def _hold_until_docker(self, args):
        before = self._holdable()
        latest = stamp(self.p.now() + timedelta(hours=self.s.docker_hold_max_hours))
        self.p.tracker.assign(self.number, self.s.my_id)
        journal.hold(self.number, latest, before["assignee"], journal.DOCKER)
        after = {**self.issue_state(), "heldUntil": latest}
        return Act({"issue": self.number, "heldUntil": latest, "condition": journal.DOCKER}, before, after,
                   {"op": "release_hold", "issue": self.number, "assignee": before["assignee"]},
                   "talos agamemnon undo <id> releases the hold and gives the issue back to whom it was assigned to")

    def _hold_until_done(self, args):
        """The blocked issue stays with Agamemnon (in review, with its open PR): only the journal hold is new. An
        issue with no PR yet (a base_test_failed handback) is assigned to Agamemnon for the hold."""
        blocker = str(args.get("blocker") or "").strip().lstrip("#")
        if not blocker.isdigit() or int(blocker) == self.number:
            raise Refused("hold-until-done needs the number of another issue to wait for")
        pr = self.p.merger.find_pr(self.repo(), self.number)
        if not pr or pr.get("merged"):
            return self._hold_without_pr(int(blocker))
        before = self.issue_state()
        if before["running"]:
            raise Refused(f"#{self.number} has a run in flight")
        if journal.active_hold(self.number):
            raise Refused(f"#{self.number} is already held")
        if str(before["assignee"] or "") != str(self.s.my_id):
            raise Refused(f"#{self.number} is not with {self.s.display_name}: the release merges the base into its "
                          f"PR only for an issue it holds")
        latest = stamp(self.p.now() + timedelta(hours=self.s.hold_max_hours))
        journal.hold(self.number, latest, before["assignee"], f"{journal.DONE}{int(blocker)}")
        after = {**before, "heldUntil": latest}
        return Act({"issue": self.number, "pr": pr["number"], "heldUntil": latest,
                    "condition": f"issue {int(blocker)} done"}, before, after,
                   {"op": "release_hold", "issue": self.number, "assignee": before["assignee"]},
                   "talos agamemnon undo <id> releases the hold; the issue stays with whom it was assigned to")

    def _hold_without_pr(self, blocker):
        """hold-until-done for an issue with no PR: when the blocker is done, the release merges the base into the
        issue's workspace and gives it back to the worker."""
        if self.incident["type"] not in HOLD_WITHOUT_PR:
            raise Refused(f"#{self.number} has no open PR: hold-until-done without one is for "
                          f"{' and '.join(HOLD_WITHOUT_PR)}")
        before = self._holdable()
        latest = stamp(self.p.now() + timedelta(hours=self.s.hold_max_hours))
        journal.hold(self.number, latest, before["assignee"], f"{journal.DONE}{blocker}")
        self.p.tracker.assign(self.number, self.s.my_id)
        after = {**self.issue_state(), "heldUntil": latest}
        return Act({"issue": self.number, "pr": None, "heldUntil": latest, "condition": f"issue {blocker} done"},
                   before, after, {"op": "release_hold", "issue": self.number, "assignee": before["assignee"]},
                   "talos agamemnon undo <id> releases the hold and gives the issue back to whom it was assigned to")

    # -- base test failures ----------------------------------------------------------------

    def _update_workspace(self, args):
        """Merge origin/<base> into the issue's branch in its workspace, for a branch with no PR yet; nothing is
        pushed, so the worker's next run tests (and later ships) the updated branch."""
        if self.issue_state()["running"]:
            raise Refused(f"#{self.number} has a run in flight")
        pr = self.p.merger.find_pr(self.repo(), self.number)
        if pr and not pr.get("merged"):
            raise Refused(f"#{self.number} has PR #{pr['number']}: update it on GitHub (update-branch)")
        folder, base = self.folder(), workspaces.base_branch(self.repo())
        result, before, after = branch_update.merge_local(self.p.git, self.p.ody_git, folder, base)
        if result == branch_update.CURRENT:
            raise Refused(f"the branch already has origin/{base} ({before[:7]}): updating again changes nothing; "
                          f"run the failing tests on the base (test-base)")
        if result == branch_update.CONFLICT:
            raise Refused(f"origin/{base} does not merge cleanly into the workspace (a conflict, or uncommitted "
                          f"changes in files the base changed); nothing was changed")
        return Act({"issue": self.number, "base": base, "from": before, "to": after}, {"head": before},
                   {"head": after}, None,
                   f"cannot be undone here: the workspace may hold the worker's uncommitted work; to take the merge "
                   f"back, run git reset --keep {before[:7]} in it by hand")

    def _test_base(self, args):
        """The named tests on a clean throwaway checkout of origin/<base>, never the issue's workspace. The run counts
        as one of the worker's runs (it waits for room under maxParallel); `ai test` takes its own test slot."""
        tests = _tests(args.get("tests"))
        if not tests:
            raise Refused("test-base needs --tests: the failing test names, comma-separated")
        repo = self.repo()
        sha, passed, output = self._on_base(repo, "the tests",
                                            lambda folder: self.p.test_filter(folder, repo, _filter(tests)))
        state = {"base": workspaces.base_branch(repo), "sha": sha, "tests": tests, "passed": bool(passed)}
        tail = "\n".join((output or "").splitlines()[-TEST_LINES:])
        return Act({"issue": self.number, **state, "output": tail}, {}, state, {"op": "none"},
                   "nothing to undo: the tests ran on a throwaway checkout that is already removed")

    def _lint_base(self, args):
        """lint (`ai test <repo> --suite lint`) on a clean throwaway checkout of origin/<base>, never the issue's
        workspace; it counts as one of the worker's runs. The notes say what to do next: when lint passes there and the
        worker's errors include invalid-todo-violation-rule, its diff made a recorded todo stale."""
        repo = self.repo()
        if repo not in LINT_REPOS:
            raise Refused(f"{repo} has no lint suite (lint-base is for {', '.join(LINT_REPOS)})")
        sha, passed, output = self._on_base(repo, "lint", lambda folder: self.p.lint(folder, repo))
        state = {"base": workspaces.base_branch(repo), "sha": sha, "passed": bool(passed),
                 "errors": [] if passed else lint.errors(output)}
        stale = bool(passed) and self._stale_todo()
        tail = "\n".join((output or "").splitlines()[-TEST_LINES:])
        return Act({"issue": self.number, **state, "staleTodo": stale, "notes": _lint_notes(state, stale),
                    "output": tail}, {}, state, {"op": "none"},
                   "nothing to undo: lint ran on a throwaway checkout that is already removed")

    def _on_base(self, repo, what, run):
        """(sha, passed, output) of run(folder) on a clean throwaway checkout of origin/<base>, removed afterwards.
        It counts as one of the worker's runs (it waits for room under maxParallel)."""
        self._wait_for_run()
        try:
            folder = self.p.base_checkout(self.number, repo)
            try:
                sha = self.p.git(folder, "rev-parse", "HEAD")
                passed, output = run(folder)
            finally:
                self.p.drop_checkout(self.number)
        except workspaces.WorkspaceError as e:
            raise Refused(f"no clean checkout of the base for {what}: {e}")
        except TalosError as e:         # not a failure: nothing is recorded, so file-bug can't take it for one
            raise Refused(f"{what} did not run on the base, so this says nothing about the base: {e}")
        return sha, passed, output

    def _stale_todo(self):
        """Whether the worker's handback or its latest run log names a recorded todo its diff made stale."""
        text = str(self.p.worker_state(self.number).get("handoff_detail") or "")
        path = self.p.run_log(self.number)
        if path and Path(path).is_file():
            text += "\n" + Path(path).read_text(encoding="utf-8", errors="replace")
        return lint.stale_todo(text)

    def _base_lint_failure(self, signature):
        """(error, base, sha) when this incident's latest lint-base run failed with the signature's error; Refused
        otherwise."""
        runs = [a for a in journal.actions(incident_id=self.incident["id"]) if a["verb"] == "lint-base"]
        last = (runs[-1]["state_after"] or {}) if runs else {}
        if not last:
            raise Refused("run lint-base first: a bug is filed only for a lint error that fails on the base branch")
        if last.get("passed"):
            raise Refused(f"lint passed on {last.get('base')} (lint-base): that is the branch's own failure, not a "
                          f"bug on the base branch")
        error = next((e for e in last.get("errors") or [] if signature in bugs.normalize(e)), None)
        if not error:
            raise Refused(f"lint-base did not report '{signature}' on {last.get('base')} (it reported: "
                          f"{'; '.join(last.get('errors') or []) or 'no error lines'})")
        return error, last.get("base"), last.get("sha")

    def _base_test_failure(self, signature):
        """(test, base, sha) when this incident's latest test-base run failed and ran the signature's test;
        Refused otherwise."""
        runs = [a for a in journal.actions(incident_id=self.incident["id"]) if a["verb"] == "test-base"]
        last = (runs[-1]["state_after"] or {}) if runs else {}
        if not last:
            raise Refused("run test-base first: a bug is filed only for a test that fails on the base branch")
        if last.get("passed"):
            raise Refused(f"the tests passed on {last.get('base')} (test-base): that is the branch's own failure, "
                          f"not a bug on the base branch")
        test = bugs.job_of(signature, last.get("tests") or [])
        if not test:
            raise Refused(f"test-base did not run '{signature}' (it ran: {', '.join(last.get('tests') or [])})")
        return test, last.get("base"), last.get("sha")

    # -- docker ----------------------------------------------------------------------------

    def docker_verdict(self, state):
        return docker_state.verdict(state, *self.s.docker_thresholds)

    def _docker(self, args):
        state = self.p.docker_read()
        return {"verdict": self.docker_verdict(state), "state": state}

    def _engine(self, action):
        before_state = self.p.docker_read()
        before = {"verdict": self.docker_verdict(before_state), "daemon": before_state["daemon"]}
        if action == "start" and before_state["daemon"]:
            raise Refused("the Docker daemon is already up; nothing to start")
        if action == "restart" and before["verdict"] != docker_state.DOWN:
            others = [n for n in self.p.in_flight() if n != self.number]
            if others:
                raise Refused(f"Docker is {before['verdict']} and runs are in flight (#"
                              f"{', #'.join(str(n) for n in others)}): a restart would kill them")
        result = self.p.docker_engine(action)
        after_state = self.p.docker_read()
        after = {"verdict": self.docker_verdict(after_state), "daemon": after_state["daemon"]}
        return Act(result, before, after, {"op": "none"},
                   f"nothing to undo: the engine was {action}ed; a stopped engine is not worth restoring")

    def _start_docker(self, args):
        return self._engine("start")

    def _restart_docker(self, args):
        return self._engine("restart")

    def _stop_stale_containers(self, args):
        try:
            chosen = self.p.stale_containers()
        except OSError as e:
            raise Refused(f"Docker could not be asked about test containers: {e}")
        if not chosen:
            raise Refused("no test container qualifies: each belongs to an issue with a run in flight, or one that "
                          "is not deferred, won't fix or unchanged for long enough")
        stopped, failed = [], []
        for issue, names, reason in chosen:
            try:
                self.p.stop_containers(names)
                stopped.append({"issue": issue, "containers": list(names), "reason": reason})
            except OSError as e:
                failed.append({"issue": issue, "containers": list(names), "error": str(e)})
        return Act({"stopped": stopped, "failed": failed}, {}, {"stopped": len(stopped)}, None,
                   "cannot be undone here: the containers were stopped, not removed, and the next `ai test` of "
                   "each issue starts them again")

    # -- tier 2: reversible and visible ----------------------------------------------------

    def _update_branch(self, args):
        pr = self.open_pr()
        head = self.pr_head(pr)
        slug = workspaces.ai_common().REPOS[self.repo()]
        self.p.gh("api", "-X", "PUT", f"repos/{slug}/pulls/{pr['number']}/update-branch",
                  "-f", f"expected_head_sha={head}")
        return Act({"issue": self.number, "pr": pr["number"], "from": head}, {"head": head},
                   {"head": head, "updateRequested": True}, None,
                   "cannot be undone here: taking the merge back needs a force push (tier 3); revert it by hand")

    def _push_merge(self, args):
        folder = self.folder()
        branch = workspaces.branch_name(self.number)
        if self.p.git(folder, "rev-parse", "--abbrev-ref", "HEAD") != branch:
            raise Refused(f"the workspace is not on {branch}")
        if (self.p.git(folder, "status", "--porcelain") or "").strip():
            raise Refused("the workspace has uncommitted changes: push only a finished, committed merge")
        head = self.p.git(folder, "rev-parse", "HEAD")
        parents = (self.p.git(folder, "rev-list", "--parents", "-n", "1", "HEAD") or "").split()[1:]
        if len(parents) < 2:
            raise Refused("HEAD is not a merge commit")
        remote_head = self.remote_head(folder, branch)
        self.p.push(folder, f"HEAD:refs/heads/{branch}")
        return Act({"issue": self.number, "pushed": head}, {"remoteHead": remote_head}, {"remoteHead": head}, None,
                   "cannot be undone here: taking a pushed commit back needs a force push (tier 3)")

    def remote_head(self, folder, branch):
        """The branch's head on the push remote (None when it has none); Refused when HEAD does not contain it, as
        the push would not be a fast-forward."""
        remote = (self.p.git(folder, "ls-remote", PUSH_REMOTE, f"refs/heads/{branch}") or "").split()
        remote_head = remote[0] if remote else None
        if remote_head and self.p.git(folder, "merge-base", "--is-ancestor", remote_head, "HEAD",
                                      check=False) is None:
            raise Refused(f"{PUSH_REMOTE}/{branch} ({remote_head[:7]}) is not in HEAD: that push would not be a "
                          f"fast-forward")
        return remote_head

    def _resolve_conflict(self, args):
        """Merge origin/<base> into the PR branch in the issue's own workspace and resolve the conflicts, only when
        conflicts.resolve finds every hunk additive; then the changed files' tests must pass before the merge is
        pushed as Agent Odysseus and the PR goes back through the CodeRabbit loop. Anything else is aborted and
        the workspace is put back as it was (Refused), so the worker can be sent instead."""
        pr = self.open_pr()
        folder = self.folder()
        repo, branch = self.repo(), workspaces.branch_name(self.number)
        self._wait_for_run()
        if self.p.worker_state(self.number).get("running"):
            raise Refused(f"#{self.number} has a run in flight: its workspace is in use")
        if self.p.git(folder, "rev-parse", "--abbrev-ref", "HEAD") != branch:
            raise Refused(f"the workspace is not on {branch}")
        if (self.p.git(folder, "status", "--porcelain") or "").strip():
            raise Refused("the workspace has uncommitted changes")
        if not any(a["verb"] == "backup" and not a["undone_at"]
                   for a in journal.actions(incident_id=self.incident["id"])):
            raise Refused("take a backup first: the workspace is put back from it if anything goes wrong")
        head = self.p.git(folder, "rev-parse", "HEAD")
        pr_head = self.pr_head(pr)
        if head != pr_head:
            raise Refused(f"the workspace is at {head[:7]}, the PR at {pr_head[:7]}: bring the workspace to the PR's "
                          f"head first")
        remote_head = self.remote_head(folder, branch)
        base = workspaces.base_branch(repo)
        self.p.git(folder, "fetch", "-q", "origin", base)
        merged = self.p.ody_git(folder, "merge", "--no-ff", "--no-commit", f"origin/{base}", check=False)
        try:
            if not self.p.git(folder, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False):
                raise Refused(f"nothing to merge: origin/{base} is already in {branch}")
            resolved = self._resolve_files(folder, merged is None)
            self.p.ody_git(folder, "commit", "--no-edit", "-m",
                           f"Merge origin/{base} into {branch}\n\n{self.s.display_name} resolved the additive "
                           f"conflicts in: {', '.join(r.path for r in resolved) or 'none'} (incident "
                           f"{self.incident['id']}).")
        except Exception:
            self.p.git(folder, "merge", "--abort", check=False)
            self.p.git(folder, "reset", "--hard", head, check=False)
            raise
        try:
            passed, output = self._test_changed(folder, repo)
        except Exception:
            self.p.git(folder, "reset", "--hard", head, check=False)
            raise
        if not passed:
            self.p.git(folder, "reset", "--hard", head)
            tail = "\n".join(str(output or "").splitlines()[-TEST_LINES:])
            raise Refused(f"the merge resolved, but the changed files' tests failed; the merge was dropped and the "
                          f"workspace is back at {head[:7]}:\n{tail}")
        merge = self.p.git(folder, "rev-parse", "HEAD")
        try:
            self.p.push(folder, f"HEAD:refs/heads/{branch}")
        except Exception:
            self.p.git(folder, "reset", "--hard", head, check=False)
            raise
        files = {r.path: r.hunks for r in resolved}
        result = {"issue": self.number, "pr": pr["number"], "pushed": merge, "resolved": files}
        try:
            self.p.bot_review(self.number, repo, pr["number"])
        except Exception as e:      # the push happened: journal it anyway, the session hands the issue on itself
            result["botReviewError"] = str(e)
        return Act(result,
                   {"head": head, "remoteHead": remote_head},
                   {"head": merge, "remoteHead": merge, "resolved": files}, None,
                   f"cannot be undone here: taking a pushed merge back needs a force push (tier 3); the branch "
                   f"before it is {head[:7]}, also in this incident's backup")

    def _wait_for_run(self):
        """A conflict test or test-base run counts as one of the worker's runs: wait (at most agaWaitMaxMinutes)
        until the worker has fewer than its maxParallel runs in flight; Refused when it still has not. It runs before
        the workspace checks, so a run that started on this issue during the wait is still caught."""
        cap = self.s.worker.max_parallel
        deadline = self.p.now() + timedelta(minutes=self.s.orchestration.aga_wait_max_minutes)
        while len(self.p.in_flight()) >= cap:
            if self.p.now() >= deadline:
                raise Refused(f"the worker has {cap} run(s) in flight (maxParallel) and none ended within "
                              f"{self.s.orchestration.aga_wait_max_minutes} min: no room for the test run, try "
                              f"again later")
            self.p.sleep(self.s.orchestration.aga_poll_seconds)

    def _test_changed(self, folder, repo):
        """ai test <repo> --changed, marked in the worker's talos.db row while it runs so docker_free's idle rule
        leaves the issue's test container alone; its end counts as the issue's last use."""
        self.p.update_worker_state(self.number, conflict_test_running=1, conflict_test_at=stamp(self.p.now()))
        try:
            return self.p.test_changed(folder, repo)
        finally:
            try:
                self.p.update_worker_state(self.number, conflict_test_running=0, conflict_test_at=stamp(self.p.now()))
            except Exception:   # keep the test's result; docker_free ignores a mark older than CONFLICT_TEST_MAX
                pass

    def _resolve_files(self, folder, conflicted):
        """Resolve and stage each conflicting file; Refused (naming the hunks) unless every one is additive."""
        if not conflicted:
            return []
        stages = {}
        for line in (self.p.git(folder, "ls-files", "-u", "-z") or "").split("\0"):
            if line.strip():
                meta, path = line.split("\t", 1)
                stages.setdefault(path, set()).add(meta.split()[2])
        if not stages:
            raise Refused("the merge failed without conflicting files (see the workspace)")
        problems, resolved = [], []
        for path, have in sorted(stages.items()):
            if have != {"1", "2", "3"}:
                problems.append(f"{path}: added, deleted or renamed on one side, not changed on both")
                continue
            self.p.git(folder, "checkout", "--conflict=diff3", "--", path)
            try:
                text = (folder / path).read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                problems.append(f"{path}: not UTF-8 text")
                continue
            result = conflicts.resolve(path, text)
            if not result.additive:
                problems.extend(f"{path} {p}" for p in result.problems)
                continue
            resolved.append(result)
        if problems:
            raise Refused("not every conflict is additive on both sides, so it is left to the worker:\n"
                          + "\n".join(problems))
        for result in resolved:
            with open(folder / result.path, "w", encoding="utf-8", newline="") as f:
                f.write(result.text)
            self.p.git(folder, "add", "--", result.path)
        return resolved

    def _file_bug(self, args):
        """A Bug for a failed CI job that fails on the base branch too, in the blocked issue's project and milestone,
        for the worker. base-ci is read again here, so the bug is filed only for what the base branch shows now. An
        open bug filed earlier for the same signature is linked instead of filing another."""
        title, body = str(args.get("title") or "").strip(), str(args.get("body") or "").strip()
        signature = bugs.normalize(args.get("signature"))
        if not title or not signature or not body:
            raise Refused("file-bug needs --title, --signature (the failing job and test) and --body")
        if self.incident["type"] == BASE_TEST_INCIDENT:
            job, base_name, base_sha = self._base_test_failure(signature)
        elif self.incident["type"] == BASE_LINT_INCIDENT:
            job, base_name, base_sha = self._base_lint_failure(signature)
        else:
            job, base_name, base_sha = self._base_ci_failure(signature)
        filed = journal.actions_of(bugs.FILE_BUG)
        project = self.issue.get("project")
        state = {"signature": signature, "job": job, "baseSha": base_sha, "project": project}
        done = self.s.worker.dependency_done_statuses
        existing = next((b for b in bugs.candidates(filed, signature, project)
                         if not bugs.is_closed(self.p.tracker.status_of(b), done)), None)
        if existing:
            return Act({"issue": self.number, "bug": existing, "linked": True}, state,
                       {**state, "bug": existing, "created": False}, {"op": "none"},
                       f"nothing to undo: #{existing} was filed earlier and only linked here")
        today = self.p.now().strftime("%Y-%m-%d")
        count = bugs.filed_on(filed, today)
        if count >= self.s.orchestration.aga_file_bugs_per_day:
            raise Refused(f"{count} bug(s) filed today already, the most allowed (agaFileBugsPerDay)")
        created = self.p.tracker.create_issue(
            project, title,
            bugs.description(self.s.display_name, self.number, signature, job, body, base_name, base_sha),
            bugs.BUG_TYPE, bugs.BUG_PRIORITY, bugs.BUG_COMPLEXITY, self.s.worker_id, self.issue.get("milestone"))
        bug = int(created["issueNumber"])
        return Act({"issue": self.number, "bug": bug, "url": created.get("url"), "linked": False}, state,
                   {**state, "bug": bug, "created": True},
                   {"op": "wont_fix", "issue": self.number, "bug": bug},
                   f"talos agamemnon undo <id> sets #{bug} to won't fix with a comment")

    def _base_ci_failure(self, signature):
        """(job, base, sha) when base-ci shows the signature's job failing on the base branch too; Refused
        otherwise."""
        base = self._read_base_ci()
        job = bugs.job_of(signature, base.get("also_failing_on_base") or [])
        if not job:
            raise Refused(f"base-ci does not show the job of '{signature}' failing on {base['base']} too (failing "
                          f"there and on the PR: {', '.join(base.get('also_failing_on_base') or []) or 'none'}): "
                          f"that is the PR's own failure, not a bug on the base branch")
        return job, base["base"], base.get("sha")

    def _merge(self, args):
        o = self.s.orchestration
        milestone = str(self.issue.get("milestone") or "").lower()
        if milestone not in {m.lower() for m in o.auto_merge_milestones}:
            raise Refused(f"milestone '{self.issue.get('milestone')}' is not one Agamemnon merges "
                          f"(autoMergeMilestones)")
        today = self.p.now().strftime("%Y-%m-%d")
        merged = len(orchestrator_state.merges_on(self.s.agent_name, today)) + journal.count_verb_on("merge", today)
        if merged >= o.max_auto_merges_per_day:
            raise Refused(f"{merged} merges today already, the most allowed (maxAutoMergesPerDay)")
        pr = self.open_pr()
        result = self.p.merger.merge(self.repo(), pr["number"])
        if not result.get("merged"):
            raise Refused(f"the merge gate did not pass ({result.get('state')}): "
                          + json.dumps(result.get("checks") or [])[:500])
        return Act({"issue": self.number, "pr": pr["number"], "merged": result.get("merged")},
                   {"head": result.get("sha")}, {"merged": result.get("merged")}, None,
                   "cannot be undone here: reverting a merge on the base branch is a person's call")


def _tests(value):
    """The test names of test-base's --tests: a list, or one comma-separated string."""
    items = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
    return [t for t in (" ".join(str(i).split()) for i in items) if t]


def _lint_notes(state, stale):
    """What lint-base's result means for the next step (and, when it passed, for the worker's notes)."""
    base, sha = state["base"], str(state.get("sha") or "")[:7]
    if not state["passed"]:
        return (f"lint fails on {base} ({sha}) too, {len(state['errors'])} error(s): file-bug with one of them as "
                f"--signature (or link the open one), then hold-until-done on that bug")
    if stale:
        return (f"lint passes on {base} ({sha}), so the base is not broken: the errors come from the branch. They "
                f"include invalid-todo-violation-rule: the diff changed code that a recorded .lint-todo entry covers, "
                f"so that todo is stale. Fix the violation (preferred), then clean the todo in the test container: "
                f"p4m test lint --clean-todo <file>. Then run the full suite again.")
    return (f"lint passes on {base} ({sha}), so the base is not broken: the errors come from the branch's own diff. "
            f"Fix them (lint fixes run in the test container, never a host npx), then run the full suite again.")


def _filter(tests):
    """`ai test --filter` for the tests: the name itself for one, a /regex/ of the names for several."""
    return tests[0] if len(tests) == 1 else "/" + "|".join(re.escape(t) for t in tests) + "/"
