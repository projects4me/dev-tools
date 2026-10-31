# gaia-mcp — Projects4Me issues for Claude Code

An MCP server that lets Claude Code find, create and update Projects4Me issues through Gaia's REST API.
It signs in as a dedicated Claude user (OAuth2 password grant), so Gaia's ACL applies to everything it does.
Pure Python standard library, like `p4m`: nothing to install.

## Tools

| Tool | What it does |
|---|---|
| `gaia_whoami` | Which user it signs in as, and which projects that user belongs to |
| `gaia_list_projects` | Projects visible to the user, filterable by name or short code |
| `gaia_project_options` | A project's issue types, statuses, priorities, milestones and members |
| `gaia_list_issues` | A project's issues, newest first; filter by status, subject text, or assigned to me |
| `gaia_get_issue` | One issue by number, with its description, complexity, comments, logged/estimated time and attached files |
| `gaia_get_file` | Read a file attached to an issue, by name or id: an image (PNG, JPEG, GIF, WebP) comes back as an image the agent sees; any other file is saved under the system temp folder (`gaia-mcp/<issue>/`) and its path returned. Max 10 MB |
| `gaia_create_issue` | Create an issue (defaults: Task, `new`, `medium`, assigned to me, starts today, ends a week later, milestone Claude-v1); `complexity` is required. A milestone the project doesn't have yet (Claude-v1 or the one named) is created; `""` means the backlog |
| `gaia_create_milestone` | Create a milestone in a project, or return the existing one with that name (defaults: in progress, from today for 30 days) |
| `gaia_add_comment` | Add an HTML comment to the issue's thread (creates the thread on first comment, like Prometheus) |
| `gaia_attach_file` | Attach a local screenshot, PDF or text file to an issue (shows in its Files box; max 10 MB) |
| `gaia_log_time` | Log spent time (default) or an estimate, with a description; a day is 8 hours |
| `gaia_update_issue` | Change status, priority, type, subject, description, complexity, assignee, milestone, dates or parent |

Projects are given by name or short code: Gaia work goes in **A Gaia** (`agaia`), Prometheus work in
**A Prometheus** (`aprom`). Types, statuses, milestones and members are given by name. Descriptions are HTML,
because that's what Prometheus renders.

**Complexity** (`simple` or `complex`) is the recommended model for the agent that works the issue:
Talos runs `complex` issues on Opus and `simple` ones on Sonnet. Gaia has no field for it, so it is stored as a
`Complexity: <value>` line at the end of the description (type the same line in Prometheus to set it by hand).
Updating the description keeps the existing marker unless the new text has one.

## Setup

`dev-tools/installer/p4m configure` does all of this. By hand:

1. Copy `gaia-mcp.env.example` to `gaia-mcp.env` (git-ignored) and fill in `GAIA_EMAIL`, `GAIA_PASSWORD`
   and `GAIA_CLIENT_SECRET` (the value in `prometheus/config/environment.js`). In the Pr4M workspace these are
   the shared **Agent Odysseus** credentials.
2. Register it with Claude Code once, at user scope so it works in the workspace and in every repo:

   ```bash
   claude mcp add --scope user gaia -- python3 "<workspace>/dev-tools/gaia-mcp/server.py"
   ```

3. Restart Claude Code (or run `/mcp`) and ask it to run `gaia_whoami`.

## Local or live

`GAIA_URL` (API) and `GAIA_UI_URL` (Prometheus, for issue links) pick the environment; they default to the local
p4m stack (`https://api.local-projects4.me`). To work against **live**, point them at the live API and app, and use
the live OAuth client id/secret (the production Prometheus config) and the agent's **live** password.

To keep both, give each its own env file and register two servers; `GAIA_MCP_ENV` names the file to read
(`dev-tools/ai` honours it too):

```bash
claude mcp add --scope user gaia       -e GAIA_MCP_ENV="<workspace>/dev-tools/gaia-mcp/live.env"  -- python3 "<workspace>/dev-tools/gaia-mcp/server.py"
claude mcp add --scope user gaia-local -e GAIA_MCP_ENV="<workspace>/dev-tools/gaia-mcp/gaia-mcp.env" -- python3 "<workspace>/dev-tools/gaia-mcp/server.py"
```

Talos reads the file named by `gaiaEnv` in `talos/agents/<name>/agent.json` (default `gaia-mcp/gaia-mcp.env`), so
point that file at the environment whose Hermes Talos listens to (`hermesUrl` in `talos/config.local.json`).

The local p4m CA comes from the system store (`p4m trust`); set `GAIA_CA_FILE` if your proxy's CA isn't there.
Live uses a public certificate and needs nothing.

## Notes

- The Claude user must be a member of a project to create issues in it (Gaia's project-scoped ACL).
- Search text is limited to letters, digits, spaces and `.,:#/-`, because Gaia's query parser inserts
  values into PHQL without binding them.
- The password and tokens never leave this process except in the request to Gaia's `/api/v1/token`.
