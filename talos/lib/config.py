"""Talos paths, settings and logging."""

import glob
import json
import os
import shutil
import sys
import threading
from datetime import datetime
from pathlib import Path

TALOS_DIR = Path(__file__).resolve().parent.parent
DEV_TOOLS = TALOS_DIR.parent
WORKSPACE = DEV_TOOLS.parent
# This machine's settings and state live in the dev-tools checkout, also when the code runs from the runtime copy
# (<workspace>/.dev-tools-runtime, see `p4m runtime`), so their paths don't depend on which copy is running.
LOCAL_DIR = WORKSPACE / "dev-tools"
AGENTS_DIR = TALOS_DIR / "agents"
STATE_DIR = Path(os.environ["TALOS_STATE_DIR"]) if os.environ.get("TALOS_STATE_DIR") else LOCAL_DIR / "talos" / "state"   # the env override keeps tests off the live state
LOG_DIR = STATE_DIR / "logs"
CONTAINER_DIR = STATE_DIR / "containers"   # per-issue ~/.claude of container runs (container.py)
CONFIG_FILE = TALOS_DIR / "config.json"
OUTCOME_SCHEMA = TALOS_DIR / "schema" / "outcome.json"
LISTENER = TALOS_DIR / "listener" / "listen.js"
GAIA_MCP = DEV_TOOLS / "gaia-mcp"


class TalosError(Exception):
    """A step failed; the message says which and why."""


def _env_value(path, key):
    """One KEY from an env file (empty when the file or key is missing)."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        name, sep, value = line.strip().partition("=")
        if sep and name.strip() == key:
            return value.strip().strip('"').strip("'")
    return ""


def settings(local_dir=None):
    """config.json, overlaid by the engineer's config.local.json (git-ignored, written by `p4m configure`).

    One Gaia URL: when config.local.json has no gaiaUrl, Talos uses GAIA_URL from gaia-mcp.env (the one the agents
    use) before the config.json default."""
    local_dir = Path(local_dir) if local_dir else LOCAL_DIR
    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    local_cfg = {}
    local = local_dir / "talos" / "config.local.json"
    if local.exists():
        with open(local, encoding="utf-8") as f:
            local_cfg = json.load(f)
    cfg.update(local_cfg)
    if not local_cfg.get("gaiaUrl"):
        cfg["gaiaUrl"] = _env_value(local_dir / "gaia-mcp" / "gaia-mcp.env", "GAIA_URL") or cfg.get("gaiaUrl")
    return cfg


def claude_bin(configured=None):
    """The Claude Code CLI: config value, then PATH, then the newest VS Code extension binary."""
    if configured:
        if not Path(configured).exists():
            raise TalosError(f"claudeBin '{configured}' does not exist")
        return configured
    found = shutil.which("claude")
    if found:
        return found
    pattern = os.path.expanduser("~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude*")
    candidates = [p for p in glob.glob(pattern) if not p.endswith(".md")]

    def version(path):
        name = Path(path).parents[2].name  # anthropic.claude-code-2.1.300-win32-x64
        parts = name.split("-")[2] if name.count("-") >= 2 else "0"
        return tuple(int(x) for x in parts.split(".") if x.isdigit())

    if not candidates:
        raise TalosError("Claude Code CLI not found: set claudeBin in dev-tools/talos/config.json")
    return max(candidates, key=version)


_log_lock = threading.Lock()


def log(msg, agent=None):
    """One timestamped line to stdout and state/talos.log."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now().astimezone().isoformat(timespec='seconds')} [{agent or 'talos'}] {msg}"
    with _log_lock:
        print(line, flush=True)
        with open(STATE_DIR / "talos.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")


def now_stamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
