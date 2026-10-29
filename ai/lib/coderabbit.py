"""CodeRabbit's review state for a PR's head commit, read from GitHub (its own API is Enterprise-only)."""

import json
import re
import time

from .common import REPOS, AiError, config, ody, say
from .review import _api, _is_bot, open_threads, resolved_threads

STATUS_CONTEXT = "CodeRabbit"   # the legacy commit status
CHECK_APP = "coderabbitai"      # the app slug of the check run that replaced it
PAUSED = re.compile(r"auto(?:matic)? reviews? (?:are |is )?paused|reviews? paused|rate limit", re.I)
EXIT_CODES = {"clean": 0, "findings": 1, "pending": 2, "paused": 3, "absent": 4}
POLL_SECONDS = 30


def enabled_repos():
    """The repos with the CodeRabbit loop (`codeRabbitRepos` in config.json); an unknown name is an error."""
    names = config().get("codeRabbitRepos", [])
    unknown = [n for n in names if n not in REPOS]
    if unknown:
        raise AiError(f"codeRabbitRepos names unknown repos: {', '.join(unknown)} (known: {', '.join(REPOS)})")
    return list(names)


def enabled(repo):
    """Does CodeRabbit review this repo's PRs? GitHub can't tell us, so the repos are listed in config.json."""
    return repo in enabled_repos()


def head_sha(repo, pr):
    return ody(repo, "gh", "api", f"repos/{REPOS[repo]}/pulls/{pr}", "--jq", ".head.sha").strip()


def review_status(check_runs, statuses):
    """CodeRabbit's report on the head SHA as one `{"state": pending|success|failure, "description"}` (or None).
    The check run from the coderabbitai app wins; the legacy commit status is the fallback."""
    runs = [r for r in check_runs if ((r.get("app") or {}).get("slug")) == CHECK_APP]
    if runs:
        run = max(runs, key=lambda r: r.get("started_at") or "")
        if run.get("status") != "completed":
            return {"state": "pending", "description": f"check run {run.get('status')}"}
        conclusion = run.get("conclusion") or "unknown"
        if conclusion == "success":
            return {"state": "success", "description": "check run completed"}
        return {"state": "failure", "description": f"check run {conclusion}"}
    return next((s for s in statuses if s["context"] == STATUS_CONTEXT), None)   # newest first


def classify(status, issue_comments, review_comments, resolved, agent, waited_out=False):
    """The state for one snapshot. `status` is `review_status()` for the head SHA (or None);
    `waited_out` means we polled until the timeout, so no status at all means CodeRabbit never ran."""
    state = (status or {}).get("state")
    if state != "success":
        bot_notes = [c["body"] or "" for c in issue_comments if _is_bot(c["user"])]
        if any(PAUSED.search(b) for b in bot_notes) and state != "pending":
            return {"state": "paused"}
        if state in ("failure", "error"):
            return {"state": "absent", "detail": f"{status.get('description', state)}"}
        if status is None and waited_out:
            return {"state": "absent"}
        return {"state": "pending"}
    threads = [c for c in open_threads(review_comments, resolved, agent)[0] if _is_bot(c["user"])]
    if threads:
        return {"state": "findings", "open": len(threads), "comment_ids": [c["id"] for c in threads]}
    return {"state": "clean"}


def snapshot(repo, pr, sha, agent, waited_out=False):
    full = REPOS[repo]
    payload = _api(repo, f"repos/{full}/commits/{sha}/check-runs")
    check_runs = payload.get("check_runs", []) if isinstance(payload, dict) else []
    statuses = [] if any(((r.get("app") or {}).get("slug")) == CHECK_APP for r in check_runs)         else _api(repo, f"repos/{full}/commits/{sha}/statuses")
    status = review_status(check_runs, statuses)
    issue_comments = _api(repo, f"repos/{full}/issues/{pr}/comments")
    review_comments = _api(repo, f"repos/{full}/pulls/{pr}/comments")
    resolved = resolved_threads(repo, pr) if status and status["state"] == "success" else set()
    return classify(status, issue_comments, review_comments, resolved, agent, waited_out)


def cmd_rabbit(args):
    """Print (and exit with) clean / findings / pending / paused / absent for the PR's head commit."""
    if not enabled(args.repo):
        result = {"state": "absent", "detail": f"CodeRabbit not enabled for {args.repo}"}
        say(json.dumps(result) if args.json else f"absent - {result['detail']}")
        return EXIT_CODES["absent"]
    agent = config()["agentLogin"]
    sha = head_sha(args.repo, args.pr)
    deadline = time.monotonic() + args.timeout * 60
    while True:
        result = snapshot(args.repo, args.pr, sha, agent)
        if not args.wait or result["state"] != "pending" or time.monotonic() >= deadline:
            break
        time.sleep(POLL_SECONDS)
    if args.wait and result["state"] == "pending":
        result = snapshot(args.repo, args.pr, sha, agent, waited_out=True)
    result["sha"] = sha
    if args.json:
        say(json.dumps(result))
    else:
        extra = f" ({result['open']} open: {', '.join(map(str, result['comment_ids']))})" if "open" in result else ""
        say(f"{result['state']}{extra} @ {sha[:7]}" + (f" - {result['detail']}" if "detail" in result else ""))
    return EXIT_CODES[result["state"]]
