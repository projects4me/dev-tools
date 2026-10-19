"""PR review: list what the reviewer said, and post replies in one go."""

import json
import re
from pathlib import Path

from .common import REPOS, config, ody, say

# Bot <details> blocks that never carry a finding of their own: tool traces, a restatement of the
# finding for agents, run metadata and the walkthrough. Nitpick / outside-diff / duplicate blocks DO
# carry findings and are kept.
FOLD_SUMMARIES = re.compile(r"Analysis chain|Prompt for AI Agents|Prompt to fix review comments|Recent review info|"
                            r"Run configuration|Commits|Files selected for processing|Files skipped from review|"
                            r"Walkthrough|Sequence Diagram|Review details|Additional context used|Learnings", re.I)
_DETAILS = re.compile(r"<details>|</details>", re.I)


def fold(body):
    """A bot comment without machine markers and boilerplate blocks; each folded block leaves a one-line
    marker, and `ai review --raw` shows the original."""
    text = re.sub(r"<!--.*?-->", "", body or "", flags=re.S)
    out, i = [], 0
    while True:
        m = re.search(r"<details>\s*<summary>(.*?)</summary>", text[i:], flags=re.S | re.I)
        if not m:
            out.append(text[i:])
            break
        start = i + m.start()
        depth, j = 0, start
        for t in _DETAILS.finditer(text, start):          # find the matching </details>
            depth += 1 if t.group(0).lower() == "<details>" else -1
            if depth == 0:
                j = t.end()
                break
        else:
            out.append(text[i:])
            break
        summary = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        out.append(text[i:start])
        if FOLD_SUMMARIES.search(summary):
            out.append(f"[folded: {summary} ({j - start} chars)]")
            i = j
        else:                                             # keep the block, fold inside it
            inner = text[start + m.end() - m.start():j - len("</details>")]
            out.append(f"<details><summary>{m.group(1)}</summary>{fold(inner)}</details>")
            i = j
    text = "".join(out)
    text = re.sub(r"^- \[ \] .*?Fix CodeRabbit comments.*$", "", text, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _is_bot(user):
    return user.get("type") == "Bot" or user.get("login", "").endswith("[bot]")


def resolved_threads(repo, pr):
    """Ids of the first comment of every review thread GitHub marks resolved."""
    owner, name = REPOS[repo].split("/")
    query = ("query($o:String!,$n:String!,$p:Int!){repository(owner:$o,name:$n){pullRequest(number:$p){"
             "reviewThreads(first:100){nodes{isResolved comments(first:1){nodes{databaseId}}}}}}}")
    out = ody(repo, "gh", "api", "graphql", "-f", f"query={query}", "-f", f"o={owner}", "-f", f"n={name}",
              "-F", f"p={pr}", check=False)
    try:
        nodes = json.loads(out)["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    except (ValueError, KeyError, TypeError):
        return set()
    return {n["comments"]["nodes"][0]["databaseId"] for n in nodes if n["isResolved"] and n["comments"]["nodes"]}


def _show(c, raw):
    return c["body"].strip() if raw or not _is_bot(c["user"]) else fold(c["body"])


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
        body = _show({"body": r.get("body") or "", "user": r["user"]}, args.raw)
        say(f"REVIEW {r['state']} by {r['user']['login']} at {r['submitted_at']}" + (f": {body}" if body else ""))

    answered = {c["in_reply_to_id"] for c in comments if c["user"]["login"] == agent and c.get("in_reply_to_id")}
    threads = [c for c in comments if not c.get("in_reply_to_id") and c["user"]["login"] != agent]
    replies_by_thread = {}
    for c in comments:
        if c.get("in_reply_to_id") and c["user"]["login"] != agent:
            replies_by_thread.setdefault(c["in_reply_to_id"], []).append(c)

    resolved = resolved_threads(repo, pr)
    shown = 0
    for c in threads:
        followups = replies_by_thread.get(c["id"], [])
        is_open = c["id"] not in resolved and (c["id"] not in answered or followups)
        if args.open and not is_open:
            continue
        shown += 1
        line = c.get("line") or c.get("original_line")
        mark = "OPEN" if is_open else "resolved" if c["id"] in resolved else "answered"
        say(f"\n[{mark}] id={c['id']} {c['path']}:{line} ({c['user']['login']}, {c['created_at'][:16]})")
        say(f"  {_show(c, args.raw)}")
        for f in followups:
            say(f"  ↳ id={f['id']} {f['user']['login']}: {_show(f, args.raw)}")
    for c in issue_comments:
        if c["user"]["login"] == agent:
            continue
        if not args.raw and _is_bot(c["user"]) and "summarize by coderabbit.ai" in (c["body"] or ""):
            say(f"\n[PR comment] id={c['id']} {c['user']['login']}: [folded: review summary/walkthrough "
                f"({len(c['body'])} chars), no findings; --raw to see]")
            continue
        say(f"\n[PR comment] id={c['id']} {c['user']['login']}: {_show(c, args.raw)}")
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
