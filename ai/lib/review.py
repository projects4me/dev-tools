"""PR review: list what the reviewer said, and post replies in one go."""

import json
from pathlib import Path

from .common import REPOS, config, ody, say


def _api(repo, path, *extra):
    out = ody(repo, "gh", "api", path, "--paginate", *extra)
    return json.loads(out) if out else []


def cmd_review(args):
    """Print reviews and inline comments; mark threads the agent already answered."""
    repo, pr = args.repo, args.pr
    agent = config()["agentLogin"]
    full = REPOS[repo]
    reviews = _api(repo, f"repos/{full}/pulls/{pr}/reviews")
    comments = _api(repo, f"repos/{full}/pulls/{pr}/comments")
    issue_comments = _api(repo, f"repos/{full}/issues/{pr}/comments")

    for r in reviews:
        if r["user"]["login"] == agent:
            continue
        body = (r.get("body") or "").strip()
        say(f"REVIEW {r['state']} by {r['user']['login']} at {r['submitted_at']}" + (f": {body}" if body else ""))

    answered = {c["in_reply_to_id"] for c in comments if c["user"]["login"] == agent and c.get("in_reply_to_id")}
    threads = [c for c in comments if not c.get("in_reply_to_id") and c["user"]["login"] != agent]
    replies_by_thread = {}
    for c in comments:
        if c.get("in_reply_to_id") and c["user"]["login"] != agent:
            replies_by_thread.setdefault(c["in_reply_to_id"], []).append(c)

    shown = 0
    for c in threads:
        followups = replies_by_thread.get(c["id"], [])
        is_open = c["id"] not in answered or followups
        if args.open and not is_open:
            continue
        shown += 1
        line = c.get("line") or c.get("original_line")
        mark = "OPEN" if is_open else "answered"
        say(f"\n[{mark}] id={c['id']} {c['path']}:{line} ({c['user']['login']}, {c['created_at'][:16]})")
        say(f"  {c['body'].strip()}")
        for f in followups:
            say(f"  ↳ id={f['id']} {f['user']['login']}: {f['body'].strip()}")
    for c in issue_comments:
        if c["user"]["login"] != agent:
            say(f"\n[PR comment] id={c['id']} {c['user']['login']}: {c['body'].strip()}")
    say(f"\n{shown} inline thread(s) shown" + (" (open only)" if args.open else ""))


def cmd_reply(args):
    """Post replies from a JSON file: {"<comment id>": "<markdown reply>", ...}."""
    replies = json.loads(Path(args.file).read_text(encoding="utf-8"))
    full = REPOS[args.repo]
    for comment_id, body in replies.items():
        reply_id = ody(args.repo, "gh", "api", "-X", "POST",
                       f"repos/{full}/pulls/{args.pr}/comments/{comment_id}/replies",
                       "-f", f"body={body}", "--jq", ".id")
        say(f"replied to {comment_id} -> {reply_id}")


def cmd_pr_body(args):
    """Replace the PR description with a file's contents (footer added if missing)."""
    body = Path(args.file).read_text(encoding="utf-8").rstrip()
    footer = config()["prFooter"]
    if footer not in body:
        body += "\n\n" + footer
    tmp = Path(args.file).with_suffix(".final.md")
    tmp.write_text(body + "\n", encoding="utf-8")
    ody(args.repo, "gh", "pr", "edit", str(args.pr), "--repo", REPOS[args.repo], "--body-file", str(tmp))
    tmp.unlink()
    say(f"PR #{args.pr} description updated")
