"""The MCP config Talos gives its agents: the gaia server from the copy Talos runs from."""

import json
import sys

from .config import GAIA_MCP


def mcp_config(path):
    """The agent's own MCP config: only the gaia server from this copy of dev-tools (--strict-mcp-config), so
    nothing else the engineer registered is loaded and a dev-tools branch being worked doesn't change the server
    under a running agent. The server inherits the agent's GAIA_* environment."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {"gaia": {"type": "stdio", "command": sys.executable,
                                                        "args": [str(GAIA_MCP / "server.py")]}}}, indent=2),
                    encoding="utf-8")
    return path
