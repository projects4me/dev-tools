"""`ai rabbit digest`: CodeRabbit's findings across Agent Odysseus PRs, categorised, as one markdown report.

Parsing is pure (`parse_inline`, `parse_review_body`, `digest`, `render`) so it is tested on fixture bodies;
only `fetch_pr` and `cmd_digest` talk to GitHub, through the odysseus gh.

Two CodeRabbit formats are read: the old `_⚠️ Potential issue_ | _🟠 Major_` header and the new
`**Category** | **Severity**` one. Findings come from inline comments and from review bodies (the
"Outside diff range" and nitpick/Trivial `<details>` sections). Replies to the agent and the
walkthrough/summary comments are skipped.
"""

import json
import re
from collections import Counter

from .common import REPOS, AiError, config, ody, say
from .coderabbit import enabled_repos
from .review import _api, _is_bot, open_threads, resolved_threads

HEADER = re.compile(r"^\s*[_*]{1,2}([^_*|\n]+?)[_*]{1,2}\s*\|\s*[_*]{1,2}([^_*|\n]+?)[_*]{1,2}\s*$", re.M)
TITLE = re.compile(r"^\s*\*\*(.+?)\*\*", re.M)
SECTION = re.compile(r"outside diff range|nitpick|trivial", re.I)
SUMMARY = re.compile(r"<summary>(.*?)</summary>", re.S | re.I)
ENTRY = re.compile(r"^\s*`(?P<lines>[\d\-, ]+)`\s*:\s*\*\*(?P<title>.+?)\*\*", re.M)
BUCKETS = ((5, "1-5 files"), (10, "6-10 files"), (15, "11-15 files"))
OVER = "16+ files"


def clean(label):
    """A header label without emoji and markup: '🟠 Major' -> 'Major'."""
    return re.sub(r"^[^A-Za-z]+", "", label or "").strip()


def _head(text):
    """(category, severity) from the first header line of a comment, else (None, None)."""
    m = HEADER.search(text or "")
    return (clean(m.group(1)), clean(m.group(2))) if m else (None, None)


def _title(text):
    """The first bold line that isn't the category | severity header."""
    for line in (text or "").splitlines():
        m = TITLE.match(line)
        if m and not HEADER.match(line):
            return m.group(1).strip()
    return ""


def parse_inline(comment, addressed):
    """One finding from a CodeRabbit inline comment; `addressed` says whether the thread was answered/resolved."""
    category, severity = _head(comment["body"])
    return {"file": comment.get("path") or "", "severity": severity or "Unknown", "category": category or "Unknown",
            "title": _title(comment["body"]) or clean(re.sub(r"<[^>]+>", "", comment["body"]).strip().split("\n")[0]),
            "addressed": addressed, "source": "inline"}


def _blocks(text):
    """Top-level `<details>` blocks as (summary text, inner)."""
    out, depth, start = [], 0, None
    for t in re.finditer(r"<details>|</details>", text, re.I):
        if t.group(0).lower() == "<details>":
            if depth == 0:
                start = t.end()
            depth += 1
        else:
            depth -= 1
            if depth == 0 and start is not None:
                inner = text[start:t.start()]
                m = SUMMARY.search(inner)
                out.append((re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else "",
                            inner[m.end():] if m else inner))
    return out


def parse_review_body(body):
    """Findings listed in a review body's outside-diff and nitpick/trivial sections (addressed is unknown)."""
    findings = []
    for summary, inner in _blocks(re.sub(r"<!--.*?-->", "", body or "", flags=re.S)):
        if not SECTION.search(summary):
            continue
        section = "Outside diff range" if re.search("outside", summary, re.I) else "Nitpick"
        for path, content in _blocks(inner):
            file = re.sub(r"\s*\(\d+\)\s*$", "", path).strip("` ")
            for m, nxt in _entries(content):
                category, severity = _head(nxt)
                findings.append({"file": file, "severity": severity or ("Trivial" if section == "Nitpick" else "Unknown"),
                                 "category": category or section, "title": m.group("title").strip(),
                                 "addressed": None, "source": section.lower()})
    return findings


def _entries(content):
    matches = list(ENTRY.finditer(content))
    for i, m in enumerate(matches):
        yield m, content[m.end():matches[i + 1].start() if i + 1 < len(matches) else len(content)]


