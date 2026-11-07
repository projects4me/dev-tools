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
            try:
                self.gh("run", "rerun", run, "--failed", "--repo", slug)
            except TalosError as e:     # keep gh's own message (a token without the Actions permission says so)
                raise TalosError(f"gh run rerun {run} --failed in {repo}: {str(e)[-400:]}")

    def failed_logs(self, repo, run, tail=150):
        """The tail of the failed steps' log of a workflow run, per job: {job name: last `tail` lines}. gh masks
        secrets in the log itself."""
        slug = github.known_repos()[repo]
        out = self.gh("run", "view", str(run), "--repo", slug, "--log-failed")
        lines = {}
        for line in (out or "").splitlines():
            job, _, rest = line.partition("\t")      # "<job>\t<step>\t<time> <text>"
            lines.setdefault(job, []).append(rest.partition("\t")[2] or rest)
        return {job: "\n".join(rows[-tail:]) for job, rows in lines.items()}

    def base_run(self, repo, run, base):
        """The latest completed run of the same workflow on the base branch:
        {"run", "sha", "conclusion", "jobs": [{"name", "conclusion"}]}, or None when there is none."""
        slug = github.known_repos()[repo]
        workflow = self.gh("api", f"repos/{slug}/actions/runs/{run}", "--jq", ".workflow_id").strip()
        out = self.gh("api", f"repos/{slug}/actions/workflows/{workflow}/runs?branch={base}&status=completed"
                      "&per_page=1", "--jq", "[.workflow_runs[] | {run: .id, sha: .head_sha, conclusion}]")
        try:
            runs = json.loads(out or "[]")
            if not runs:
                return None
            jobs = self.gh("api", f"repos/{slug}/actions/runs/{runs[0]['run']}/jobs?per_page=100", "--jq",
                           "[.jobs[] | {name, conclusion}]")
            return {**runs[0], "jobs": json.loads(jobs or "[]")}
        except ValueError:
            raise TalosError(f"gh api runs of workflow {workflow} on {base} in {repo}: {(out or '')[-300:]}")
