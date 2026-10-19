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
| `test <repo>` | gaia: phpunit security + utils, prepare-test, api backend,acl,client. Prints summary lines and failures marked known/unexpected; full logs in `state/` |
| `commit <repo> <issue> -F msg --files …` | branch check, refuse if origin/dev moved, refuse unrelated staged files / never-commit files, commit (+co-author trailer), push to `odysseus` |
| `pr <repo> <issue> --title … --body-file …` | PR as Agent Odysseus (+footer), comment the link on the issue, log time, `in_review`, assign the reviewer |
| `handoff <repo> <issue> <pr-url>` | the tracker half of `pr` for a PR opened another way |
| `review <repo> <pr> [--open]` | reviews + inline threads, marks which I already answered |
| `reply <repo> <pr> replies.json` | replies to many threads in one call (`{"<comment id>": "<reply>"}`) |
| `pr-body <repo> <pr> <file.md>` | replace a PR description |
| `api-cases client\|acl <cases.json>` | append api-tester cases in the catalog's own style (no re-serialising), duplicate-id check |
| `call METHOD <path> [json] [--target test\|dev] [--profile aclNoSetting] [--jq data.0]` | log in and call the API (test API as an api-tester profile, dev as Agent Odysseus), truncated output |

Config: `config.json` (co-author trailer, PR footer, never-commit files, known test failures), overlaid by your
git-ignored `config.local.json` (`reviewer`, `timezoneLabel`), which `dev-tools/installer/p4m configure` writes.
State (timers, test logs): `state/` (local only).

## Skills and agents (`.claude/` in the workspace)

| Kind | Name | Use |
|---|---|---|
| skill | `/start-issue <n>` | `ai start`, then dependency + rule checks before coding |
| skill | `/ship-issue <n>` | tests (agent), design review (agent), notes, approval, `ai commit`, `ai pr` |
| skill | `/address-review <repo> <pr> <issue>` | `ai review --open`, changes, re-test, follow-up commit, `ai reply`, `ai pr-body`, tracker |
| agent | `test-runner` (Haiku) | runs `ai test`, returns pass/fail + unexpected failures only |
| agent | `design-reviewer` (Sonnet) | reviews the diff against the `### Design` rules (Fowler layering, SOLID) and Gaia review rules |

## Typical issue, end to end

```
/start-issue 3960                      # ai start gaia 3960 + checks
… implement …
/ship-issue 3960                       # test-runner, design-reviewer, ai commit, ai pr (after approval)
/address-review gaia 180 3960          # after the reviewer comments
dev-tools/ai/ai sync gaia              # after merge (start does this too)
```
