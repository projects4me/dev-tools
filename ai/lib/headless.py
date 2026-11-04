"""Run a one-shot headless Claude Code prompt (`claude -p`) and return its text answer."""

import os
import shutil
import subprocess

from .common import AiError, kill_tree


def claude_bin():
    """`claude` on PATH, else the binary of the Claude Code session running this (often not on PATH on Windows)."""
    found = shutil.which("claude") or os.environ.get("CLAUDE_CODE_EXECPATH")
    if not found:
        raise AiError("the Claude Code CLI (`claude`) is not on PATH and CLAUDE_CODE_EXECPATH is not set")
    return found


def ask(prompt, instructions, cwd, tools, max_turns, timeout, model=None):
    """The answer to `prompt` (sent on stdin: a Windows command line holds ~32k characters) with `instructions`
    appended to the system prompt and only `tools`. Raises subprocess.TimeoutExpired after `timeout` seconds,
    having stopped the whole process tree."""
    cmd = [claude_bin(), "-p", "--output-format", "text", "--append-system-prompt", instructions,
           "--tools", tools, "--max-turns", str(max_turns)]
    if model:
        cmd += ["--model", model]
    with subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8", errors="replace",
                          start_new_session=(os.name != "nt")) as proc:   # own group, so kill_tree can killpg it
        try:
            out, err = proc.communicate(prompt, timeout=timeout)
        except BaseException:
            kill_tree(proc.pid)
            proc.communicate()
            raise
    if proc.returncode != 0:
        raise AiError(f"claude -p failed ({proc.returncode}): {(err or out).strip()[-2000:]}")
    return out.strip()
