# Cutting Claude Code token cost (Projects4Me)

Written 2026-10-15 (agaia-3975) from a review of my own steps over the last sessions plus the current
Claude Code docs. Sources at the end.

## 1. Where my tokens went (self-review)

| Pattern | Example from these sessions | Fix |
|---|---|---|
| **Workflow overhead as separate calls** | Each issue: fetch / checkout / branch / status / timer at the start; base check / add / commit / push / PR / time / comment / status+assign / memory at the end. About 15–20 calls per issue, each re-sending the whole conversation. | `dev-tools/ai` commands and the `/start-issue`, `/ship-issue`, `/address-review` skills: about 4 calls per issue. |
| **One very long session** | Five issues and two review rounds in one conversation, so every turn re-sent everything before it. | `/clear` after each PR hand-off (the workspace CLAUDE.md now says so). Memory and the tracker carry the state. |
| **Huge tool output** | `grep` over `public/index.php` printed the 2 × 3 KB CORS header lines, twice. 200–300-line `sed` ranges of `RestController`. Raw `gaia_get_issue` JSON with HTML. Full api-tester reports parsed ad hoc. | `ai test` prints about 6 lines (logs saved to files); `ai issue` prints plain text; use `grep -c` / `-o` / `cut -c1-200` and narrow line ranges. |
| **Avoidable retries** | Heredocs eating PHP backslashes (3 times), a `cat > file` that waited on stdin until timeout, a route-debug script rebuilt 4 times. | Write scripts with the Write tool, never heredocs with `\`; use `ai call` for API probes. |
| **Repeated boilerplate** | Token-fetch plus curl blocks; Python one-offs to append api-tester cases (5 times); reply loops for PR comments. | `ai call`, `ai api-cases`, `ai reply`. |
| **Frequent small memory edits** | The current-state memory was rewritten after nearly every step. | Update memory once per issue (at hand-off), not per step. |
| **Long deliberation** | Weighing options I didn't take (e.g. three reorder designs) before asking. | Recommend one option, ask once. |
| **Full test suite rerun several times per issue** | 3–4 runs of all three api-tester modes per issue, output in context. | Run `ai test` through the `test-runner` agent (Haiku); only the verdict returns. |

## 2. Recommendations, highest savings first

1. **`/clear` between issues.** It costs nothing and resets the history that every turn re-sends. Use `/compact keep <what matters>` only at natural breaks, and `/rewind` instead of arguing back from a wrong path.
2. **Measure first:** `/context` (what fills the window), `/usage` (session cost, prompt-cache hit rate and the cause of the last miss, breakdown by skill/subagent/MCP), `/doctor` (skills that cost context but are never used).
3. **Use the scripts and agents above.** Fewer calls per issue, and verbose output (tests, logs) stays in subagents or files.
4. **Switch off unused claude.ai connectors.** Asana, Atlassian, Box, Canva, Figma, Gmail, Google Calendar/Drive, HubSpot, Intercom, Linear, Notion, Slack and monday.com are listed every session and none is used here. Setting: `"disableClaudeAiConnectors": true` (or `ENABLE_CLAUDEAI_MCP_SERVERS=false`), or deny individual ones with `deniedMcpServers` / toggle them in `/mcp`. Keep tool search on (don't set `ENABLE_TOOL_SEARCH=false`).
5. **Match model and effort to the job.**
   - Sonnet as the default for routine issues, Opus for design-heavy ones (`/model`). Pick at session start and don't switch mid-session: switching model or effort, or turning on fast mode, breaks the prompt cache.
   - Subagents get their own model in frontmatter (`model: haiku` for `test-runner`). `CLAUDE_CODE_SUBAGENT_MODEL` sets a default.
   - Lower `/effort` for mechanical work. Thinking tokens bill as output.
   - Leave fast mode off when cost matters (premium rate, and the first fast turn misses the cache).
6. **Trim output at the source.**
   - Lower `bashOutputMaxChars` / `BASH_MAX_OUTPUT_LENGTH` (default 30 000 chars) to around 10 000.
   - Optionally add a `PreToolUse` hook on Bash that pipes test commands through a failures-only filter (`ai test` already does this).
   - Keep `MAX_MCP_OUTPUT_TOKENS` at its default or lower.
7. **Keep CLAUDE.md lean and move procedures into skills (done 2026-10-15: skills `p4m-stack`, `run-tests`; workspace CLAUDE.md 4.6 KB → 2.1 KB).** CLAUDE.md and the first 200 lines of MEMORY.md load into every session and every subagent; a skill's body loads only when used. Candidates to move: the "Running tests" table and "Tooling gotchas" in the workspace CLAUDE.md → a `/p4m-recipes` skill. Path-scoped rules in `.claude/rules/` (with `paths:` frontmatter) load only when matching files are touched.
8. **Subagents only for verbose, self-contained work** (tests, log digging, broad searches). For a quick git step or a small edit they cost more: a fresh context, plus CLAUDE.md re-read.
9. **A permissions allowlist** for the safe repeated commands (`dev-tools/ai/ai *`, `dev-tools/installer/p4m test *`, read-only git) cuts prompt round-trips. The `/fewer-permission-prompts` skill can generate it from transcripts.
10. **Headless for scripted steps.** `claude -p "…" --max-budget-usd 1` for one-shot jobs such as "run tests and summarise" from a shell or CI.

## 3. Decisions (2026-10-15)

- Done: claude.ai connectors disabled (Hammad); CLAUDE.md procedures moved into skills.
- **Rejected: lowering the Bash output cap and the permissions allowlist (items 6 and 9).** Accuracy comes first: no truncated output, and every command keeps its confirmation.

## 4. Originally pending your OK

Items 4, 6, 9 and the CLAUDE.md move in 7 change your Claude Code settings or house-rule files. Say which ones you want and I'll apply them.

## Sources

- https://code.claude.com/docs/en/costs
- https://code.claude.com/docs/en/prompt-caching
- https://code.claude.com/docs/en/context-window
- https://code.claude.com/docs/en/mcp
- https://code.claude.com/docs/en/sub-agents
- https://code.claude.com/docs/en/skills
- https://code.claude.com/docs/en/hooks
- https://code.claude.com/docs/en/env-vars
- https://composio.dev/blog/ways-to-cut-token-consumption-in-claude-code
- https://www.analyticsvidhya.com/blog/2026/05/tips-for-claude-code-token-saving/
- https://aident.ai/blog/reduce-mcp-token-usage-claude-code-codex
