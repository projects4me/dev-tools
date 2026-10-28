# Talos: Projects4Me issues → headless Claude Code agents

Talos listens to **Hermes** for issue assignments and hands each one to the right agent, running
`claude -p` headless. The tracker is the only channel: work, questions, blockers and failures all end up
on the issue, and **the assignee is the baton**: whoever an issue is assigned to has the next move.

```
Gaia ──publish──▶ Hermes ──issue.assignee.changed──▶ Talos ──claude -p──▶ agent (e.g. Odysseus)
                                                       │                     │
                                                       └── re-reads issue ◀──┴── comments / PR / hand-off
```

Agents come in two kinds (`kind` in agent.json): **issues** (the default; Odysseus), described below, and
**ideas** (Nestor), which brainstorm ideas with an engineer; see [Ideas agents](#ideas-agents-agent-nestor).

## Run

```bash
dev-tools/talos/talos check                    # Claude CLI, node + socket.io-client, Hermes, agent sign-in
dev-tools/talos/talos agents                   # the registry, with Gaia user and project ids resolved
dev-tools/talos/talos run [--dry-run]          # listen until Ctrl+C (Windows: talos.cmd)
dev-tools/talos/talos dispatch odysseus 3714 [--dry-run]   # work one issue now, as if just assigned
dev-tools/talos/talos status [--steps 20]                  # what is running now (issues, idea turns) + the latest steps
dev-tools/talos/talos runs [--issue 3714] [--limit 50]     # per-run stats (issues and idea turns): model, outcome, cost, turns, context
dev-tools/talos/talos reset odysseus 3714 [--model]        # after a manual look: fresh iteration allowance
dev-tools/talos/talos clean 3714 [--force]                 # issue finished: remove its .runs/issue-N workspace
dev-tools/talos/talos gc [--dry-run]                       # remove finished issues' workspaces (also when `talos run` starts; a closed issue is removed even with uncommitted changes)
python -m unittest discover -s dev-tools/talos/tests       # unit tests (from dev-tools/talos: -s tests)
```

`--dry-run` decides and logs everything but never starts Claude or writes to the tracker. First-time setup:
`npm install` in this folder (the listener needs `socket.io-client`); `p4m runtime setup` does it for the runtime copy.

### The runtime copy: Talos runs `main`, not the checkout being worked

Odysseus works A Dev Tool issues in `dev-tools/`, so that checkout gets edited and switches branches. Talos and
the tooling its agents call therefore run from a separate copy: `<workspace>/.dev-tools-runtime`, a detached
`git worktree` of `origin/main` that `p4m setup` / `p4m configure` create (`p4m runtime setup`).

- The launchers (`dev-tools/talos/talos`, `dev-tools/ai/ai`, `dev-tools/installer/p4m`, `nestor`, `sim`, `odysseus`, and their
  `.cmd` twins) hand over to the runtime copy when it exists, so the commands, the prompts, the skills and the
  `allowedTools` rules keep saying `dev-tools/ai/ai`. `P4M_RUNTIME=off` runs the copy you called instead (to try
  a tooling branch by hand).
- Talos gives its agents the gaia MCP server from the runtime copy (`--strict-mcp-config`,
  `state/<agent>/mcp.json`), and reads `agent.json` and the role prompts from it.
- This machine's files stay in `dev-tools/` whichever copy runs: `*/state/` (talos.db, logs, kb.db, timers, sim),
  `*.local.json` (including `talos/agents/<agent>/agent.local.json`), `gaia-mcp/*.env`, `odysseus/odysseus.env`,
  `p4m.env` and `.p4m/`.
- A merged dev-tools PR takes effect only after `p4m runtime update`: it fast-forwards the copy to `origin/main`
  (and runs `npm ci` when the lock file changed), refusing while `talos.db` lists an active run (`--force` for a
  stale one) or the copy has local changes. Then restart `talos run`. `p4m runtime status` shows where it is.

### Per-issue workspaces: each run has its own worktree

Every issue run works in `<workspace>/.runs/issue-N/` (`lib/workspaces.py`), so two runs on issues in the same
repo never switch branches under each other:

- `<repo>/` is a `git worktree` of the main checkout on `claude-issue-N` (an existing branch is reused, a new one
  starts at `origin/<base>`). git refuses a branch checked out in two worktrees, which keeps two runs off one branch.
- `dev-tools` and `.dev-tools-runtime` link to the copy Talos runs from (a junction on Windows, a symlink on Linux;
  for a dev-tools issue `dev-tools/` is the worktree and the `.dev-tools-runtime` link sends `dev-tools/ai/ai` to
  the tooling), `.claude` links to the workspace's, and `CLAUDE.md` is copied. So the prompts, skills and
  `allowedTools` keep saying `dev-tools/ai/ai`.
- Claude runs with that folder as cwd and `P4M_WORKSPACE` set to it; `ai` then resolves the repos there
  (`ai/lib/common.py` `repo_dir`) and `ai start` branches from `origin/<base>` without checking out the base branch.
  Per-machine files stay under `dev-tools/` (`LOCAL_DIR`). Without `P4M_WORKSPACE` (interactive use) nothing changes.
- The workspace is sticky: uncommitted work survives a `needs_input` pause. `talos clean <issue>` removes it when
  the issue is finished (links, worktrees and folder; the branch stays), refusing uncommitted changes unless
  `--force`. Issues in the workspace repo itself (`pr4m`) run in the workspace root as before.
- Every run shares the workspace's Claude auto-memory (`~/.claude/projects/<workspace slug>/memory`, `lib/memory.py`):
  Talos passes `--settings {"autoMemoryDirectory": ...}`, so a run loads the team's `MEMORY.md` and its new memories
  land there, whatever its cwd. Nothing is linked, so `talos clean`/`gc` never reach the memory. Parallel runs write
  to the same folder; each memory is its own file and Claude's Edit/Write refuse a file changed since it was read,
  so a run that loses a race on `MEMORY.md` re-reads it. Two runs writing the same new memory file at once is an
  accepted risk.

### Container runs: an agent on any Docker host

An agent's `runner` in `agent.json` (or `agent.local.json`) says where its runs execute. The default is this
machine (`lib/runner.py` `LocalRunner`, the per-issue workspaces above). `{"type": "container", ...}`
(`lib/container.py` `ContainerRunner`) runs each `claude -p` in a container instead, on any Docker host that reaches
Gaia, Hermes, GitHub and the Anthropic API. Talos stays the one coordinator either way: the Hermes listener, the
policy, the tracker writes and `talos.db` stay here; only the Claude session moves, and its JSON result comes back on
`docker run`'s stdout as from a local `claude -p`, so the outcome and the `runs` row are the same.

Build the image once per Docker host (`talos/container/Dockerfile`: the Claude CLI, node, python, git, gh and the
docker CLI; no credentials):

```
docker build -t p4m-talos-runner dev-tools/talos/container
```

```json
"runner": {"type": "container", "image": "p4m-talos-runner",
           "addHosts": ["api.local-projects4.me:host-gateway"]}
```

| key | meaning |
|---|---|
| `image` | the runner image (required) |
| `dockerHost` | the Docker host to run on (`DOCKER_HOST` for the `docker` CLI; default its own) |
| `network`, `addHosts`, `dockerArgs` | `docker run --network`, `--add-host` entries (e.g. the local Gaia name to `host-gateway`), extra args |
| `claudeHome` | the container's `~/.claude`: `bind` (default) a per-issue host folder `state/containers/<agent>-<issue>/claude`, so sessions resume and the run stats and `talos status` read its transcripts; `volume` a named volume `talos-<agent>-<issue>-claude` (a remote Docker host, where a host folder isn't there; sessions resume, the stats come from the result only); `none`: nothing kept, every run starts cold with the catch-up prompt |
| `testDockerHost` | `DOCKER_HOST` inside the container for the test suites; default the host's `/var/run/docker.sock` is mounted in |
| `envFiles` | credential files under `<workspace>/dev-tools` (default `odysseus/odysseus.env`, `talos/container.env`) |
| `gitBase`, `pushBase` | where repos are cloned from (default `https://github.com/projects4me`) and pushed to (default `git@github-odysseus:projects4me`, the `odysseus` remote) |

