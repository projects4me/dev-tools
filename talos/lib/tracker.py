"""Projects4Me tracker access for one agent, reusing the gaia-mcp client (same code as the MCP server)."""

import html
import os
import ssl
import sys
import tempfile
import threading
import time
from pathlib import Path

from .dependency_gate import parse_dependencies
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
        self._milestones = {}            # project -> (names, read at)

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

    ISSUE_RELS = "issuestatus,issuetype,issuemilestone,project"

    def issue(self, number):
        """The fields Talos decides on, read fresh from Gaia (events are only hints)."""
        return self._fields(self._call(self.gaia.issue_by_number, number, rels=self.ISSUE_RELS))

    def issues_in(self, project, milestone_prefix):
        """The project's issues in milestones whose name starts with milestone_prefix (any case), as issue() reads
        them."""
        p, _ = self._call(self.gaia.project_options, project)
        prefix = milestone_prefix.lower()
        found = []
        for m in p.get("milestones") or []:
            if not str(m.get("name") or "").lower().startswith(prefix):
                continue
            rows = self._call(self.gaia.find, "issue",
                              f"((Issue.projectId : {p['id']}) AND (Issue.milestoneId : {m['id']}))",
                              rels=self.ISSUE_RELS, limit=-1)
            found.extend(self._fields(r) for r in rows if r.get("issueNumber"))
        return found

    @staticmethod
    def _fields(i):
        return {
            "number": int(i.get("issueNumber")),
            "id": i.get("id"),
            "status": (i.get("issuestatus") or {}).get("name") or i.get("status"),
            "assignee": str(i.get("assignee") or ""),
            "type": (i.get("issuetype") or {}).get("name") or i.get("type"),
            "modifiedUser": str(i.get("modifiedUser") or ""),
            "priority": str(i.get("priority") or "").lower(),
            "project": (i.get("project") or {}).get("name"),
            "milestone": (i.get("issuemilestone") or {}).get("name"),
            "complexity": gaia_mcp.read_complexity(i.get("description")),
            "dependsOn": parse_dependencies(i.get("description")),
        }

    def status_of(self, number):
        """The issue's status name alone (no other relations); TalosError when Gaia can't be read."""
        i = self._call(self.gaia.issue_by_number, number, rels="issuestatus")
        return (i.get("issuestatus") or {}).get("name") or i.get("status")

    def statuses_of(self, numbers):
        """{issue number: status name or None when it has none}; a TalosError when any can't be read, which is not
        the same as the dependency being unfinished."""
        return {number: self.status_of(number) for number in numbers}

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

    def assign(self, number, assignee):
        """Set only the assignee; the status stays, so the new assignee's own action for it runs."""
        return self._call(self.tools.update_issue, {"issue_number": number, "assignee": assignee})

    def hand_off(self, number, status, assignee):
        return self._call(self.tools.update_issue,
                          {"issue_number": number, "status": status, "assignee": assignee})

    def create_issue(self, project, subject, description, type_, priority, complexity, assignee, milestone):
        """Create an issue as this agent; returns gaia-mcp's summary (issueNumber, projectShortcode, url, ...)."""
        return self._call(self.tools.create_issue, {
            "project": project, "subject": subject, "description": description, "type": type_,
            "priority": priority, "complexity": complexity, "assignee": assignee, "milestone": milestone})["created"]

    MILESTONES_TTL = 600

    def milestones(self, project):
        """The project's milestone names, re-read at most every 10 minutes (a new one shows up without a restart)."""
        with self._lock:
            names, at = self._milestones.get(project, (None, 0))
            if names is None or time.monotonic() - at > self.MILESTONES_TTL:
                options = self._call(self.tools.project_options, {"project": project})
                names = [m["name"] for m in options["milestones"]]
                self._milestones[project] = (names, time.monotonic())
            return names

    def attach_png(self, number, name, png):
        """Attach PNG bytes to an issue as `name` (it shows in the issue's Files box)."""
        with tempfile.TemporaryDirectory(prefix="talos-attach-") as tmp:
            path = Path(tmp) / name
            path.write_bytes(png)
            return self._call(self.tools.attach_file, {"issue_number": number, "file_path": str(path)})

    # -- ideas -----------------------------------------------------------------------------

    def tool(self, name, args):
        """A gaia-mcp tool by its method name (e.g. "get_idea"), as this agent."""
        return self._call(getattr(self.tools, name), args)

    def download(self, upload_id):
        """An upload's bytes (e.g. an idea screen's PNG)."""
        return self._call(self.gaia.download, upload_id)

    def project_codes(self):
        """Project name -> short code for the agent's configured projects."""
        visible = {p["name"]: p["shortCode"] for p in self._call(self.tools.list_projects, {})["projects"]}
        return {name: visible[name] for name in self.agent.projects if name in visible}

    # -- for child processes ----------------------------------------------------------------

    def child_env(self):
        """Environment for `claude`, so the gaia MCP server and dev-tools/ai sign in as this agent."""
        return {**os.environ, **self.env}
