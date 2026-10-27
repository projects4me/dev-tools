"""`ai cost`: token and turn accounting per session and per issue, from Claude Code transcripts.

Reads ~/.claude/projects/<workspace>*/*.jsonl (assistant message.usage, tool_use inputs, tool_result sizes).
A session is one transcript file; its issue is the claude-issue-N branch it ran on, else the first
`ai start|ship|time|... <N>` / "issue #N" it mentions. Its repo is the Pr4M/<repo> path it touches most.
"""

import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .common import REPOS, STATE_DIR, WORKSPACE, say

TOKEN_FIELDS = ("output", "input", "cache_creation", "cache_read")
BRANCH_RE = re.compile(r"claude-issue-(\d+)")
ISSUE_RES = (re.compile(r"\bai(?:\.py)? (?:start|time|ship|context|issue|status|comment|pr|commit|handoff|test)\b[^\n]*?\b(\d{4,})\b"),
             re.compile(r"\b[Ii]ssue #?(\d{4,})\b"))
FILE_TOOLS = {"Read", "Edit", "Write", "NotebookEdit"}
BASH_CATEGORIES = (                     # first match wins
    ("tests", re.compile(r"\b(ai test|p4m test|phpunit|npm (run )?test|ember test|pytest|unittest|api-tester)\b")),
    ("ai-tool", re.compile(r"(^|[\s/])ai(\.py)? |dev-tools/ai/|odysseus")),
    ("docker", re.compile(r"\bdocker\b|\bpsql\b")),
    ("git", re.compile(r"(^|[\s;&|])(git|gh)\s")),
    ("file-reads", re.compile(r"(^|[\s;&|])(cat|head|tail|sed|ls|find|grep|rg|wc)\s")),
)


def transcript_dirs(root=None):
    """~/.claude/projects directories of the workspace and of its subfolders (dev-tools, ...)."""
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(root or WORKSPACE.resolve())).lower()
    base = Path.home() / ".claude" / "projects"
    return sorted(d for d in base.glob("*") if d.is_dir() and d.name.lower().startswith(slug))


def bash_category(command):
    for name, rx in BASH_CATEGORIES:
        if rx.search(command):
            return name
    return "other"


def result_size(content):
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(b.get("text", "")) for b in content if isinstance(b, dict))
    return 0


def tool_text(inp):
    return " ".join(str(v) for v in inp.values() if isinstance(v, (str, int)))


def parse_session(path):
    """One transcript -> a metrics dict (None when it has no assistant turns)."""
    usage = {}                  # message id -> usage (a message is logged once per content block)
    tool_by_id, chars = {}, Counter()
    files, branches, repos, mentions = set(), Counter(), Counter(), Counter()
    first = last = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            ts = d.get("timestamp")
            if ts:
                first = first or ts
                last = ts
            if d.get("gitBranch"):
                branches[d["gitBranch"]] += 1
            msg = d.get("message") if isinstance(d.get("message"), dict) else {}
            content = msg.get("content")
            if d.get("type") == "assistant" and msg.get("usage"):
                usage[msg.get("id") or d.get("uuid")] = msg["usage"]
            if not isinstance(content, list):
                if isinstance(content, str) and d.get("type") == "user":
                    _mentions(content, mentions)
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and d.get("type") == "user":
                    _mentions(b.get("text", ""), mentions)
                elif b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    name = b.get("name", "?")
                    if name == "Bash":
                        cmd = inp.get("command", "")
                        tool_by_id[b.get("id")] = ("Bash:" + bash_category(cmd), name)
                        _mentions(cmd, mentions)
                    else:
                        tool_by_id[b.get("id")] = (name, name)
                    text = tool_text(inp)
                    for m in re.finditer(r"Pr4M[\\/]+(" + "|".join(REPOS) + r")\b", text, re.I):
                        repos[m.group(1).lower()] += 1
                    if name in FILE_TOOLS and inp.get("file_path"):
                        files.add(str(inp["file_path"]).replace("\\", "/"))
                elif b.get("type") == "tool_result":
                    label = tool_by_id.get(b.get("tool_use_id"), ("?", "?"))[0]
                    chars[label] += result_size(b.get("content"))
    if not usage:
        return None
    totals, peak = Counter(), 0
    for u in usage.values():
        totals["output"] += u.get("output_tokens", 0)
        totals["input"] += u.get("input_tokens", 0)
        totals["cache_creation"] += u.get("cache_creation_input_tokens", 0)
        totals["cache_read"] += u.get("cache_read_input_tokens", 0)
        peak = max(peak, u.get("cache_read_input_tokens", 0) + u.get("input_tokens", 0))
    return {"session": Path(path).stem, "start": first, "end": last, "issue": _issue(branches, mentions),
            "repo": repos.most_common(1)[0][0] if repos else _repo_from_cwd(path), "turns": len(usage),
            "peak_context": peak, **{k: totals[k] for k in TOKEN_FIELDS}, "tool_chars": dict(chars),
            "files": sorted(files)}


