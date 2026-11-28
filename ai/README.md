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
| `sync <repo>` / `sync --all [--check]` | fast-forward local dev (main for dev-tools) to origin for one repo or for gaia, prometheus, hermes, ares and dev-tools, **without switching branches or touching the working tree**: a checkout on an issue branch updates `dev` with `git fetch origin dev:dev`; a clean checkout of `dev` is fast-forwarded; a dirty or diverged repo is skipped with a `WARNING` and the others still sync. In a Talos run worktree it only fetches. `ai merge` syncs the merged repo, and `talos run` runs `sync --all` every 10 minutes. `--check` only prints `behind origin/<base>: N` per repo. Agents that read merged code should read `origin/dev` after a fetch, not local `dev` |
| `issue <n> [--full]` | plain-text issue view (no HTML/JSON), last 10 comments |
| `time <issue> "<what>"` | logs **actual elapsed** time since the issue's last checkpoint ("Actual elapsed HH:MM-HH:MM PKT: …") and moves the checkpoint |
| `timer <issue>` | reset the checkpoint (e.g. when resuming an issue later) |
| `comment <issue> (<file.html> or --text HTML)` / `status <issue> <status> [--assignee]` | tracker comment / status |
| `kb sync [--full]` / `kb search <words> [--project aprom] [--status new] [--type Bug] [--kind issue|doc|node|pr]` / `kb section <file#anchor>` / `kb show <n>` / `kb stats` | listing and reading many issues to find the related ones: a local SQLite full-text index (`state/kb.db`) of every issue you can see, with comments. `sync` reads only what changed since the last sync (issues modified, plus issues with new comments; ~4s); `--full` rebuilds (deleted issues and comments stay until then). A failed sync leaves the index unchanged. `search` ranks subject > description > comments, stems words, any word may match; `show` reads the issue from the index and its links (same groups as `context`) from Gaia, so it prints `links unavailable` when Gaia is down. `sync` also indexes the Gaia checkout's `docs/generated` and `docs/architecture` sections, `graph.json` nodes (models, routes, controllers, components, permissions, profiles, assertions) and the exemplar PRs of `docs/dev/bundles.md` (content-hashed, so only changed ones are rewritten); `search` prints each hit's `file#anchor` and size, `section` prints that one section (≤ 40 lines) |
| `screen render` | Nestor's preview: reads one ```` ```nestor-screen ```` block on stdin, cleans it and renders it with Talos' renderer to `talos/state/nestor/previews/<key>.png`; prints the path, the stylesheet source and cleaning problems. Non-zero exit for no block, an invalid block or no Chromium-based browser |
| `context <issue>` | 20–35 exploratory `cat`/`sed`/`grep` calls: outlines (declarations + line numbers) of the files, classes and endpoints the issue names, their tests, api-tester cases, recent commits, the issue's links from Gaia (blocked by with open blockers first and marked `!`, blocks, parent, children, split, relates; 10 per group; text-only `depends on #N` lines kept as legacy "Blocked by (line)"; a clear message when Gaia is unreachable); for Gaia issues also a capped pack (`gaiaPackLines`, default 300): `gaia-dev` model, ACL and case answers, matching doc sections, closest bundle with its exemplar PR, and the gaia-dev checks to run before shipping. `--json` prints only the resolved files (`{issue, repo, files}`), for Talos' overlap hold |
| `test <repo> [--changed\|--filter X\|--suite S]` | gaia: api backend,acl,client, then search (prepares the test db; `p4m test api` loads the search fixture between them; a checkout without `apis/search.json` skips search with a note), then the whole `tests/phpunit.xml` suite with `--exclude-group quarantine`, as Gaia CI runs it (`--changed`/`--filter` narrow only the api run; skipped tests are not failures); prometheus: lint (`lint:js` + `lint:hbs`, as CI runs them; any error fails the run and the ship gate, with the stale-entry fix as a test-container command: `p4m test lint --prune` / `--clean-todo <file>`, which run in the container and copy the file they rewrote (`.lint-todo` / `eslint-suppressions.json`) back, and make the next sync push the checkout's copy again; never a host npx) then ember; hermes; dev-tools: unittest. Runs against **this workspace's checkout** (throwaway container when the shared one serves another folder). Failures are `(known)` if in `config.json` or failing on the base commit (name baseline, recorded on demand), else `<-- unexpected`. `--changed` runs only the tests mapped from changed files (`lib/testmap.py`). When `app/metadata/model/*` or `core/config/routes.php` changed, the api run first regenerates `backend.json` (`p4m test api --regenerate-backend`); a filter matching 0 cases is warned about, not reported as a pass. `--suite S` runs only that one of the repo's suites (e.g. `prometheus --suite lint`; never a full run). Unslotted api runs seed `pr4m_test` with the checkout's own schema (a scratch copy of pr4m migrated with the checkout's code; pr4m itself is never migrated), as slots do, so the baseline and the branch see the same schema. Full logs in `state/` |
| `doctor gaia [--json]` | the Gaia test environment in one call (also run by `ai start` for Gaia, silent when healthy): test container, CRLF on harness scripts, `pr4m_test` seeded and matching `pr4m`, `backend.json` drift, base baseline, vendor packages the gaia-dev checks run with vs `composer.lock` (a stale `nikic/php-parser` makes every graph look stale; fix `p4m shell gaia composer install`), odysseus remote. Each failure prints its fix command |
| `test gaia --slot K` | run the gaia suites in test slot K (own container and database `pr4m_test_K`, reset from a template instead of re-seeded), so several runs can go at once; logs get a `-slotK` suffix. See `p4m test slots` in `installer/P4M-CLI.md` |
| `test gaia --explain <case-id>` / `--case <id>` | expected vs actual for one failed api case from `state/test-gaia-api.log` (`-slotK`, `-issue-N` suffixes in a test slot or Talos run) (+ the JSON report's request); run one api case on the already-seeded DB without re-seeding (substring match on id/path/resource; a case that needs earlier cases' data needs the full run; never satisfies `ai ship`) |
| `baseline <repo>` | stash-and-rerun on dev to see if a failure is old: records the failing test names on `origin/<base>` |
| `ship <repo> <issue> ship.json [--dry-run]` | the whole hand-off: refuses unless the last full test run passed on this exact tree, then (Gaia only) the gaia-dev gate (`graph export --check`, `docs generate --check`, `check api-cases`; skipped with a notice when php/docker is unavailable; a failing check is re-run on the merge-base in the baseline worktree, and one failing there with the same output lines (paths aside; empty output never counts) is reported as known, noted in the tracker comment and doesn't stop the ship: only drift the branch adds does), then commit, push, PR (body from the spec + recorded test result), one tracker comment (notes, tests, PR link), time, `in_review`, reviewer. `--defer-handoff` stops before the status change (issue stays `in_progress` with the agent, pending record in `ai/state`) until `ai handoff <issue>`, so Talos can route the issue. It adds `DESIGN REVIEW: clean` to the How tested block only when the review `ai design-review` recorded is clean and matches the tree; a missing, stale or unclean review is reported and the PR opens without the line. The line is for the reviewer: the merge gate doesn't read it |
| `commit <repo> <issue> -F msg --files …` | branch check, refuse if origin/<base> moved, refuse unrelated staged files / never-commit files, commit (+co-author trailer), push to `odysseus` (works in submodules) |
| `pr <repo> <issue> --title … --body-file …` | PR as Agent Odysseus (+footer), comment the link on the issue, log time, `in_review`, assign the reviewer |
| `handoff <issue>` | finishes a deferred hand-off (`ship --defer-handoff`): in_review, assign the reviewer, clear the record |
| `review <repo> <pr> [--open] [--raw] [--issue N]` | reviews + inline threads, marks answered / resolved (GitHub's thread state); bot boilerplate folded to one line each, findings kept in full (`--raw` shows everything). `--issue N` adds the issue's Gaia comments from people since the agent's last comment and the PR's CI, and ends with `NEEDS ADDRESSING: ...` or `NOTHING TO ADDRESS` |
| `design-review <repo>` | runs the `design-reviewer` agent headless (`claude -p`, read-only tools, the change against `origin/<base>` on stdin) and records its verdict for this exact tree, like `ai test` (`state/design-review-<repo>.json`). Clean = no `blocker` or `should-fix` finding and a readable report (nits are listed, don't block); a tree changed during the review is recorded stale. Exit 0 clean, 1 not. Run it once on the final tree, after the full `ai test`: it is advisory, not a merge condition |
| `mergecheck <repo> <pr> [--json]` / `merge <repo> <pr>` | the merge gate for an agent PR's head commit (ok/fail/pending, exit 0/1/2), then (`merge`) approve and merge as Agent Agamemnon. Checks: open, CI, recorded tests, mergeable, people, labels, `self` (`agamemnonProtected`). The design review is not a check (adt-5046). `tests` (`RESULT: PASS`) trusts the body `ai ship` wrote: an edited body isn't detected |
| `reply <repo> <pr> replies.json` | replies to many threads in one call (`{"<comment id>": "<reply>"}`) |
| `pr-body <repo> <pr> <file.md>` | replace a PR description |
| `api-cases client\|acl <cases.json>` | append api-tester cases in the catalog's own style (no re-serialising), duplicate-id check |
| `gaia-dev <args>` | an allowlisted Gaia `gaia-dev` command on **this workspace's** gaia checkout (see below) |
| `call METHOD <path> [json] [--target test\|dev] [--profile aclNoSetting] [--jq data.0]` | log in and call the API (test API as an api-tester profile, dev as Agent Odysseus), truncated output |

`ai gaia-dev` runs Gaia's `php tools/gaia-dev/bin/gaia-dev` on this workspace's gaia checkout (`P4M_WORKSPACE`; refused from a `.runs` folder when it doesn't match, like `ai test`), so headless runs regenerate `docs/generated` and `graph.json` instead of blocking. Allowlist: `graph export [--check]`, `docs generate [--check]`, `check api-cases [<endpoint>]`, `check resource <Name>`, `check action`, `check behavior [--skip-tests]`, `check live-events`, the file-only scaffolders (`scaffold resource <Name>`, `scaffold action <Model> <action>`, `scaffold behavior <name>`, `scaffold live-events <Name>`, `scaffold api-cases [<METHOD> <endpoint>]`, each with its documented `--opt=value` options; values are restricted to word characters plus `: = , . -` (and `[ ] "` for `--assert`, spaces for `--role`); `--cases-file` is refused) and the queries `model`, `route`, `acl`, `cases [--method=…] [--profile=…] [--all]`, `assertions`, `profiles`, `fixtures`; anything else (`migration …`, `check behavior` without `--skip-tests`, shell metacharacters) is refused. The pack's "checks to run before shipping" lines use it for allowlisted checks.

Every `gaia-dev` call from `ai` (this command, `ai context` and the `ai ship` gate) uses local php only when the checkout has `vendor/autoload.php`; else (a Talos run worktree, whose `vendor/` is empty) a throwaway `gaia-gaia` container (`--entrypoint php`) with the checkout mounted read-write at `/src` and the `gaia_vendor` volume read-only at `/src/vendor`, `--network none`, as the caller's uid:gid on POSIX so the files it writes stay theirs. It writes to that checkout, never to the shared gaia-dev container, another checkout or the shared packages. A stale drift check in the `ai ship` gate names the `ai gaia-dev` command that regenerates it.

**Base moved while the work is uncommitted:** `ai ship` and `ai commit` first fetch; a branch with no commits of its own is stashed (`ai-ship-<issue>`), fast-forwarded to `origin/<base>` and the stash re-applied (`lib/branchsync.py`). A clean pop stops `ai ship` with "the tree changed: run the full `ai test` again" (re-test, then ship). A conflicting pop leaves the conflict in the tree and fails with `merge conflict after updating to origin/<base>: <files>` (the stash is kept). Intent-to-add files (`git add -N`), which `git stash` can't take, are set back to untracked for the update and marked intent-to-add again after it; one the base also adds fails the fast-forward with git's message, the file left as it was. A branch with commits of its own keeps the old refusal (merge the base, don't rebase). Never commit under another identity to work around it.

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
| skill | `/address-review <repo> <pr> <issue>` | `ai review --open --issue N` (threads, Gaia comments, CI), changes, re-test, follow-up commit, `ai reply`, `ai pr-body`, tracker |
| agent | `test-runner` (Haiku) | runs `ai test`, returns pass/fail + unexpected failures only |
| agent | `design-reviewer` (Sonnet) | reviews the diff against the `### Design` rules (Fowler layering, SOLID) and Gaia review rules |

Repos: `gaia`, `prometheus`, `hermes`, `ares` (base `dev`), `dev-tools` and `pr4m` (the workspace root; base `main`).

## Typical issue, end to end

```
/start-issue 3960                      # ai start gaia 3960, ai context 3960, checks
… implement; ai test gaia --changed while fixing …
/ship-issue 3960                       # test-runner (full ai test), design-reviewer, ai ship (after approval)
/address-review gaia 180 3960          # after the reviewer comments
dev-tools/ai/ai sync --all              # every repo's dev to origin, branches untouched (merge and Talos do this too)
```

## Running tests safely

- `ai test` and `ai ship` take the checkout from `P4M_WORKSPACE`, not the cwd. From inside a `.runs/issue-N` folder
  they refuse to run when it is unset or points elsewhere (the error names the value to set), and they print the
  resolved checkout path and branch first.
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
