"""The workspace's Claude auto-memory, shared by every Talos run.

Claude Code keeps auto-memory (MEMORY.md and one file per memory) in ~/.claude/projects/<slug>/memory, where the
slug is the project folder (the git root of the cwd) with every character that is not a letter or digit replaced
by "-". A run in <workspace>/.runs/issue-N happens to resolve to the workspace's folder today, because .runs is
inside the workspace repo; a container run, whose ~/.claude is its own and whose /work is no git checkout, does
not. So Talos names the folder outright with the `autoMemoryDirectory` setting (`claude --settings`), which wins
over the cwd for every run. A setting, unlike a link from the run's slug folder, leaves nothing for `talos gc` to
remove, and gc never touches this folder.

Parallel runs write here at the same time. Each memory is its own file, and Claude's Edit/Write tools refuse to
change a file that changed since the session read it, so a run that loses a race re-reads MEMORY.md and adds its
line again; the remaining risk (two runs writing one new memory file in the same moment) is accepted.
"""

import os
import re
from pathlib import Path


def claude_home():
    """Claude Code's config folder: CLAUDE_CONFIG_DIR, else ~/.claude."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(os.path.expanduser(configured)) if configured else Path.home() / ".claude"


def slug(project):
    """Claude Code's folder name for a project path (D:\\Work\\Pr4M -> D--Work-Pr4M)."""
    return re.sub(r"[^a-zA-Z0-9]", "-", str(project))


def folder(root):
    """The auto-memory folder of the workspace at root, the one interactive sessions there read and write."""
    return claude_home() / "projects" / slug(Path(root)) / "memory"


def settings(memory_dir):
    """The `claude --settings` part that points a run at memory_dir."""
    return {"autoMemoryDirectory": str(memory_dir)}