def _mentions(text, counter):
    for rx in ISSUE_RES:
        for m in rx.finditer(text):
            counter[int(m.group(1))] += 1


def _issue(branches, mentions):
    for branch, _ in branches.most_common():
        m = BRANCH_RE.search(branch)
        if m:
            return int(m.group(1))
    return mentions.most_common(1)[0][0] if mentions else None


def _repo_from_cwd(path):
    name = Path(path).parent.name.lower()
    return "dev-tools" if name.endswith("dev-tools") else None


def load_sessions(dirs=None):
    out = []
    for d in dirs if dirs is not None else transcript_dirs():
        for f in sorted(d.glob("*.jsonl")):
            s = parse_session(f)
            if s:
                out.append(s)
    return out


def select(sessions, repo=None, issue=None, since=None):
    out = []
    for s in sessions:
        if repo and s["repo"] != repo:
            continue
        if issue and s["issue"] != issue:
            continue
        if since and (s["start"] or "")[:10] < since:
            continue
        out.append(s)
    return out


def issue_rollup(sessions):
    by = defaultdict(list)
    for s in sessions:
        by[s["issue"]].append(s)
    rows = []
    for issue, group in by.items():
        row = {"issue": issue, "sessions": len(group), "repos": sorted({s["repo"] for s in group if s["repo"]}),
               "turns": sum(s["turns"] for s in group), "peak_context": max(s["peak_context"] for s in group)}
        for k in TOKEN_FIELDS:
            row[k] = sum(s[k] for s in group)
        rows.append(row)
    return sorted(rows, key=lambda r: (r["issue"] is None, r["issue"] or 0))


def repo_rollup(sessions):
    by = defaultdict(list)
    for s in sessions:
        by[s["repo"] or "(none)"].append(s)
    rows = {}
    for repo, group in sorted(by.items()):
        row = {"sessions": len(group)}
        for k in (*TOKEN_FIELDS, "turns", "peak_context"):
            vals = [s[k] for s in group]
            row[k] = sum(vals)
            row["median_" + k] = int(statistics.median(vals))
        rows[repo] = row
    return rows


def repeated_files(sessions, minimum=2):
    seen = Counter()
    for s in sessions:
        seen.update(s["files"])
    return [(f, n) for f, n in seen.most_common() if n >= minimum]


def tool_chars_total(sessions):
    total = Counter()
    for s in sessions:
        total.update(s["tool_chars"])
    return total


def hook_bypass(log=None):
    """Counts from the guard hook's log: blocked attempts per kind and --raw uses, so the redirect list can be tuned."""
    log = log or STATE_DIR / "hooks.jsonl"
    counts = Counter()
    if log.exists():
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                counts[json.loads(line)["kind"]] += 1
            except (ValueError, KeyError):
                continue
    blocked = counts["block-read"] + counts["block-docker"]
    return {"blocked_reads": counts["block-read"], "blocked_docker": counts["block-docker"], "raw_uses": counts["raw"],
            "bypass_rate": round(counts["raw"] / (counts["raw"] + blocked), 2) if counts["raw"] + blocked else 0.0}


