"""A streamed `claude -p` run for an ideas agent: the command, its stream-json events, the running process.

runner.py runs issue agents to a structured outcome; an ideas agent talks to a person who watches, so its run is
streamed and turned into a few progress events:
  {"type": "text", "text": ...}      a piece of the reply, as it is written
  {"type": "turn"}                   the agent starts another message (the text so far was narration)
  {"type": "step", "text": ...}      a tool call, in a few words (steps.py)
  {"type": "result", ...}            the run finished: final text, session id, cost, error flag

The agent's tool policy comes from its agent.json: `builtinTools` (--tools: what is loaded at all; each loaded
tool's schema is in every request), `allowedTools` with permission mode dontAsk (everything else refused), and
`deniedTools` (deny beats any allow rule from a settings file, and denied tools are left out of the context).
"""

import json
import os
import signal
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from ..config import LOG_DIR, WORKSPACE
from .steps import describe


def build_command(claude, agent, model, system_prompt, mcp=None, session_id=None, new_session_id=None):
    """The argv; the prompt goes on stdin (no command-line length limit, never parsed as a flag)."""
    cmd = [claude, "-p", "--model", model,
           "--output-format", "stream-json", "--verbose", "--include-partial-messages",
           "--append-system-prompt", system_prompt, "--permission-mode", agent.permission_mode]
    if agent.builtin_tools:
        cmd += ["--tools", agent.builtin_tools]
    if agent.allowed_tools:
        cmd += ["--allowedTools", *agent.allowed_tools]
    if agent.denied_tools:
        cmd += ["--disallowedTools", *agent.denied_tools]
    if mcp:
        cmd += ["--strict-mcp-config", "--mcp-config", str(mcp)]
    if agent.max_turns:
        cmd += ["--max-turns", str(agent.max_turns)]
    if session_id:
        cmd += ["--resume", session_id]
    elif new_session_id:
        cmd += ["--session-id", new_session_id]
    return cmd


def events(raw):
    """The progress events for one stream-json event (a dict). Subagent traffic is ignored."""
    if raw.get("parent_tool_use_id"):
        return []
    kind = raw.get("type")
    if kind == "stream_event":
        event = raw.get("event") or {}
        if event.get("type") == "message_start":
            return [{"type": "turn"}]
        delta = event.get("delta") or {}
        if event.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
            return [{"type": "text", "text": delta.get("text", "")}]
        return []
    if kind == "assistant":
        return [{"type": "step", "text": describe(block.get("name", ""), block.get("input"))}
                for block in (raw.get("message") or {}).get("content") or [] if block.get("type") == "tool_use"]
    if kind == "result":
        # total_cost_usd is the whole session's cost so far when the session was resumed
        return [{"type": "result", "text": raw.get("result") or "", "session_id": raw.get("session_id"),
                 "cost": raw.get("total_cost_usd"), "turns": raw.get("num_turns"),
                 "is_error": bool(raw.get("is_error")) or raw.get("subtype") != "success",
                 "subtype": raw.get("subtype")}]
    return []


class Run:
    """One running `claude -p`. Iterate it for events; `stop()` ends it (and its children) from another thread."""

    def __init__(self, cmd, prompt, env, label):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.log_path = LOG_DIR / f"{label}-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
        # its own process group on Linux/macOS, so stop() can end the MCP server and Bash children with it
        self.proc = subprocess.Popen(cmd, cwd=WORKSPACE, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                     start_new_session=os.name != "nt")
        self.stopped = False
        self._stderr = []
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        try:
            self.proc.stdin.write(prompt)
            self.proc.stdin.close()
        except OSError:
            pass  # it already exited; the missing result is reported below

    def _drain_stderr(self):
        for line in self.proc.stderr:
            self._stderr.append(line)

    def stop(self):
        self.stopped = True
        self._kill()

    def _kill(self):
        if self.proc.poll() is not None:
            return
        # the whole tree: kill() alone would leave the MCP server and Bash children running
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)], capture_output=True)
        else:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def __iter__(self):
        got_result = False
        try:
            with open(self.log_path, "w", encoding="utf-8") as log:
                for line in self.proc.stdout:
                    log.write(line)
                    try:
                        raw = json.loads(line)
                    except ValueError:
                        continue
                    for event in events(raw):
                        got_result = got_result or event["type"] == "result"
                        yield event
            self.proc.wait()
        finally:
            self._kill()  # the consumer stopped early or raised: don't leave it running
        if not got_result:
            why = "stopped" if self.stopped else ("".join(self._stderr)[-1200:] or f"exit {self.proc.returncode}")
            yield {"type": "result", "text": "", "session_id": None, "cost": None, "turns": None, "is_error": True,
                   "subtype": "stopped" if self.stopped else "crashed", "error": why}


class Launcher:
    """Starts runs for one agent with fixed settings (CLI, MCP config, environment)."""

    def __init__(self, claude, agent, env, mcp=None):
        self.claude, self.agent, self.env, self.mcp = claude, agent, env, mcp

    def start(self, prompt, model, system_prompt, session_id=None, new_session_id=None, label="run"):
        cmd = build_command(self.claude, self.agent, model, system_prompt, mcp=self.mcp, session_id=session_id,
                            new_session_id=new_session_id)
        return Run(cmd, prompt, self.env, label)
