# dev-tools/ai: workflow automation for Claude Code

Fixed sequences that Claude Code used to run as 5–15 separate tool calls each (git, `gh` as Agent
Odysseus, the Projects4Me tracker, tests) are commands here. Steps that need judgement are skills and
agents in `.claude/` that call these commands.

Everything runs as **Agent Odysseus**: git/gh through `dev-tools/odysseus/odysseus`, the tracker through
the `dev-tools/gaia-mcp` client (same credentials and code as the `gaia` MCP server).

## Commands (`dev-tools/ai/ai <command> -h` for options)

| Command | Replaces |
|---|---|
| `start <repo> <issue>` | fetch, checkout dev, ff-merge, create `claude-issue-N`, set `in_progress`, start timer, compact issue view |
| `sync <repo>` | fetch + fast-forward local dev after a merge |
| `issue <n> [--full]` | plain-text issue view (no HTML/JSON), last 10 comments |
| `time <issue> "<what>"` | logs **actual elapsed** time since the issue's last checkpoint ("Actual elapsed HH:MM-HH:MM PKT: …") and moves the checkpoint |
| `timer <issue>` | reset the checkpoint (e.g. when resuming an issue later) |
| `comment <issue> <file.html>` / `status <issue> <status> [--assignee]` | tracker comment / status |
| `kb sync [--full]` / `kb search <words> [--project aprom] [--status new] [--type Bug] [--kind issue|doc|node|pr]` / `kb section <file#anchor>` / `kb show <n>` / `kb stats` | listing and reading many issues to find the related ones: a local SQLite full-text index (`state/kb.db`) of every issue you can see, with comments. `sync` reads only what changed since the last sync (issues modified, plus issues with new comments; ~4s); `--full` rebuilds (deleted issues and comments stay until then). A failed sync leaves the index unchanged. `search` ranks subject > description > comments, stems words, any word may match; `show` reads from the index (no API call). `sync` also indexes the Gaia checkout's `docs/generated` and `docs/architecture` sections, `graph.json` nodes (models, routes, controllers, components, permissions, profiles, assertions) and the exemplar PRs of `docs/dev/bundles.md` (content-hashed, so only changed ones are rewritten); `search` prints each hit's `file#anchor` and size, `section` prints that one section (≤ 40 lines) |
| `context <issue>` | 20–35 exploratory `cat`/`sed`/`grep` calls: outlines (declarations + line numbers) of the files, classes and endpoints the issue names, their tests, api-tester cases, recent commits, referenced issues' status; for Gaia issues also a capped pack (`gaiaPackLines`, default 300): `gaia-dev` model, ACL and case answers, matching doc sections, closest bundle with its exemplar PR, and the gaia-dev checks to run before shipping |
| `test <repo> [--changed\|--filter X]` | gaia: phpunit security + utils, prepare-test, api backend,acl,client; prometheus: ember; hermes; dev-tools: unittest. Runs against **this workspace's checkout** (throwaway container when the shared one serves another folder). Failures are `(known)` if in `config.json` or failing on the base commit (name baseline, recorded on demand), else `<-- unexpected`. `--changed` runs only the tests mapped from changed files (`lib/testmap.py`). When `app/metadata/model/*` or `core/config/routes.php` changed, the api run first regenerates `backend.json` (`p4m test api --regenerate-backend`); a filter matching 0 cases is warned about, not reported as a pass. Full logs in `state/` |
| `doctor gaia [--json]` | the Gaia test environment in one call (also run by `ai start` for Gaia, silent when healthy): test container, CRLF on harness scripts, `pr4m_test` seeded and matching `pr4m`, `backend.json` drift, base baseline, odysseus remote. Each failure prints its fix command |
| `test gaia --slot K` | run the gaia suites in test slot K (own container and database `pr4m_test_K`, reset from a template instead of re-seeded), so several runs can go at once; logs get a `-slotK` suffix. See `p4m test slots` in `installer/P4M-CLI.md` |
| `test gaia --explain <case-id>` / `--case <id>` | expected vs actual for one failed api case from `state/test-gaia-api.log` (+ the JSON report's request); run one api case on the already-seeded DB without re-seeding (substring match on id/path/resource; a case that needs earlier cases' data needs the full run; never satisfies `ai ship`) |
| `baseline <repo>` | stash-and-rerun on dev to see if a failure is old: records the failing test names on `origin/<base>` |
| `ship <repo> <issue> ship.json [--dry-run]` | the whole hand-off: refuses unless the last full test run passed on this exact tree, then (Gaia only) the gaia-dev gate (`graph export --check`, `docs generate --check`, `check api-cases`; skipped with a notice when php/docker is unavailable), then commit, push, PR (body from the spec + recorded test result), one tracker comment (notes, tests, PR link), time, `in_review`, reviewer. `--defer-handoff` stops before the status change (issue stays `in_progress` with the agent, pending record in `ai/state`) until `ai handoff <issue>` |
| `commit <repo> <issue> -F msg --files …` | branch check, refuse if origin/<base> moved, refuse unrelated staged files / never-commit files, commit (+co-author trailer), push to `odysseus` (works in submodules) |
| `pr <repo> <issue> --title … --body-file …` | PR as Agent Odysseus (+footer), comment the link on the issue, log time, `in_review`, assign the reviewer |
| `handoff <issue>` | finishes a deferred hand-off (`ship --defer-handoff`): in_review, assign the reviewer, clear the record |
| `review <repo> <pr> [--open] [--raw]` | reviews + inline threads, marks answered / resolved (GitHub's thread state); bot boilerplate folded to one line each, findings kept in full (`--raw` shows everything) |
| `rabbit <repo> <pr> [--json] [--wait [--timeout MIN]]` | CodeRabbit's state for the PR's head commit, read from GitHub: `clean`, `findings` (open bot threads, with comment ids; same open rule as `review --open`), `pending`, `paused` (auto review paused / rate-limited) or `absent` (no review within the timeout, or the check run ended in another conclusion than success). It reads the head commit's check run `CodeRabbit` from the `coderabbitai` app (queued/in progress = pending, completed + success = done), and falls back to the legacy `CodeRabbit` commit status when there is no check run. Exit code 0/1/2/3/4; `--wait` polls every 30 s until not pending |
| `reply <repo> <pr> replies.json` | replies to many threads in one call (`{"<comment id>": "<reply>"}`) |
| `pr-body <repo> <pr> <file.md>` | replace a PR description |
| `api-cases client\|acl <cases.json>` | append api-tester cases in the catalog's own style (no re-serialising), duplicate-id check |
| `call METHOD <path> [json] [--target test\|dev] [--profile aclNoSetting] [--jq data.0]` | log in and call the API (test API as an api-tester profile, dev as Agent Odysseus), truncated output |

Config: `config.json` (co-author trailer, PR footer, never-commit files, known test failures), overlaid by your
git-ignored `config.local.json` (`reviewer`, `timezoneLabel`), which `dev-tools/installer/p4m configure` writes.
`ai cost [--repo R] [--issue N] [--since DATE] [--json]` reads the Claude Code transcripts of this workspace and
reports tokens, turns and peak context per session, issue and repo, tool-result chars per tool and Bash category,
and files read in 2+ sessions. `ai ship` adds the issue's cost line to the hand-off comment. The baseline is
`state/cost-baseline.md` (the one tracked file in `state/`).

State (timers, test logs, the `kb` index): `state/` (local only; the index holds tracker content, which stays out
of this public repo).

## Skills and agents (`.claude/` in the workspace)

| Kind | Name | Use |
|---|---|---|
| skill | `/start-issue <n>` | `ai start`, then dependency + rule checks before coding |
| skill | `/ship-issue <n>` | tests (agent), design review (agent), notes, approval, `ai commit`, `ai pr` |
| skill | `/address-review <repo> <pr> <issue>` | `ai review --open`, changes, re-test, follow-up commit, `ai reply`, `ai pr-body`, tracker |
| agent | `test-runner` (Haiku) | runs `ai test`, returns pass/fail + unexpected failures only |
| agent | `design-reviewer` (Sonnet) | reviews the diff against the `### Design` rules (Fowler layering, SOLID) and Gaia review rules |

Repos: `gaia`, `prometheus`, `hermes`, `ares` (base `dev`), `dev-tools` and `pr4m` (the workspace root; base `main`).

## Typical issue, end to end

```
/start-issue 3960                      # ai start gaia 3960, ai context 3960, checks
… implement; ai test gaia --changed while fixing …
/ship-issue 3960                       # test-runner (full ai test), design-reviewer, ai ship (after approval)
/address-review gaia 180 3960          # after the reviewer comments
dev-tools/ai/ai sync gaia              # after merge (start does this too)
```

## Running tests safely

- Don't pipe `ai test` through `tail`/`head`: the log `state/test-<repo>-<suite>.log` is written as the run
  progresses, so read that instead. Prefer `--changed` or a plain-text `--filter`.
- From PowerShell or cmd run `dev-toolsii ...` (`ai.cmd`, same runtime-copy selection and `P4M_RUNTIME=off`). A bare
  `bash` in PowerShell is the WSL launcher, not Git Bash, so `bash dev-tools/ai/ai` fails there.
- The launchers set `MSYS_NO_PATHCONV=1`, so `--filter "/idea/i"` reaches QUnit unchanged from Git Bash. A filter
  that looks like a converted Windows path is rejected.
- `ember test` is stopped (and its Chrome killed in the container) when nothing matches the filter, a QUnit
  global/load failure is reported, it exceeds 15 minutes, or the run is interrupted. A new run first clears
  leftovers of an earlier one.

## Guard hooks (`hooks/guard.py`)
PreToolUse hook on Bash and Read, wired in the workspace `.claude/settings.json`. It redirects, never truncates:
reads of `RestController.php`, `Acl.php`, `Model.php`, `tools/api-tester/{src,apis,harness}/**` and
`fixtures/default.json` are denied with the `gaia-dev` command that answers the question (escape: the file is in the
branch's changed set, or the command has `--raw` / `# raw`); `docker exec|run` of a test suite is denied in favour of
`ai test`. Blocks and `--raw` uses go to `state/hooks.jsonl`; `ai cost` reports the bypass rate. `ai ship` adds a
warning to the PR and tracker comment when the issue's output tokens exceed the repo's per-issue median (never blocks).
