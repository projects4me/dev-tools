# Talos: Projects4Me issues → headless Claude Code agents

Talos listens to **Hermes** for issue assignments and hands each one to the right agent, running
`claude -p` headless. The tracker is the only channel: work, questions, blockers and failures all end up
on the issue, and **the assignee is the baton**: whoever an issue is assigned to has the next move.

```
Gaia ──publish──▶ Hermes ──issue.assignee.changed──▶ Talos ──claude -p──▶ agent (e.g. Odysseus)
                                                       │                     │
                                                       └── re-reads issue ◀──┴── comments / PR / hand-off
```

## Run

```bash
dev-tools/talos/talos check                    # Claude CLI, node + socket.io-client, Hermes, agent sign-in
dev-tools/talos/talos agents                   # the registry, with Gaia user and project ids resolved
dev-tools/talos/talos run [--dry-run]          # listen until Ctrl+C (Windows: talos.cmd)
dev-tools/talos/talos dispatch odysseus 3714 [--dry-run]   # work one issue now, as if just assigned
dev-tools/talos/talos status [--steps 20]                  # what is running now + the live transcript's latest steps
dev-tools/talos/talos runs [--issue 3714] [--limit 50]     # per-run stats: model, outcome, cost of that run, turns, context
dev-tools/talos/talos reset odysseus 3714 [--model]        # after a manual look: fresh iteration allowance
python -m unittest discover -s dev-tools/talos/tests       # unit tests (from dev-tools/talos: -s tests)
```

`--dry-run` decides and logs everything but never starts Claude or writes to the tracker. First-time setup:
`npm install` in this folder (the listener needs `socket.io-client`).

## How an assignment is handled

1. **Event**: `issue.assignee.changed` where the new assignee is the agent and the actor is in the agent's
   `acceptAssignmentsFrom` (Gaia user ids). Everything else is ignored.
2. **Re-read**: the issue is fetched from Gaia; the event is only a hint. It must still be assigned to the
   agent, in one of its `projects` and `milestones`, in a status that has an `action`.
3. **Queue**: one queue per agent, one run at a time (runs share working trees and the local stack).
   Repeats for an issue already queued or running are dropped.
4. **Run**: the issue is first moved to the agent's `pickupStatus` (`in_progress` for Odysseus), then `claude -p "<action prompt>"` from the workspace root, with the agent's model, role prompt
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
| `projects` | project name → repo folder (the `{repo}` placeholder) |
| `milestones` | only work issues in these (empty = any) |
| `acceptAssignmentsFrom`, `handoffTo` | Gaia user ids that may hand it work / that get questions and failures |
| `pickupStatus` | status set when Talos starts a run (e.g. `in_progress`); empty leaves it alone |
| `handoffStatuses` | status for `needs_input`, `blocked`, `failed`, `limit` (defaults `feedback`, `pending`, `pending`, `pending`) |
| `actions` | status → `{prompt, resume}`; placeholders `{issue}`, `{repo}`, `{project}`, `{status}` (never issue text) |
| `model.default`, `model.byComplexity`, `model.escalate` | e.g. `sonnet`; `{simple: sonnet, complex: opus}`; `opus` |
| `maxIterations` | Claude runs per issue before a human must look (0 = no limit; default 10) |
| `maxTurns` | turns per run (`claude --max-turns`); when reached, the issue goes back as `needs_input` with the session kept, so assigning it back resumes it (0 = no limit; Odysseus: 80) |
| `resumeMaxIdleMinutes` | resume a session only if its last run ended this recently (0 = always; default 55) |
| `permissionMode`, `allowedTools` | passed to `claude -p` |
| `rolePrompt` | file appended to the system prompt: the agent's role and headless rules |

`config.json`: `hermesUrl`, `claudeBin` (empty = PATH, then the newest VS Code extension binary), `nodeBin`,
`reconnectSeconds`. `config.local.json` (git-ignored) overrides any of them per engineer.

**Per-engineer settings.** Everyone runs their own Talos with the shared Agent Odysseus identity, so each copy must
act only on the issues *its* engineer assigns. `agents/<name>/agent.local.json` (git-ignored, written by
`dev-tools/installer/p4m configure`) sets `acceptAssignmentsFrom` and `handoffTo` to that engineer's Gaia user id and
`reviewerName` to their name (used in the prompts as `{reviewer}`). Talos refuses to start an agent without them.

## State: `state/talos.db` (SQLite)

Readable by other tools while Talos runs (WAL mode), e.g. a future Projects4Me UI panel.

- `issues`: one row per agent and issue: session to resume, its cumulative cost, run count (toward
  `maxIterations`), escalation, `running`, the live session id, `started_at`, `last_ended_at`.
- `runs`: one row per Claude run, inserted when it starts (`ended_at` NULL while running): iteration, model,
  session, resumed or not, status at start, duration, outcome, summary, **`cost_usd` (this run alone)** and
  `session_cost_usd` (Claude reports a resumed session's cumulative cost; Talos subtracts what the session had
  already cost), plus turns, input/cache-read/cache-write/output tokens and peak context from the session
  transcript (main session; subagents are in the cost but not the token counts).

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
| `lib/runner.py` | builds and runs the `claude -p` command, keeps its output |
| `lib/tracker.py` | Gaia as one agent, through the `gaia-mcp` client |
| `lib/db.py` | SQLite connection, schema, one-time import of `sessions.json` |
| `lib/sessions.py` | issue state (`issues` table): session, run count, escalation, in-flight flag |
| `lib/runs.py` | run records (`runs` table) and the per-run cost calculation |
