"""Runs one headless Claude Code session for an agent and returns its JSON result."""

import glob
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .config import LOG_DIR, OUTCOME_SCHEMA, WORKSPACE, now_stamp


def build_command(claude, agent, prompt, model, session_id=None, new_session_id=None, limits=None):
    """The `claude -p` argv. Kept separate so it can be checked without running anything.

    A new session gets its id up front (new_session_id) so its live transcript can be found while it runs.
    `limits` is the run's (max turns, max budget USD) from policy.run_limits; None = the agent's maxTurns only.
    """
    max_turns, max_budget = limits or (agent.max_turns, 0)
    with open(OUTCOME_SCHEMA, encoding="utf-8") as f:
        schema = json.dumps(json.load(f), separators=(",", ":"))
    cmd = [claude, "-p", prompt,
           "--model", model,
           "--output-format", "json",
           "--json-schema", schema,
           "--append-system-prompt", f"Your reviewer is {agent.reviewer_name}: they assign you work, answer "
                                     f"your questions and review and merge your PRs.\n\n{agent.role_prompt}",
           "--permission-mode", agent.permission_mode]
    if max_turns:
        cmd += ["--max-turns", str(max_turns)]
    if max_budget:
        cmd += ["--max-budget-usd", str(max_budget)]
    if agent.builtin_tools:
        cmd += ["--tools", agent.builtin_tools]
    if agent.allowed_tools:
        cmd += ["--allowedTools", *agent.allowed_tools]
    if agent.denied_tools:
        cmd += ["--disallowedTools", *agent.denied_tools]
    if session_id:
        cmd += ["--resume", session_id]
    elif new_session_id:
        cmd += ["--session-id", new_session_id]
    return cmd


def transcript(session_id):
    """The session's live transcript (~/.claude/projects/<workspace>/<id>.jsonl), or None."""
    if not session_id:
        return None
    found = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{session_id}.jsonl"))
    return Path(max(found, key=os.path.getmtime)) if found else None


def transcript_for_issue(issue, max_age_hours=24):
    """Fallback for runs started without a known session id: the newest recent transcript whose
    opening prompt names the issue."""
    cutoff = time.time() - max_age_hours * 3600
    needle = f"issue #{issue} "
    found = []
    for path in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
        if os.path.getmtime(path) < cutoff:
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            head = "".join(line for _, line in zip(range(8), f))
        if needle in head:
            found.append(path)
    return Path(max(found, key=os.path.getmtime)) if found else None


def usage_since(path, since):
    """Token stats for the messages a run added to a transcript (a resumed transcript also holds earlier
    runs, so only messages at or after `since`, an aware datetime, count). Main session only; subagents
    are in the run's cost but not here."""
    stats = {"turns": 0, "input_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
             "output_tokens": 0, "peak_context_tokens": 0}
    if not path:
        return stats
    seen = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") != "assistant" or not row.get("timestamp"):
                continue
            try:
                when = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            except ValueError:
                continue
            if when < since:
                continue
            message = row.get("message") or {}
            seen[message.get("id") or id(row)] = message.get("usage") or {}  # streamed parts share an id
    for usage in seen.values():
        read = usage.get("cache_read_input_tokens", 0) or 0
        write = usage.get("cache_creation_input_tokens", 0) or 0
        fresh = usage.get("input_tokens", 0) or 0
        stats["turns"] += 1
        stats["input_tokens"] += fresh
        stats["cache_read_tokens"] += read
        stats["cache_write_tokens"] += write
        stats["output_tokens"] += usage.get("output_tokens", 0) or 0
        stats["peak_context_tokens"] = max(stats["peak_context_tokens"], read + write + fresh)
    return stats


def recent_steps(path, count=10):
    """The last tool calls and messages in a transcript, one short line each."""
    steps = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") != "assistant":
                continue
            when = (row.get("timestamp") or "")[11:19]
            for part in (row.get("message") or {}).get("content") or []:
                if part.get("type") == "tool_use":
                    inp = part.get("input") or {}
                    what = (inp.get("command") or inp.get("file_path") or inp.get("pattern")
                            or inp.get("skill") or inp.get("description") or "")
                    steps.append(f"{when} {part.get('name')}: {' '.join(str(what).split())[:110]}")
                elif part.get("type") == "text" and part.get("text", "").strip():
                    steps.append(f"{when} says: {' '.join(part['text'].split())[:110]}")
    return steps[-count:]


def run(cmd, env, agent_name, issue):
    """Run Claude from the workspace root; keep the raw output in state/logs. Returns (result dict|None, log path)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{agent_name}-{issue}-{now_stamp()}.json"
    proc = subprocess.run(cmd, cwd=WORKSPACE, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(proc.stdout or "")
        if proc.stderr:
            f.write("\n--- stderr ---\n" + proc.stderr)
    result = None
    for line in reversed((proc.stdout or "").strip().splitlines()):
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict) and "session_id" in candidate:
            result = candidate
            break
    if result is None and proc.returncode != 0:
        result = {"is_error": True, "subtype": f"exit {proc.returncode}",
                  "result": (proc.stderr or proc.stdout or "")[-1500:]}
    return result, log_path
