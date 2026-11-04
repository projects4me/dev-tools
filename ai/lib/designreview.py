"""`ai design-review`: run the design-reviewer agent headless on a repo's change and record its verdict for the
exact tree it reviewed (reviewrecord), which `ai ship` turns into the PR body's design-review line.
"""

import os
import re
import subprocess

from . import headless, results, reviewrecord
from .common import WORKSPACE, AiError, base_branch, git, repo_dir, run_workspace, say

AGENT = "design-reviewer"
TOOLS = "Read,Grep,Glob"
MAX_TURNS = 40
TIMEOUT_SECONDS = 1200


def agent_file():
    """The design-reviewer definition of the workspace this runs in (`.claude/agents/design-reviewer.md`)."""
    path = (run_workspace() or WORKSPACE) / ".claude" / "agents" / f"{AGENT}.md"
    if not path.exists():
        raise AiError(f"no {AGENT} agent at {path}")
    return path


def parse_agent(text):
    """(model, instructions) from an agent file: its frontmatter `model` and the body after the frontmatter."""
    model, body = None, text
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
    if match:
        body = match.group(2)
        found = re.search(r"^model:\s*(\S+)", match.group(1), re.M)
        model = found.group(1) if found else None
    return model, body.strip()


def review_prompt(repo, folder, base, diff, untracked):
    new_files = "\n".join(untracked) or "(none)"
    return (f"Review the change in the repo `{repo}` (folder `{folder}`) against `origin/{base}`.\n"
            "The diff is below, so don't run git; read the repo's CLAUDE.md and any file you need with Read, Grep "
            "and Glob. Report in the format your instructions give.\n\n"
            f"New untracked files (read them):\n{new_files}\n\n```diff\n{diff}\n```\n")


def change(repo):
    """The working tree's change against the merge base with the base branch, and the untracked files."""
    base = base_branch(repo)
    merge_base = git(repo, "merge-base", f"origin/{base}", "HEAD")
    diff = git(repo, "diff", merge_base)
    untracked = sorted(git(repo, "ls-files", "-o", "--exclude-standard").splitlines())
    if not diff and not untracked:
        raise AiError(f"{repo} has no change against origin/{base} to review")
    return base, diff, untracked


def cmd_design_review(args):
    """Review the repo's change headless, record the verdict for the tree and print the report."""
    repo = args.repo
    started = results.tree_fingerprint(repo)
    base, diff, untracked = change(repo)
    model, instructions = parse_agent(agent_file().read_text(encoding="utf-8"))
    cwd = run_workspace() or WORKSPACE
    try:
        report = headless.ask(review_prompt(repo, os.path.relpath(repo_dir(repo), cwd), base, diff, untracked),
                              instructions, cwd, TOOLS, MAX_TURNS, TIMEOUT_SECONDS, model)
    except subprocess.TimeoutExpired:
        raise AiError(f"{AGENT} gave no report within {TIMEOUT_SECONDS // 60} minutes; nothing recorded")
    found = reviewrecord.verdict(report)
    stale = results.changed_since(repo, started)
    reviewrecord.record(repo, started, found, report, stale=stale)
    say(report)
    say(f"\ndesign review: {reviewrecord.describe(dict(found, stale=stale))} (recorded for this tree)")
    return 0 if found["clean"] and not stale else 1
