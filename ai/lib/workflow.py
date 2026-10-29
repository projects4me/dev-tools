"""Issue lifecycle: start, time logging, commit, PR hand-off, tracker views.

Each command is a fixed sequence of steps that used to be separate tool calls.
"""

import json
import subprocess
from datetime import datetime
from pathlib import Path

import sys

from .common import (AiError, PROJECT_FOR_REPO, REPOS, STATE_DIR, base_branch, config, git, git_dir, issue_repo,
                     devstack_locked, load_timers, now, ody, repo_dir, run_workspace, save_timers, say, start_timer,
                     strip_html, succeeds, timers_locked, tracker_call)


def branch_name(issue):
    return f"claude-issue-{issue}"


# --- sync / start ---------------------------------------------------------------------------

def in_run_workspace(repo):
    """Whether the repo is a Talos per-issue worktree: there the base branch is never checked out (it may be
    checked out in the main checkout, and git refuses one branch in two worktrees)."""
    return repo != "pr4m" and run_workspace() is not None


def sync_dev(repo):
    """Fetch origin and fast-forward the local base branch (dev, or main for the tooling); returns its head.
    In a per-issue worktree only fetches, and returns the head of origin/<base>, the branch point."""
    base = base_branch(repo)
    git(repo, "fetch", "-q", "origin")
    if in_run_workspace(repo):
        return git(repo, "log", "--oneline", "-1", f"origin/{base}")
    git(repo, "checkout", "-q", base)
    git(repo, "merge", "--ff-only", "-q", f"origin/{base}")
    return git(repo, "log", "--oneline", "-1")


def cmd_sync(args):
    say(f"dev: {sync_dev(args.repo)}")


def cmd_start(args):
    issue, repo = args.issue, args.repo
    head = sync_dev(repo)
    branch = branch_name(issue)
    exists = git(repo, "rev-parse", "--verify", "--quiet", branch, check=False)
    if exists:
        git(repo, "checkout", "-q", branch)
        say(f"branch {branch} exists; checked out (not reset)")
    elif in_run_workspace(repo):
        git(repo, "checkout", "-q", "-b", branch, f"origin/{base_branch(repo)}")
        say(f"branch {branch} created from origin/{base_branch(repo)} ({head})")
    else:
        git(repo, "checkout", "-q", "-b", branch)
        say(f"branch {branch} created from dev ({head})")
    tracker_call("update_issue", issue_number=issue, status="in_progress", assignee="me")
    start_timer(issue, repo)
    say(f"issue #{issue}: in_progress, timer started {now().strftime('%H:%M')}")
    from . import doctor
    doctor.preflight(repo)
    cmd_issue(args)


# --- time -----------------------------------------------------------------------------------

def log_elapsed(issue, description, dry_run=False):
    """Log actual elapsed minutes since the issue's last checkpoint, then move the checkpoint."""
    timers = load_timers()
    entry = timers.get(str(issue))
    if not entry:
        raise AiError(f"no timer for #{issue}: run `ai start` or `ai timer {issue}` first")
    start = datetime.fromisoformat(entry["checkpoint"])
    end = now()
    minutes = max(1, round((end - start).total_seconds() / 60))
    tz = config().get("timezoneLabel", "")
    text = f"Actual elapsed {start.strftime('%H:%M')}-{end.strftime('%H:%M')} {tz}: {description}".replace("  ", " ")
    hours, mins = divmod(minutes, 60)
    if dry_run:
        say(f"[dry-run] would log {hours}h {mins}m on #{issue}: {text}")
        return minutes
    if start.date() != end.date():
        say(f"warning: checkpoint is from {start.date()}; logging {minutes}m anyway, check it")
    tracker_call("log_time", issue_number=issue, hours=hours, minutes=mins, description=text)
    with timers_locked():       # re-read: another run may have saved its own timer while the tracker call ran
        timers = load_timers()
        timers.setdefault(str(issue), entry)["checkpoint"] = end.isoformat(timespec="seconds")
        save_timers(timers)
    say(f"logged {hours}h {mins}m on #{issue}: {text}")
    return minutes


def cmd_time(args):
    log_elapsed(args.issue, args.description, args.dry_run)


def cmd_timer(args):
    """Start or reset an issue's checkpoint without touching git or status."""
    start_timer(args.issue, args.repo or issue_repo(args.issue) or "gaia")
    say(f"timer for #{args.issue} set to {now().strftime('%H:%M')}")


