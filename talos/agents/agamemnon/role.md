# Agent Agamemnon: what it may and may not do

Agamemnon is an orchestrator: Talos runs it as code (`talos/lib/orchestrator`), not as a Claude prompt. This file
records its role.

## Never auto-merge changes to its own powers
- The merge gate (`ai mergecheck`) has a `self` check. It fails when a PR changes a path in `agamemnonProtected`
  (`ai/config.json`, the one list): this agent's folder, `talos/lib/orchestrator/**`, `ai/lib/mergegate.py`
  and the `agamemnon/**` tool wrapper.
- A failed gate hands the issue to the owner (in_review) with the reason. Agamemnon never approves such a PR.

## Tools: propose, never build
- Agamemnon may only propose a new tool. It creates an issue in its milestone describing the repeated gap.
- It never writes or installs tools itself.
