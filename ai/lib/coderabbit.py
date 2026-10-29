"""CodeRabbit's review state for a PR's head commit, read from GitHub (its own API is Enterprise-only)."""

import json
import re
import time

from .common import REPOS, config, ody, say
from .review import _api, _is_bot, open_threads, resolved_threads

STATUS_CONTEXT = "CodeRabbit"
PAUSED = re.compile(r"auto(?:matic)? reviews? (?:are |is )?paused|reviews? paused|rate limit", re.I)
EXIT_CODES = {"clean": 0, "findings": 1, "pending": 2, "paused": 3, "absent": 4}
POLL_SECONDS = 30


def head_sha(repo, pr):
    return ody(repo, "gh", "api", f"repos/{REPOS[repo]}/pulls/{pr}", "--jq", ".head.sha").strip()


def classify(status, issue_comments, review_comments, resolved, agent, waited_out=False):
    """The state for one snapshot. `status` is the newest CodeRabbit commit status on the head SHA (or None);
    `waited_out` means we polled until the timeout, so no status at all means CodeRabbit never ran."""
    state = (status or {}).get("state")
    if state != "success":
        bot_notes = [c["body"] or "" for c in issue_comments if _is_bot(c["user"])]
        if any(PAUSED.search(b) for b in bot_notes) and state != "pending":
            return {"state": "paused"}
        if state in ("failure", "error"):
            return {"state": "absent", "detail": f"status {state}: {status.get('description', '')}"}
        if status is None and waited_out:
            return {"state": "absent"}
        return {"state": "pending"}
    threads = [c for c in open_threads(review_comments, resolved, agent)[0] if _is_bot(c["user"])]
    if threads:
        return {"state": "findings", "open": len(threads), "comment_ids": [c["id"] for c in threads]}
    return {"state": "clean"}


def snapshot(repo, pr, sha, agent, waited_out=False):
    full = REPOS[repo]
    statuses = _api(repo, f"repos/{full}/commits/{sha}/statuses")
    status = next((s for s in statuses if s["context"] == STATUS_CONTEXT), None)   # newest first
    issue_comments = _api(repo, f"repos/{full}/issues/{pr}/comments")
    review_comments = _api(repo, f"repos/{full}/pulls/{pr}/comments")
    resolved = resolved_threads(repo, pr) if status and status["state"] == "success" else set()
    return classify(status, issue_comments, review_comments, resolved, agent, waited_out)


def cmd_rabbit(args):
    """Print (and exit with) clean / findings / pending / paused / absent for the PR's head commit."""
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