def is_coderabbit(user):
    return _is_bot(user) and "coderabbit" in user.get("login", "").lower()


def findings_for_pr(reviews, comments, resolved, agent):
    """All findings of one PR: inline threads CodeRabbit started plus review-body sections."""
    open_ids = {c["id"] for c in open_threads(comments, resolved, agent)[0]}
    found = [parse_inline(c, c["id"] not in open_ids) for c in comments
             if is_coderabbit(c["user"]) and not c.get("in_reply_to_id")]
    for r in reviews:
        if is_coderabbit(r["user"]):
            found += parse_review_body(r.get("body"))
    return found


def bucket(files):
    return next((name for limit, name in BUCKETS if files <= limit), OVER)


def digest(prs):
    """prs: [{repo, number, files, lines, findings}] -> totals by severity/category and per size bucket."""
    sev = Counter(f["severity"] for p in prs for f in p["findings"])
    cat = Counter(f["category"] for p in prs for f in p["findings"])
    buckets = {}
    for p in prs:
        b = buckets.setdefault(bucket(p["files"]), {"prs": 0, "findings": 0, "lines": 0})
        b["prs"] += 1
        b["findings"] += len(p["findings"])
        b["lines"] += p["lines"]
    return {"severity": sev, "category": cat, "buckets": buckets, "prs": prs}


def _table(rows, head):
    return "\n".join(["| " + " | ".join(head) + " |", "|" + "---|" * len(head)] + ["| " + " | ".join(map(str, r)) + " |" for r in rows])


def render(result):
    prs = result["prs"]
    total = sum(len(p["findings"]) for p in prs)
    out = [f"# CodeRabbit findings digest", "", f"{total} finding(s) across {len(prs)} PR(s).", "",
           "## By severity", _table(result["severity"].most_common(), ("Severity", "Findings")), "",
           "## By category", _table(result["category"].most_common(), ("Category", "Findings")), "",
           "## By PR size"]
    rows = []
    for name in [b[1] for b in BUCKETS] + [OVER]:
        b = result["buckets"].get(name)
        if b:
            per_k = f"{b['findings'] / (b['lines'] / 1000):.1f}" if b["lines"] else "-"
            rows.append((name, b["prs"], b["findings"], f"{b['findings'] / b['prs']:.1f}", per_k))
    out += [_table(rows, ("Size", "PRs", "Findings", "Per PR", "Per 1k changed lines")), "", "## Findings"]
    for p in prs:
        out.append(f"\n### {p['repo']} #{p['number']} ({p['files']} files, {p['lines']} lines, {len(p['findings'])} findings)")
        for f in p["findings"]:
            state = {True: "addressed", False: "open", None: "unknown"}[f["addressed"]]
            out.append(f"- [{f['severity']}] {f['category']}: {f['title']} (`{f['file']}`, {state})")
    return "\n".join(out)


# --- GitHub --------------------------------------------------------------------------------------

def list_prs(repo, agent, since=None):
    out = ody(repo, "gh", "pr", "list", "--repo", REPOS[repo], "--author", agent, "--state", "all", "--limit", "500",
              "--json", "number,createdAt,additions,deletions,changedFiles")
    prs = json.loads(out or "[]")
    return [p for p in prs if not since or p["createdAt"][:10] >= since]


def fetch_pr(repo, pr, agent):
    full = REPOS[repo]
    reviews = _api(repo, f"repos/{full}/pulls/{pr['number']}/reviews")
    comments = _api(repo, f"repos/{full}/pulls/{pr['number']}/comments")
    found = findings_for_pr(reviews, comments, resolved_threads(repo, pr["number"]), agent)
    return {"repo": repo, "number": pr["number"], "files": pr["changedFiles"],
            "lines": pr["additions"] + pr["deletions"], "findings": found}


def cmd_digest(args):
    repos = [args.repo] if args.repo else enabled_repos()
    if args.repo and args.repo not in enabled_repos():
        raise AiError(f"CodeRabbit not enabled for {args.repo}")
    agent = config()["agentLogin"]
    prs = [fetch_pr(r, p, agent) for r in repos for p in list_prs(r, agent, args.since)]
    say(render(digest(prs)))
    return 0