In the container the entrypoint (`talos/container/entrypoint.py`) lays out `/work`, the per-issue volume
`talos-<agent>-<issue>-work`, like a local workspace: the workspace repo and dev-tools `main` (`.workspace`,
`.dev-tools-runtime`), `CLAUDE.md`, `.claude` and `dev-tools` linked in, and the issue's repo cloned on
`claude-issue-N` (the pushed branch on `odysseus` when there is one) with the `odysseus` remote. The volume is
sticky like a workspace; remove it with `docker volume rm` when the issue is done. Issues in the workspace repo
(`pr4m`) run locally only.

**Credentials** are never in the image or on the `docker run` command line. Talos passes each one by name
(`docker run -e NAME`, so the docker CLI copies the value from the environment Talos gives it) and mounts key files
read-only under `/run/secrets/talos`. Only what the run needs crosses over, never the rest of Talos's environment:

| what | from | in the container |
|---|---|---|
| Claude | `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) or `ANTHROPIC_API_KEY`, in `dev-tools/talos/container.env` (git-ignored) or Talos's environment; a run without one fails | the same variable |
| the agent's Gaia sign-in | `GAIA_*` from the agent's `gaiaEnv` (as for a local run; never from the env files) | the same variables, for the gaia MCP server and `ai` |
| Odysseus on GitHub | `GH_TOKEN`, `ODYSSEUS_NAME`/`_EMAIL` from `odysseus/odysseus.env` (required: a run without it fails; an engineer's own `GH_TOKEN` in Talos's environment or agent.json `env` is not passed) | `gh` uses `GH_TOKEN`; git over https asks a credential helper that reads it when needed (it is not written to any git config) |
| the Odysseus SSH key | `ODYSSEUS_SSH_KEY` (default `~/.ssh/agent_odysseus`) and its `.pub`; a run fails when the file is missing | mounted read-only, copied to `~/.ssh` with mode 600 for the `github-odysseus` alias and commit signing |
| a private CA | `GAIA_CA_FILE` | mounted read-only, the variable pointed at it |
| `agent.json` `env`, the test slot | as for a local run | the same variables |

Known limits: the test suites that bind-mount a checkout into the stack's containers (`p4m test`) need that
checkout on the Docker host the tests run on, so for those repos a remote `testDockerHost` doesn't work yet; and
`talos gc` doesn't remove container volumes. Key files and the `bind` claude home are mounted by their path on
this machine, so with a remote `dockerHost` they must exist at the same path on that host. Without `testDockerHost`
the host's docker socket is mounted, which gives the agent root on the Docker host: use a dedicated Docker host or
a rootless `testDockerHost` where that matters. A container left by a stopped Talos is removed before the next run
of that issue.

Auto-memory in a container: with the `bind` claude home the workspace's auto-memory folder is mounted at `/memory`
and the run pointed at it, as for a local run; `volume` and `none` homes (meant for a Docker host without this
machine's folders) keep the container's own, empty memory. The container runs as root, so on a Linux host the memory
files it writes are owned by root, like the rest of the `bind` home; `chown` them if an interactive session can't
edit them.

## How an assignment is handled

1. **Event**: `issue.assignee.changed` where the new assignee is the agent and the actor is in the agent's
   `acceptAssignmentsFrom` (Gaia user ids). Everything else is ignored.
2. **Re-read**: the issue is fetched from Gaia; the event is only a hint. It must still be assigned to the
   agent, in one of its `projects` and `milestones`, in a status that has an `action`.
3. **Queue**: one queue per agent, one run at a time (runs share the local stack; each issue has its own workspace).
   Repeats for an issue already queued or running are dropped.
4. **Run**: the issue is first moved to the agent's `pickupStatus` (`in_progress` for Odysseus), then `claude -p "<action prompt>"` in the issue's workspace (see above), with the agent's model, role prompt
   (`--append-system-prompt`), permission mode and allowed tools, and the agent's `GAIA_*`
   credentials in the environment (so the `gaia` MCP server and `dev-tools/ai` act as that agent).
   Actions with `"resume": true` continue the issue's previous Claude session, **but only while its prompt
   cache is warm** (its last run ended less than `resumeMaxIdleMinutes`, 55, ago). A cold resume re-pays the
   whole earlier context on every turn, so after that Talos starts a fresh session and adds a catch-up
   instruction (issue thread, `git log`/`git diff --stat` of the branch) to the prompt.
   **Model**: an escalated issue stays on `model.escalate`; otherwise the issue's complexity picks it
   (`model.byComplexity`, from the `Complexity: simple|complex` line the gaia MCP tools write into the
   description); otherwise `model.default`.
   **Iterations**: every Claude run on an issue counts (escalation re-runs too). At `maxIterations` (10 for
   Odysseus) Talos stops, comments "needs manual intervention", sets `pending` and assigns `handoffTo`.
   After looking at it, `talos reset <agent> <issue>` gives it a fresh allowance.
5. **Outcome** (structured output, `schema/outcome.json`):

   | outcome | Talos does |
   |---|---|
   | `done` | checks the issue is no longer assigned to the agent (the skills hand it to the reviewer); if it still is, hands it off as failed |
   | `ready_to_ship` | starts a **fresh ship session** (same model, counts as an iteration) with `shipPrompt` and the agent's `handoff` note, so `/ship-issue` doesn't re-read the implementing session's context on every call; that session ends with one of the other outcomes. Without a `shipPrompt`, or from the ship session itself, it counts as failed |
   | `needs_input` | posts the agent's question, status `feedback`, assigns `handoffTo` |
   | `blocked` | posts the blocker, status `pending`, assigns `handoffTo` |
   | `escalate` | comments why, then resumes the same session on `model.escalate` (once); the issue stays on that model |
   | crash or no outcome | comments the error and log name, status `pending`, assigns `handoffTo` |

   You answer by commenting and assigning the issue back; the status picks the action (`feedback` and
   `pending` resume the session, `in_review` runs `/address-review`).

**Missed events.** Hermes keeps no history, so on every (re)connect Talos lists the issues assigned to each
agent and queues them. Since there's no event saying who assigned them, a reconciled issue only runs if the
user who last changed it is in `acceptAssignmentsFrom` (issues an agent created for itself are skipped).
**Interrupted runs** (Talos stopped mid-run) are handed back as failed on the next start, never silently
restarted.

## Adding an agent

Create `agents/<name>/agent.json` and its role prompt; no Talos code changes. The agent needs its own Gaia
user (and its own git identity if it commits), membership in its projects (that is what lets Hermes deliver
their events), and an env file with its `GAIA_*` credentials.

| key | meaning |
|---|---|
| `name`, `displayName`, `role`, `enabled` | identity; disabled agents are skipped by `run` |
| `gaiaEnv` | env file with the agent's `GAIA_*` credentials, relative to `dev-tools/` |
| `projects` | project name → repo folder (the `{repo}` placeholder). Odysseus: A Gaia, A Prometheus, A Hermes, A Ares and A Dev Tool. Talos runs from the runtime copy, so a merged dev-tools PR takes effect after `p4m runtime update` and a restart of `talos run` |
| `milestones` | only work issues in these (empty = any; Odysseus: any, so an issue from any milestone assigned to it is worked. Issues the agents *create* still go in Claude-v1 by default) |
| `acceptAssignmentsFrom`, `handoffTo` | Gaia user ids that may hand it work / that get questions and failures |
| `pickupStatus` | status set when Talos starts a run (e.g. `in_progress`); empty leaves it alone |
| `handoffStatuses` | status for `needs_input`, `blocked`, `failed`, `limit` (defaults `feedback`, `pending`, `pending`, `pending`) |
| `actions` | status → `{prompt, resume}`; placeholders `{issue}`, `{repo}`, `{project}`, `{status}` (never issue text) |
| `model.default`, `model.byComplexity`, `model.escalate` | e.g. `sonnet`; `{simple: sonnet, complex: opus}`; `opus` |
| `limitCommentHours` | A Claude usage/rate-limit pause pauses the whole queue until the reset (exponential backoff when none is given) and re-queues the issue without counting an iteration; the issue gets one comment only when the pause is longer than this many hours (default 1) |
| `maxIterations` | Claude runs per issue before a human must look (0 = no limit; default 10) |
| `maxTurns` | turns per run (`claude --max-turns`); when reached, the issue goes back as `needs_input` with the session kept, so assigning it back resumes it (0 = no limit; Odysseus: 80) |
| `limitsByModel` | model → `{maxTurns, maxBudgetUsd}` for runs on that model, in place of `maxTurns`; `maxBudgetUsd` is `claude --max-budget-usd` and, when reached, hands the issue back like the turn cap (Odysseus: sonnet 80 turns; opus 150 turns and $15 per run) |
| `shipPrompt` | prompt of the fresh ship session after `ready_to_ship`; placeholders as for `actions` plus `{handoff}` (empty = the outcome is not offered) |
| `builtinTools`, `deniedTools` | `claude --tools` (only these built-in tools are loaded, which keeps the fixed context small) and `--disallowedTools` |
| `env` | extra environment for the Claude process (not `GAIA_*`: the credentials come from `gaiaEnv`). Odysseus sets `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`: subagents and commands run in the foreground, so no turns go to waiting and no late task notification arrives after the outcome |
| `resumeMaxIdleMinutes` | resume a session only if its last run ended this recently (0 = always; default 55) |
| `maxParallel` | worker threads: issues run at once, each in its own workspace and test slot (default 1) |
| `maxParallelByRepo`, `maxParallelByModel` | caps on runs at once per repo and per model (model default `{"opus": 1}`) |
| `testSlots` | test slots leased among the runs (default `maxParallel`; at most `TEST_SLOTS` in `p4m.env`) |
| `permissionMode`, `allowedTools` | passed to `claude -p` |
| `rolePrompt` | file appended to the system prompt: the agent's role and headless rules |
| `runner` | where runs execute: this machine (default) or `{"type": "container", "image": ...}`, see "Container runs" |

`config.json`: `hermesUrl`, `claudeBin` (empty = PATH, then the newest VS Code extension binary), `nodeBin`,
`reconnectSeconds`. `config.local.json` (git-ignored) overrides any of them per engineer.

**Per-engineer settings.** Everyone runs their own Talos with the shared Agent Odysseus identity, so each copy must
act only on the issues *its* engineer assigns. `agents/<name>/agent.local.json` (git-ignored, written by
`dev-tools/installer/p4m configure`) sets `acceptAssignmentsFrom` and `handoffTo` to that engineer's Gaia user id and
`reviewerName` to their name (used in the prompts as `{reviewer}`). Talos refuses to start an agent without them.

## Ideas agents (Agent Nestor)

An ideas agent talks with an engineer about an idea (Prometheus' Ideas section), proposes an implementation plan,
and creates the approved plan's issues. Ideas live in Gaia and Gaia publishes their events through Hermes; Gaia
and Prometheus know nothing about agents: Talos decides that Agent Nestor answers every idea in his projects.
With `"ideaSource": "sim"` in agent.json the agent uses the [`dev-tools/sim`](../sim/README.md) stand-ins instead
(and the [Nestor UI](../nestor/README.md)), until the sim is retired.

```
Prometheus ─▶ Gaia ─publish─▶ Hermes ─idea.message.created──────▶ Talos ─claude -p (streamed)─▶ agent
                                     ─idea.plan.status.changed─▶   │  └─ notice / reply / plan ─gaia MCP─▶ Gaia
                                                                   └─ approved plan ─gaia MCP─▶ Gaia issues
