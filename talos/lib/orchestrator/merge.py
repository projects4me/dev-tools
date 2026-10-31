"""How the orchestrator reaches GitHub to merge: find an issue's PR, and run `ai mergecheck` / `ai merge`."""

import json
import re

from ..ai_cli import ai_json
from ..config import TalosError
from . import github


class Merger:
    """The merge gate and the merge, as Agent Agamemnon. Gate results are `ai mergecheck --json`'s:
    {"state": ok|pending|fail, "sha", "checks": [{"name", "state", "detail"}]}; `ai merge` adds "merged"."""

    def __init__(self, env, gh):
        self.env = env          # env() -> the environment `ai` runs with, read fresh for each call
        self.gh = gh            # gh(*args) -> stdout of `gh` as Agamemnon (github.agamemnon_gh)

    def find_pr(self, repo, issue):
        """The PR from claude-issue-N in the repo as {"number", "merged", "merge_sha"}: the open one, else the most
        recently merged one (a merge whose bookkeeping failed is finished, not merged again), else None."""
        slug = github.known_repos().get(repo)
        if not slug:
            return None
        out = self.gh("pr", "list", "--repo", slug, "--head", f"claude-issue-{issue}", "--state", "all",
                      "--json", "number,state,mergeCommit")
        try:
            prs = json.loads(out or "[]")
        except ValueError:
            raise TalosError(f"gh pr list for claude-issue-{issue} in {repo}: {out[-300:]}")
        for state in ("OPEN", "MERGED"):
            found = sorted((p for p in prs if p.get("state") == state), key=lambda p: p["number"], reverse=True)
            if found:
                pr = found[0]
                return {"number": pr["number"], "merged": state == "MERGED",
                        "merge_sha": (pr.get("mergeCommit") or {}).get("oid")}
        return None

    def check(self, repo, pr):
        return ai_json(self.env(), "mergecheck", repo, str(pr))

    def merge(self, repo, pr):
        return ai_json(self.env(), "merge", repo, str(pr))

    def failed_jobs(self, repo, sha):
        """The failed GitHub Actions jobs on the commit: [{"name", "url", "run"}] (run: the workflow run's id)."""
        slug = github.known_repos().get(repo)
        if not slug:
            return []
        out = self.gh("api", f"repos/{slug}/commits/{sha}/check-runs", "--jq",
                      "[.check_runs[] | {name, status, conclusion, url: .details_url}]")
        try:
            runs = json.loads(out or "[]")
        except ValueError:
            raise TalosError(f"gh api check-runs for {repo}@{sha[:7]}: {out[-300:]}")
        jobs = []
        for run in runs:
            if run.get("status") != "completed" or run.get("conclusion") in ("success", "neutral", "skipped"):
                continue
            found = re.search(r"/actions/runs/(\d+)", run.get("url") or "")
            jobs.append({"name": run["name"], "url": run.get("url") or "", "run": found.group(1) if found else None})
        return jobs

    def rerun(self, repo, runs):
        """Re-run the failed jobs of each workflow run id."""
        slug = github.known_repos()[repo]
        for run in sorted(set(runs)):
            self.gh("run", "rerun", run, "--failed", "--repo", slug)
