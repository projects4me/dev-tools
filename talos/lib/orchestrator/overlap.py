"""Holding a ready issue back while an issue in flight edits the same files: parallel issues on one file were the
main source of merge conflicts. Deterministic (git and `ai context`, no Claude)."""

import html
import subprocess
from pathlib import Path

from .. import workspaces
from ..ai_cli import ai_json
from ..config import WORKSPACE, TalosError, log
from . import policy
from . import state as store
from .worker import _now


class IssueFiles:
    """The files an issue touches, as a set, or None when unknown: the branch's diff against its base once the branch
    has commits, else the files `ai context --json` resolves from the issue's text. Predictions are cached for one
    sweep (`clear()`)."""

    def __init__(self, env, root=WORKSPACE):
        self.env = env          # env() -> the environment `ai` runs with (the orchestrator's Gaia identity)
        self.root = Path(root)
        self._predicted = {}

    def clear(self):
        self._predicted = {}

    def __call__(self, repo, issue, started):
        """started: the issue is in flight, so its branch (if any) says more than its text."""
        return (self.branch(repo, issue) if started else None) or self.predicted(repo, issue)

    def branch(self, repo, issue):
        main = self.root / repo
        if not (main / ".git").exists():
            return None
        r = subprocess.run(["git", "diff", "--name-only", f"origin/{workspaces.base_branch(repo)}..."
                            f"{workspaces.branch_name(issue)}", "--"], cwd=main, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
        if r.returncode != 0:      # no such branch yet, or origin/<base> missing (not fetched here)
            log(f"#{issue}: no branch diff ({r.stderr.strip()[:200]})", "overlap")
            return None
        return {f.strip() for f in r.stdout.splitlines() if f.strip()} or None

    def predicted(self, repo, issue):
        key = (repo, issue)
        if key not in self._predicted:
            try:
                files = set(ai_json(self.env(), "context", str(issue), "--repo", repo).get("files") or [])
            except TalosError as e:
                log(f"#{issue}: no file prediction ({e})", "overlap")
                files = set()
            self._predicted[key] = files or None
        return self._predicted[key]


class OverlapGate:
    """Per sweep: begin() with the issues in the worker's milestones, then hold_reason() for each new candidate and
    assigned() for each one assigned, then end() to release the holds that cleared. A held issue gets one comment
    naming the issue and the files it waits for."""

    def __init__(self, agent, worker, tracker, files_of, dry_run=False, now=_now):
        self.agent, self.worker, self.tracker = agent, worker, tracker
        self.files_of = files_of        # files_of(repo, issue, started) -> set or None; clear() per sweep (IssueFiles)
        self.ignore = tuple(agent.orchestration.overlap_ignore)
        self.dry_run, self.now = dry_run, now
        self._active, self._held, self._passed, self._candidates = {}, set(), set(), set()

    def begin(self, issues, candidates):
        """issues: the sweep's issues (the started, unfinished ones are in flight, grouped by repo); candidates: the
        numbers of the new issues ready to assign."""
        self.files_of.clear()
        self._active, self._held, self._passed = {}, set(), set()
        self._candidates = set(candidates)
        for issue in issues:
            repo = self.worker.repo_for(issue.get("project"))
            if repo and policy.is_active(issue, self.worker.dependency_done_statuses):
                self._active.setdefault(repo, []).append(issue["number"])

    def hold_reason(self, issue):
        """Why the issue waits (and the hold recorded, the comment made once), or None when it may be assigned."""
        number, repo = issue["number"], self.worker.repo_for(issue.get("project"))
        mine = self.files_of(repo, number, False) if repo and self._active.get(repo) else None
        others = [(n, self.files_of(repo, n, True)) for n in self._active[repo] if n != number] if mine else []
        found = policy.overlapping(mine, others, self.ignore)     # nothing predicted: never hold on a guess
        if not found:
            self._passed.add(number)
            return None
        blocker, files = found
        self._held.add(number)
        reason = f"files overlap #{blocker}: {', '.join(files[:5])}" + (" …" if len(files) > 5 else "")
        if self.dry_run:
            return reason
        if store.hold(self.agent.name, number, repo, blocker, files, self.now()):
            log(f"#{number} held: {reason}", self.agent.name)
            try:
                self.tracker.comment(number, f"{self.agent.display_name}: held until #{blocker} is done",
                                     self._why(blocker, files))
            except TalosError as e:     # forget the hold so the next sweep comments again
                log(f"#{number} comment failed: {e}", self.agent.name)
                store.release(self.agent.name, [number])
        return reason

    def assigned(self, issue):
        """An issue assigned in this sweep is in flight for the candidates after it."""
        repo = self.worker.repo_for(issue.get("project"))
        if repo:
            self._active.setdefault(repo, []).append(issue["number"])

    def end(self):
        """Release the holds that cleared: the issue was checked and passed, is no longer a new candidate (assigned or
        changed), or its blocker is no longer in flight. A hold the sweep didn't reach (no slot left) stays.
        Returns the released issue numbers."""
        released = sorted(n for n, h in store.holds(self.agent.name).items()
                           if n not in self._held and (n in self._passed or n not in self._candidates
                                                       or h["blocker"] not in self._active.get(h["repo"], ())))
        if released and not self.dry_run:
            store.release(self.agent.name, released)
            log(f"released {', '.join(f'#{n}' for n in released)}", self.agent.name)
        return released

    def _why(self, blocker, files):
        listed = "".join(f"<li><code>{html.escape(f)}</code></li>" for f in files[:20])
        more = f"<li>… {len(files) - 20} more</li>" if len(files) > 20 else ""
        return (f"<p>Not assigned yet: issue #{blocker} is in flight in the same repo and touches the same files. "
                f"Working both at once would conflict on merge, so this issue waits until #{blocker} is done or its "
                f"PR is merged; the next sweep after that assigns it.</p><ul>{listed}{more}</ul>")
