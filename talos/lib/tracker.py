"""Projects4Me tracker access for one agent, reusing the gaia-mcp client (same code as the MCP server)."""

import html
import os
import ssl
import sys
import threading

from .config import GAIA_MCP, TalosError

sys.path.insert(0, str(GAIA_MCP))
import server as gaia_mcp  # noqa: E402  (dev-tools/gaia-mcp/server.py)


def read_env_file(path):
    """KEY=VALUE pairs from an env file, without touching os.environ."""
    values = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class Tracker:
    """Gaia as one agent. Thread-safe: the listener and the worker share it."""

    def __init__(self, agent):
        self.agent = agent
        self.env = {k: v for k, v in read_env_file(agent.gaia_env).items() if k.startswith("GAIA_")}
        g = gaia_mcp.Gaia()
        g.base = self.env.get("GAIA_URL", g.base).rstrip("/")
        g.email = self.env.get("GAIA_EMAIL", "")
        g.password = self.env.get("GAIA_PASSWORD", "")
        g.client_id = self.env.get("GAIA_CLIENT_ID", g.client_id)
        g.client_secret = self.env.get("GAIA_CLIENT_SECRET", "")
        if self.env.get("GAIA_CA_FILE"):
            g.ssl = ssl.create_default_context(cafile=self.env["GAIA_CA_FILE"])
        self.gaia = g
        self.tools = gaia_mcp.Tools(g)
        self._lock = threading.RLock()

    def _call(self, fn, *args, **kwargs):
        with self._lock:
            try:
                return fn(*args, **kwargs)
            except gaia_mcp.GaiaError as e:
                raise TalosError(str(e))

    # -- identity --------------------------------------------------------------------------

    def token(self):
        return self._call(self.gaia.token)

    def user_id(self):
        return str(self._call(self.gaia.me)["id"])

    def project_ids(self):
        """Project name -> id for the agent's configured projects."""
        return {name: str(self._call(self.gaia.project, name)["id"]) for name in self.agent.projects}

    # -- issues ----------------------------------------------------------------------------

    def issue(self, number):
        """The fields Talos decides on, read fresh from Gaia (events are only hints)."""
        i = self._call(self.gaia.issue_by_number, number,
                       rels="issuestatus,issuemilestone,project")
        return {
            "number": int(i.get("issueNumber")),
            "id": i.get("id"),
            "status": (i.get("issuestatus") or {}).get("name") or i.get("status"),
            "assignee": str(i.get("assignee") or ""),
            "modifiedUser": str(i.get("modifiedUser") or ""),
            "project": (i.get("project") or {}).get("name"),
            "milestone": (i.get("issuemilestone") or {}).get("name"),
            "complexity": gaia_mcp.read_complexity(i.get("description")),
        }

    def assigned_issue_numbers(self, project_id, user_id):
        rows = self._call(self.gaia.find, "issue",
                          f"((Issue.projectId : {project_id}) AND (Issue.assignee : {user_id}))",
                          limit=-1)
        return sorted(int(r["issueNumber"]) for r in rows if r.get("issueNumber"))

    def comment(self, number, title, body_html):
        text = f"<p><strong>{html.escape(title)}</strong></p>{body_html}"
        return self._call(self.tools.add_comment, {"issue_number": number, "comment": text})

    def set_status(self, number, status):
        return self._call(self.tools.update_issue, {"issue_number": number, "status": status})

    def hand_off(self, number, status, assignee):
        return self._call(self.tools.update_issue,
                          {"issue_number": number, "status": status, "assignee": assignee})

    # -- for child processes ----------------------------------------------------------------

    def child_env(self):
        """Environment for `claude`, so the gaia MCP server and dev-tools/ai sign in as this agent."""
        return {**os.environ, **self.env}
