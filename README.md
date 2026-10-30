# dev-tools: the Projects4Me development tooling

Tools for running the Projects4Me stack locally and for working on it with Claude Code. This repo is meant to be
used inside the **[Pr4M](https://github.com/projects4me/Pr4M)** workspace, which holds it as a submodule next to
`gaia`, `prometheus`, `hermes`, `ares` and `docker-images`, plus the shared Claude Code skills and agents in
`.claude/`.

| Folder | What it is |
|---|---|
| [`installer/`](installer/P4M-CLI.md) | **p4m**: installs prerequisites, clones the repos, runs the whole stack over HTTPS with a local CA (`p4m setup`, `up`, `down`, `db`, `test`, `trust`, …), and `p4m configure` for your personal settings |
| [`gaia-mcp/`](gaia-mcp/README.md) | MCP server that lets Claude Code read and write Projects4Me issues (local or live) |
| [`odysseus/`](odysseus/README.md) | **Agent Odysseus**, the team's shared agent identity: git and `gh` as the agent, never as you |
| [`ai/`](ai/README.md) | Workflow CLI for Claude Code: start an issue, log real elapsed time, run suites, commit, open PRs, answer reviews |
| [`talos/`](talos/README.md) | Runs the agents with headless Claude Code: Agent Odysseus works issues assigned to it (Hermes); Agent Nestor brainstorms ideas (sim) |
| [`nestor/`](nestor/README.md) | The UI for brainstorming ideas with **Agent Nestor** (a Talos agent): refine an idea into an implementation plan, approve it, and Talos creates the issues in Gaia |
| [`sim/`](sim/README.md) | Temporary stand-ins for Gaia's idea resources and Hermes' idea events, until Gaia and Hermes support ideas |

## Setup

Supported on Windows 10/11 (Git Bash + Docker Desktop), macOS and Ubuntu/Debian.

1. **Clone the workspace** (all repos at once):

   ```bash
   git clone --recurse-submodules https://github.com/projects4me/Pr4M.git
   cd Pr4M
   ```

2. **Install and start the stack:** `dev-tools/installer/p4m setup` (Windows: `dev-tools\installer\p4m.cmd setup`).
   It checks or installs git and Docker, builds the images, migrates and seeds the database, trusts the local CA
   and starts everything at `https://app.local-projects4.me`. See [installer/P4M-CLI.md](installer/P4M-CLI.md).

3. **Configure your tools:** `dev-tools/installer/p4m configure`. It asks for:
   - **you:** your name, your Projects4Me login email (used to find your user id on that environment; local and live
     ids differ, and it asks for the id if the lookup fails), and a timezone label for time
     logs;
   - **service URLs:** Gaia API, Prometheus app, Hermes and Ares. The defaults are the local stack; give the live
     URLs to work against live;
   - **Agent Odysseus**, shared by the team lead over a secure channel: its Projects4Me password, its GitHub token
     and its SSH private key.

   - **Agent Agamemnon (optional):** answer `yes` to switch on the orchestrator that feeds issues to Odysseus, then
     give its email and password. It writes `gaia-mcp/agamemnon.env`, checks the sign-in and reads its Gaia user id,
     writes `talos/agents/agamemnon/agent.local.json` (`enabled: true`, owned by you) and adds its user id to
     `acceptAssignmentsFrom` in Odysseus' `agent.local.json`, keeping the ids already there. Skipping leaves
     Agamemnon disabled; re-running is safe.

   The OAuth client id and secret are read from `prometheus/config/environment.js` (the same in every
   environment), so nobody types them.

   It then writes the git-ignored credential and settings files, installs the agent's SSH key and `github-odysseus`
   host alias, adds the `odysseus` remote to every repo, registers the gaia MCP server with Claude Code, installs
   Talos' listener dependency, and checks GitHub and SSH access. Re-run it any time; your previous answers are the
   defaults.

4. **Restart Claude Code** in the workspace folder and ask it to run `gaia_whoami`.

5. **Optional: run Talos** with `dev-tools/talos/talos run`. Your copy acts only on issues **you** assign to Agent
   Odysseus, and hands questions and finished work back to you. It runs Claude Code with **your own Claude
   subscription**. See [talos/README.md](talos/README.md).

### Prerequisites

- **Docker** (Docker Desktop on Windows/macOS, Docker Engine on Linux) and **git**: `p4m install` helps on Windows.
- **Python 3.10+**: everything here uses only the standard library.
- **Claude Code** (`claude` on PATH, or the VS Code extension).
- **Node.js 22.15+** and npm for Talos (`--use-system-ca`). Ubuntu's own `nodejs` package is older; use NodeSource or
  nvm.
- **GitHub CLI** (`gh`) for PRs as the agent. Ubuntu: `sudo apt install gh`.
- **bash** for the `odysseus` and `ai` wrappers (Git Bash on Windows).
- Ubuntu: `libnss3-tools` so `p4m trust` can add the local CA to Chrome's store too.

## What stays on your machine

Everything personal or secret is git-ignored: `p4m.env`, `.p4m/`, `logs/`, `workspace.local.json`, `*.local.json`
next to each tool's config, `gaia-mcp/*.env`, `odysseus/odysseus.env`, and the `state/` folders of `ai` and `talos`.

## Licence

AGPLv3, like the other Projects4Me repositories: see [LICENSE](LICENSE).