def regression_warning(issue, repo, sessions=None, minimum=3):
    """A warning line when the issue's output tokens exceed the repo's rolling median per issue, else None."""
    sessions = sessions if sessions is not None else load_sessions()
    rows = issue_rollup(select(sessions, repo=repo))
    mine = next((r for r in rows if r["issue"] == issue), None)
    others = [r["output"] for r in rows if r["issue"] not in (issue, None)]
    if not mine or len(others) < minimum:
        return None
    median = int(statistics.median(others))
    if mine["output"] <= median:
        return None
    return (f"Cost warning: {k(mine['output'])} output tokens is above the {repo} median of {k(median)} "
            f"per issue ({len(others)} issues).")


def build_report(sessions):
    return {"hook_bypass": hook_bypass(), "sessions": sessions, "issues": issue_rollup(sessions), "repos": repo_rollup(sessions),
            "tool_chars": dict(tool_chars_total(sessions).most_common()),
            "files_read_in_multiple_sessions": [{"file": f, "sessions": n} for f, n in repeated_files(sessions)]}


def k(n):
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def summary_line(s):
    return (f"{k(s['output'])} output tokens, {s['turns']} turns, peak context {k(s['peak_context'])}, "
            f"cache reads {k(s['cache_read'])}")


def render_text(report, top=15):
    lines = ["## Per repo (sessions; median per session)",
             "| repo | sessions | output | input | cache create | cache read | turns | peak ctx |", "|---|---|---|---|---|---|---|---|"]
    for repo, r in report["repos"].items():
        lines.append(f"| {repo} | {r['sessions']} | {k(r['median_output'])} | {k(r['median_input'])} | "
                     f"{k(r['median_cache_creation'])} | {k(r['median_cache_read'])} | {r['median_turns']} | "
                     f"{k(r['median_peak_context'])} |")
    lines += ["", "## Per issue", "| issue | sessions | repos | output | input | cache create | cache read | turns | peak ctx |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in report["issues"]:
        lines.append(f"| {r['issue'] or '-'} | {r['sessions']} | {','.join(r['repos']) or '-'} | {k(r['output'])} | "
                     f"{k(r['input'])} | {k(r['cache_creation'])} | {k(r['cache_read'])} | {r['turns']} | "
                     f"{k(r['peak_context'])} |")
    lines += ["", "## Tool result chars (all selected sessions)"]
    lines += [f"- {name}: {k(n)}" for name, n in list(report["tool_chars"].items())[:top]]
    lines += ["", f"## Files read in 2+ sessions (top {top})"]
    lines += [f"- {f['file']} ({f['sessions']})" for f in report["files_read_in_multiple_sessions"][:top]]
    return "\n".join(lines)


def cost_comment(issue, sessions=None):
    """One line for the tracker: the issue's cost so far, or None when no transcript mentions it."""
    group = select(sessions if sessions is not None else load_sessions(), issue=issue)
    if not group:
        return None
    row = issue_rollup(group)[0]
    return (f"Cost so far: {k(row['output'])} output tokens, {row['turns']} turns, "
            f"peak context {k(row['peak_context'])}, cache reads {k(row['cache_read'])} "
            f"({row['sessions']} session{'s' if row['sessions'] != 1 else ''}).")


def cmd_cost(args):
    sessions = select(load_sessions(), args.repo, args.issue, args.since)
    report = build_report(sessions)
    if args.json:
        say(json.dumps(report, indent=1))
    else:
        generated = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        say(f"{len(sessions)} sessions ({generated})\n" + render_text(report))
    return 0