def cmd_devstack(args):
    """Run a command (a browser check) while holding the dev-stack lock, so parallel runs take turns on it."""
    command = [c for c in args.command if c != "--"] if args.command[:1] == ["--"] else args.command
    if not command:
        raise AiError("usage: ai devstack -- <command> [args...]")
    with devstack_locked():
        return subprocess.run(command, stdin=subprocess.DEVNULL).returncode


# --- commit / push --------------------------------------------------------------------------

def with_trailer(text):
    text = text.strip()
    trailer = config()["coAuthor"]
    if trailer not in text:
        text += "\n\n" + trailer
    return text + "\n"


def check_commit(repo, issue, files):
    """Refuse a commit that would break a house rule: wrong branch, never-commit file, base moved."""
    branch = git(repo, "branch", "--show-current")
    if branch != branch_name(issue):
        raise AiError(f"on branch '{branch}', expected '{branch_name(issue)}'")
    blocked = [f for f in files if f.replace("\\", "/") in config()["neverCommit"]]
    if blocked:
        raise AiError(f"refusing to commit: {', '.join(blocked)} (house rule)")
    base = base_branch(repo)
    git(repo, "fetch", "-q", "origin")
    if not succeeds(["git", "merge-base", "--is-ancestor", f"origin/{base}", "HEAD"], cwd=repo_dir(repo)):
        sha = git(repo, "rev-parse", "--short", f"origin/{base}")
        raise AiError(f"origin/{base} ({sha}) has moved past this branch: rebase onto origin/{base} first")
    return branch


def commit_files(repo, issue, message, files, push=True):
    """Stage exactly `files`, commit as Agent Odysseus with the co-author trailer, push to odysseus."""
    branch = check_commit(repo, issue, files)
    ody(repo, "git", "add", "--", *files)
    staged = git(repo, "diff", "--cached", "--name-only").splitlines()
    unexpected = sorted(set(staged) - {f.replace("\\", "/") for f in files})
    if unexpected:
        raise AiError(f"other files are staged, unstage them first: {', '.join(unexpected)}")
    msg_file = git_dir(repo) / "AI_COMMIT_MSG"
    msg_file.write_text(with_trailer(message), encoding="utf-8")
    ody(repo, "git", "commit", "-q", "-F", str(msg_file))
    say(git(repo, "show", "--stat", "--format=%h %an: %s", "HEAD"))
    if push:
        ody(repo, "git", "push", "-q", "-u", "odysseus", branch)
        say(f"pushed {branch} to odysseus (based on origin/{base_branch(repo)} "
            f"{git(repo, 'rev-parse', '--short', f'origin/{base_branch(repo)}')})")


def cmd_commit(args):
    text = Path(args.message_file).read_text(encoding="utf-8") if args.message_file else args.message
    commit_files(args.repo, args.issue, text, args.files, push=not args.no_push)


# --- PR hand-off ----------------------------------------------------------------------------

def open_pr(repo, issue, title, body):
    """Open the PR as Agent Odysseus (footer added); returns its URL."""
    body = body.rstrip()
    footer = config()["prFooter"]
    if footer not in body:
        body += "\n\n" + footer
    body_file = git_dir(repo) / "AI_PR_BODY.md"
    body_file.write_text(body + "\n", encoding="utf-8")
    url = ody(repo, "gh", "pr", "create", "--repo", REPOS[repo], "--base", base_branch(repo),
              "--head", branch_name(issue), "--title", title, "--body-file", str(body_file)).splitlines()[-1]
    say(f"PR: {url}")
    return url


def cmd_pr(args):
    """Open the PR as Agent Odysseus, then comment, log time, set in_review and assign the reviewer."""
    repo, issue = args.repo, args.issue
    url = open_pr(repo, issue, args.title, Path(args.body_file).read_text(encoding="utf-8"))
    commit = git(repo, "rev-parse", "--short", "HEAD")
    base = git(repo, "rev-parse", "--short", f"origin/{base_branch(repo)}")
    pr_hand_off(issue, url, commit, base, args.time_note, args.no_time)


