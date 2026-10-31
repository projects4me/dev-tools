# Agent Agamemnon: the orchestrator's GitHub identity

Agent Agamemnon (`agent-agamemnon` on GitHub, `agent.agamemnon@projects4.me` in Projects4Me) feeds issues to Agent
Odysseus (see `talos/README.md`, Orchestrator agents) and approves and merges agent PRs. It never writes code: the
agent that opens a PR (Odysseus) is never the one that accepts it, and `dev` requires an approving review, which
GitHub doesn't let a PR's author give.

| Piece | Where |
|---|---|
| GitHub account | `agent-agamemnon`, maintainer on the repos |
| GitHub API token | `agamemnon.env` (git-ignored): `GH_TOKEN=` (template: `agamemnon.env.example`) |
| Wrapper | `./agamemnon gh …` (there is no `git`: Agamemnon never commits or pushes) |
| Guarded tool | `./aga <verb>`: the only way an Agamemnon AI session acts (see `talos/README.md`, The `aga` tool) |
| Projects4Me user | Agent Agamemnon, `agent.agamemnon@projects4.me`; credentials in `gaia-mcp/agamemnon.env` |

`p4m configure` doesn't ask for this token: copy `agamemnon.env.example` to `agamemnon.env` and paste it in.
`dev-tools/talos/talos check --agent agamemnon` confirms the token and its write access to the worker's repos.

## Usage

```bash
dev-tools/agamemnon/agamemnon gh api user --jq .login             # agent-agamemnon
dev-tools/agamemnon/agamemnon gh pr review 12 --approve --repo Projects4Me/gaia
```

## Creating the GitHub token (one time, by the team lead)

Signed in to GitHub **as agent-agamemnon**:

1. **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token.**
2. **Token name:** `Agamemnon Talos`. **Expiration:** 90 days (add a reminder to rotate).
3. **Resource owner:** `Projects4Me` (the organization). If the org requires approval, approve the request under
   *Projects4Me org → Settings → Personal access tokens → Pending requests*.
4. **Repository access:** *Only select repositories*: `gaia`, `prometheus`, `hermes`, `ares`, `dev-tools`.
5. **Repository permissions:**

   | Permission | Access | Why |
   |---|---|---|
   | Contents | Read and write | merge PRs, update a branch that is behind its base |
   | Pull requests | Read and write | approve and merge PRs |
   | Checks | Read-only | read CI and CodeRabbit check runs |
   | Commit statuses | Read-only | read commit statuses |
   | Metadata | Read-only | added automatically |

   Leave everything else at *No access*.
6. **Generate**, copy the token, and put it in `dev-tools/agamemnon/agamemnon.env` as `GH_TOKEN=…`.
   Don't paste it into chat.

**Rotation / revocation:** generate a new token and replace the line; revoking it stops Agamemnon from approving
and merging without touching anyone else's credentials.