```

- **Events** (lib/hermes.py `Listener`, lib/ideas/policy.py `gaia_job_for`): `idea.message.created` and
  `idea.plan.status.changed` in every project of the agent. A `kind: message` from someone else starts a turn, a
  plan set to `approved` starts the issue creation. The agent's own events are ignored, and only users in
  `acceptAssignmentsFrom` hand it work; the worker re-reads the idea (events are hints). On the sim: agent.json
  `events`, user-scoped intents `user:<agent id>`, and `agent_state: stopping` stops the running turn.
- **Idea client** (lib/ideas/client.py on the gaia MCP's idea tools; sim_client.py on the sim): maps Gaia's
  records onto one shape. A message's `kind` plus its author become its role (`message` by the agent is its
  reply), `ideaplanId`/`costUsd` carry the plan and cost, `steps` and `screens` are JSON; the idea's screens are
  the uploads its messages list; a plan's created issues are its Ideaplanissues; the idea's owner is who created
  it. Idea ids are GUIDs. Gaia ideas have no agent state: a turn starts with a `kind: system` notice ("Agent
  Nestor is working on it"), and there is no live progress (Hermes has no agent publish path).
- **A turn** (lib/ideas/conversation.py): takes in every engineer message since the last turn (also ones sent
  while the agent was answering), resumes the idea's Claude session (or replays the conversation when there is
  none), syncs the `ai kb` issue index (at most every `kbMaxAgeMinutes`), runs `claude -p --output-format
  stream-json` (on the sim, publishing `idea.agent.progress`: text in small batches, steps, turns), then posts
  the reply with its steps, cost and screens. A
  fenced `nestor-plan` JSON block in it is validated (lib/ideas/plan.py; descriptions reduced to plain HTML by
  sanitize.py) and posted as a new plan version; an invalid block gets one correction round.
- **Milestone**: every plan issue goes in **Claude-v1** unless the engineer asked for another; Nestor then names
  it (`milestone` in the plan block). Talos uses the project's exact spelling, marks a milestone the project
  doesn't have as new (the plan card shows it), and the gaia MCP client creates it when the issue is created.
- **UI screens** (lib/ideas/screens.py, render.py): when asked, the agent writes `nestor-screen` blocks of HTML in
  Prometheus' markup; Talos sanitises them (no scripts, styles or external URLs), renders each to a PNG with a
  headless Chromium (Edge or Chrome on Windows; Chromium, Chrome or Edge on Linux; `browserBin` to choose) and
  uploads it to the idea, listed in the reply's `screens` (on the sim: `ideascreen`), where the UI shows it. The page uses Prometheus' real built CSS from the
  running app (`prometheusUrl`, else `appUrl` from `p4m configure`), falling back to the checkout's
  Bootstrap/AdminLTE plus screen_kit/projects4me.css. Plan issues list `screens`; on approval their latest PNGs
  are attached to the created issues (a failed attachment is reported in the idea). No browser, or a screen that fails to render: the reply says so and the plan simply doesn't attach it.
- **Approval** (lib/ideas/approval.py): no LLM. The approved version's issues are created through the gaia MCP
  client as the agent, assigned to the idea's owner, in dependency order, each with a "Depends on" line; each is
  recorded on the idea at once, so approving a failed plan again creates only the missing ones. The agent is told
  the numbers at the start of its next turn.
- **One job at a time** per agent (lib/ideas/worker.py); after each (re)connect it reconciles, per project:
  messages not followed by the agent's reply get a turn (so a Talos restarted mid-turn answers), and plans left
  `approved` or `creating` carry on (one issue may then be created twice; see approval.py).
- **Tool policy** in agent.json: `builtinTools` (`--tools`: only these are loaded, which keeps the context small),
  `allowedTools` with `permissionMode: dontAsk`, and `deniedTools` (beats any settings allow rule; Nestor's deny
  Write, git, the gaia write tools and reading credentials). MCP is only the gaia server
  (`--strict-mcp-config`, state/<agent>/mcp.json). `knowledgeMap`: the one file the agent may edit.
- **State**: the conversation lives in Gaia (or the sim); Talos keeps the agent's own memory in the `ideas` table.

Config: `hermesUrl` (as for issue agents); on the sim, `sim.url` and `sim.secretFile` (or `sim.secret` in
config.local.json); `kbMaxAgeMinutes`; `browserBin` and `prometheusUrl` for screens (empty: found automatically).

## State: `state/talos.db` (SQLite)

Readable by other tools while Talos runs (WAL mode), e.g. a future Projects4Me UI panel.

- `issues`: one row per agent and issue: session to resume, its cumulative cost, run count (toward
  `maxIterations`), escalation, `running`, the live session id, `started_at`, `last_ended_at`.
- `runs`: one row per Claude run, inserted when it starts (`ended_at` NULL while running): iteration, model,
  session, resumed or not, status at start, duration, outcome, summary, **`cost_usd` (this run alone)** and
  `session_cost_usd` (Claude reports a resumed session's cumulative cost; Talos subtracts what the session had
  already cost), plus turns, input/cache-read/cache-write/output tokens and peak context from the session
  transcript (main session; subagents are in the cost but not the token counts).
- `ideas`: one row per ideas agent and idea: session to resume and its cumulative cost, the note for the next turn,
  the last engineer message a turn has taken in, `last_ended_at`.

The pre-SQLite `state/sessions.json` is imported when `talos run`/`dispatch` starts, then renamed
`sessions.json.imported`. `state/talos.log` and the raw JSON per run in `state/logs/` stay as they were.

## Layout

| file | responsibility |
|---|---|
| `talos.py` | CLI: wires the pieces per agent |
| `lib/registry.py` | loads and validates `agents/*/agent.json` |
| `lib/hermes.py` + `listener/listen.js` | Hermes connection: Node Socket.IO client, supervised and restarted with a fresh token |
| `lib/policy.py` | pure decisions: which events count, whether to run, prompt filling, outcome parsing |
| `lib/dispatcher.py` | per-agent queue; runs a job and applies the outcome to the tracker |
| `lib/runner.py` | builds and runs the `claude -p` command, keeps its output; the `Runner` interface and `LocalRunner` |
| `lib/container.py` + `container/` | `ContainerRunner`: the run in a container (`docker run` with the credentials by name); the image and its entrypoint |
| `lib/workspaces.py` | per-issue workspaces: `.runs/issue-N` with the repo's worktree and the tooling linked in |
| `lib/memory.py` | the workspace's Claude auto-memory folder, which every run is pointed at |
| `lib/mcp.py` | the agent's MCP config: only the gaia server from the copy Talos runs from |
| `lib/tracker.py` | Gaia as one agent, through the `gaia-mcp` client |
| `lib/db.py` | SQLite connection, schema, one-time import of `sessions.json` |
| `lib/sessions.py` | issue state (`issues` table): session, run count, escalation, in-flight flag |
| `lib/runs.py` | run records (`runs` table) and the per-run cost calculation |
| `lib/simbus.py` | sim Hermes connection (SSE): the event source for ideas agents until Hermes carries idea events |
| `lib/ideas/` | ideas agents: `setup` (wiring), `policy` (events, what waits), `worker` (queue, stop, reconcile), `conversation` (a turn), `approval` (plan → issues), `stream` (streamed run, command), `client` (Gaia idea resources) and `sim_client` (the sim's), `progress` (live progress on the sim), `plan`, `screens` (blocks) + `render` (= `styles` + `page` + `browser`) + `screen_kit/` (UI screens → PNG), `sanitize`, `steps`, `kb`, `state` |

## Parallel runs

With `maxParallel` above 1 an agent has that many worker threads. The queue hands out resumptions (feedback,
pending, in_progress, in_review: any status whose action resumes a session) first, then by issue priority, then first
in, first out; an issue already queued or running is never queued twice. Before Claude starts, a run waits for a free
slot under the repo and model caps, then leases a test slot: `P4M_TEST_SLOT` is set in its environment and
`ai test gaia` runs in that slot (`--slot` overrides). The lease is released when the run ends. A ship session or an
escalation re-run leases again. `talos status` lists every run in flight with its repo and slot.

Shared files: `ai/state/timers.json` is changed under a lock (`state/timers.lock`). Browser checks against the shared
dev stack (gaia-dev, Prometheus on :4200) take turns with `dev-tools/ai/ai devstack -- <command>`.