def pr_hand_off(issue, url, commit, base, time_note=None, no_time=False, notes_html="", defer=None):
    """Post the PR notes and log time, then hand the issue to the reviewer; with defer (the repo name), leave it
    in progress with the agent and record the pending hand-off for `ai handoff`."""
    reviewer = config().get("reviewer")
    if not reviewer:                     # checked first, so nothing is posted twice on a retry
        raise AiError("no reviewer configured: run `dev-tools/installer/p4m configure` (sets ai/config.local.json)")
    number = url.rstrip("/").split("/")[-1]
    repo_name = url.split("github.com/")[-1].split("/pull/")[0].split("/")[-1]
    tracker_call("add_comment", issue_number=issue, comment=(
        f"<p><strong>PR opened:</strong> <a href=\"{url}\">{repo_name}#{number}</a> "
        f"(commit {commit}, based on {base}).</p>{notes_html}"))
    if not no_time:
        log_elapsed(issue, time_note or f"commit and PR {repo_name}#{number}")
    if defer:
        save_pending_handoff(issue, defer, url)
        say(f"issue #{issue}: comment added, hand-off deferred (still in_progress); finish with `ai handoff {issue}`")
        return
    finish_hand_off(issue)


def _pending_file(issue):
    return STATE_DIR / f"handoff-{issue}.json"


def save_pending_handoff(issue, repo, url):
    STATE_DIR.mkdir(exist_ok=True)
    _pending_file(issue).write_text(json.dumps({"repo": repo, "pr": url, "issue": issue}, indent=2), encoding="utf-8")


def load_pending_handoff(issue):
    p = _pending_file(issue)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def finish_hand_off(issue):
    """Set in_review, assign the reviewer and clear any pending record."""
    reviewer = config().get("reviewer")
    if not reviewer:
        raise AiError("no reviewer configured: run `dev-tools/installer/p4m configure` (sets ai/config.local.json)")
    tracker_call("update_issue", issue_number=issue, status="in_review", assignee=reviewer)
    _pending_file(issue).unlink(missing_ok=True)
    say(f"issue #{issue}: in_review, assigned to {reviewer}")


def cmd_handoff(args):
    """Finish a deferred hand-off (`ai ship --defer-handoff`): in_review, assigned to the reviewer."""
    pending = load_pending_handoff(args.issue)
    if pending:
        say(f"finishing the deferred hand-off of {pending['pr']}")
    else:
        say(f"no pending hand-off recorded for #{args.issue}; handing off anyway")
    finish_hand_off(args.issue)


# --- tracker views --------------------------------------------------------------------------

def cmd_issue(args):
    issue = tracker_call("get_issue", issue_number=args.issue,
                         comments=100 if args.full else max(1, min(args.comments, 100)))
    say(f"#{issue['issueNumber']} [{issue['status']}] {issue['subject']}")
    say(f"  {issue['project']} · {issue['type']} · {issue['priority']} · assignee {issue['assignee']} · "
        f"spent {issue.get('spent', {}).get('total', '?')}")
    say(f"  {issue.get('url', '')}")
    say("")
    say(strip_html(issue.get("description"), None if args.full else 1500))
    comments = issue.get("comments") or []
    shown = comments if args.full else comments[-args.comments:] if args.comments else []
    if comments:
        older = ", older ones not loaded (--comments N, up to 100)" if issue.get("olderComments") else ""
        say(f"\n--- comments ({len(comments)}, showing {len(shown)}{older}) ---")
    for c in shown:
        say(f"[{c['date'][:16]}] {c['by']}: {strip_html(c['comment'], None if args.full else 600)}")


def cmd_comment(args):
    text = Path(args.file).read_text(encoding="utf-8") if args.file != "-" else sys.stdin.read()
    tracker_call("add_comment", issue_number=args.issue, comment=text.strip())
    say(f"comment added to #{args.issue}")


def cmd_status(args):
    fields = {"issue_number": args.issue, "status": args.status}
    if args.assignee == "reviewer":          # the engineer's reviewer from config.local.json
        args.assignee = config().get("reviewer") or ""
        if not args.assignee:
            raise AiError("no reviewer configured: run `dev-tools/installer/p4m configure`")
    if args.assignee:
        fields["assignee"] = args.assignee
    tracker_call("update_issue", **fields)
    say(f"#{args.issue}: {args.status}" + (f", assignee {args.assignee}" if args.assignee else ""))


def default_project(repo):
    return PROJECT_FOR_REPO.get(repo, "A Gaia")
