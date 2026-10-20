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
| `kb sync [--full]` / `kb search <words> [--project aprom] [--status new] [--type Bug]` / `kb show <n>` / `kb stats` | listing and reading many issues to find the related ones: a local SQLite full-text index (`state/kb.db`) of every issue you can see, with comments. `sync` reads only what changed since the last sync (issues modified, plus issues with new comments; ~4s); `--full` rebuilds (deleted issues and comments stay until then). A failed sync leaves the index unchanged. `search` ranks subject > description > comments, stems words, any word may match; `show` reads from the index (no API call) |
| `context <issue>` | 20–35 exploratory `cat`/`sed`/`grep` calls: outlines (declarations + line numbers) of the files, classes and endpoints the issue names, their tests, api-tester cases, recent commits, referenced issues' status |
| `test <repo> [--changed\|--filter X]` | gaia: phpunit security + utils, prepare-test, api backend,acl,client; prometheus: ember; hermes; dev-tools: unittest. Runs against **this workspace's checkout** (throwaway container when the shared one serves another folder). Failures are `(known)` if in `config.json` or failing on the base commit (name baseline, recorded on demand), else `<-- unexpected`. `--changed` runs only the tests mapped from changed files (`lib/testmap.py`). Full logs in `state/` |
| `baseline <repo>` | stash-and-rerun on dev to see if a failure is old: records the failing test names on `origin/<base>` |
| `ship <repo> <issue> ship.json [--dry-run]` | the whole hand-off: refuses unless the last full test run passed on this exact tree, then commit, push, PR (body from the spec + recorded test result), one tracker comment (notes, tests, PR link), time, `in_review`, reviewer |
| `commit <repo> <issue> -F msg --files …` | branch check, refuse if origin/<base> moved, refuse unrelated staged files / never-commit files, commit (+co-author trailer), push to `odysseus` (works in submodules) |
| `pr <repo> <issue> --title … --body-file …` | PR as Agent Odysseus (+footer), comment the link on the issue, log time, `in_review`, assign the reviewer |
| `handoff <repo> <issue> <pr-url>` | the tracker half of `pr` for a PR opened another way |
| `review <repo> <pr> [--open] [--raw]` | reviews + inline threads, marks answered / resolved (GitHub's thread state); bot boilerplate folded to one line each, findings kept in full (`--raw` shows everything) |
| `reply <repo> <pr> replies.json` | replies to many threads in one call (`{"<comment id>": "<reply>"}`) |
| `pr-body <repo> <pr> <file.md>` | replace a PR description |
| `api-cases client\|acl <cases.json>` | append api-tester cases in the catalog's own style (no re-serialising), duplicate-id check |
| `call METHOD <path> [json] [--target test\|dev] [--profile aclNoSetting] [--jq data.0]` | log in and call the API (test API as an api-tester profile, dev as Agent Odysseus), truncated output |

Config: `config.json` (co-author trailer, PR footer, never-commit files, known test failures), overlaid by your
git-ignored `config.local.json` (`reviewer`, `timezoneLabel`), which `dev-tools/installer/p4m configure` writes.
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
