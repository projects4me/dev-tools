"""Issue lifecycle: start, time logging, commit, PR hand-off, tracker views.

Each command is a fixed sequence of steps that used to be separate tool calls.
"""

from datetime import datetime
from pathlib import Path

import sys

from .common import (AiError, PROJECT_FOR_REPO, REPOS, base_branch, config, git, git_dir, issue_repo,
                     load_timers, now, ody, repo_dir, save_timers, say, start_timer, strip_html, succeeds,
                     tracker_call)


def branch_name(issue):
    return f"claude-issue-{issue}"


# --- sync / start ---------------------------------------------------------------------------

def sync_dev(repo):
    """Fetch origin and fast-forward the local base branch (dev, or main for the tooling); returns its head."""
    base = base_branch(repo)
    git(repo, "fetch", "-q", "origin")
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
    else:
        git(repo, "checkout", "-q", "-b", branch)
        say(f"branch {branch} created from dev ({head})")
    tracker_call("update_issue", issue_number=issue, status="in_progress", assignee="me")
    start_timer(issue, repo)
    say(f"issue #{issue}: in_progress, timer started {now().strftime('%H:%M')}")
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
    entry["checkpoint"] = end.isoformat(timespec="seconds")
    save_timers(timers)
    say(f"logged {hours}h {mins}m on #{issue}: {text}")
    return minutes


def cmd_time(args):
    log_elapsed(args.issue, args.description, args.dry_run)


def cmd_timer(args):
    """Start or reset an issue's checkpoint without touching git or status."""
    start_timer(args.issue, args.repo or issue_repo(args.issue) or "gaia")
    say(f"timer for #{args.issue} set to {now().strftime('%H:%M')}")


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


def pr_hand_off(issue, url, commit, base, time_note=None, no_time=False, notes_html=""):
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
    tracker_call("update_issue", issue_number=issue, status="in_review", assignee=reviewer)
    say(f"issue #{issue}: comment added, in_review, assigned to {reviewer}")


def cmd_handoff(args):
    """Tracker hand-off for a PR that already exists (e.g. opened by hand)."""
    repo = args.repo
    commit = git(repo, "rev-parse", "--short", "HEAD")
    base = git(repo, "rev-parse", "--short", f"origin/{base_branch(repo)}")
    pr_hand_off(args.issue, args.url, commit, base, args.time_note, args.no_time)


# --- tracker views --------------------------------------------------------------------------

def cmd_issue(args):
    issue = tracker_call("get_issue", issue_number=args.issue)
    say(f"#{issue['issueNumber']} [{issue['status']}] {issue['subject']}")
    say(f"  {issue['project']} · {issue['type']} · {issue['priority']} · assignee {issue['assignee']} · "
        f"spent {issue.get('spent', {}).get('total', '?')}")
    say(f"  {issue.get('url', '')}")
    say("")
    say(strip_html(issue.get("description"), None if args.full else 1500))
    comments = issue.get("comments") or []
    shown = comments if args.full else comments[-args.comments:] if args.comments else []
    if comments:
        say(f"\n--- comments ({len(comments)}, showing {len(shown)}) ---")
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
