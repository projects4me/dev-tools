# Agent Odysseus: the AI agent's identity

Claude Code commits, pushes and opens PRs as **Agent Odysseus** (`agent-odysseus` on GitHub,
`agent.odysseus@projects4.me`), the team's shared agent identity, never as the engineer running it. Nobody's own
git and `gh` setup is changed.

**Setup for an engineer:** `dev-tools/installer/p4m configure` asks for the agent's GitHub token and SSH key (shared
by the team lead over a secure channel), installs the key as `~/.ssh/agent_odysseus`, adds the `github-odysseus`
host alias and the `odysseus` remote in every repo, and writes `odysseus.env`.

| Piece | Where |
|---|---|
| SSH key (auth + commit signing) | `~/.ssh/agent_odysseus` (public key added to agent-odysseus twice: authentication and signing) |
| SSH host alias | `~/.ssh/config`: `Host github-odysseus` |
| Git remote per repo | `odysseus` → `git@github-odysseus:Projects4Me/<repo>.git` (push only here, never `origin`) |
| GitHub API token | `odysseus.env` (git-ignored): `GH_TOKEN=` (template: `odysseus.env.example`) |
| Wrapper | `./odysseus git …` and `./odysseus gh …` |
| Projects4Me user | Agent Odysseus, `agent.odysseus@projects4.me` (Coding Agent role) |

## Usage

```bash
dev-tools/odysseus/odysseus git commit -m "..."   # authored + signed as the agent
dev-tools/odysseus/odysseus git push -u odysseus claude-issue-NNNN
dev-tools/odysseus/odysseus gh pr create --repo Projects4Me/<repo> --base dev ...
```

## Creating the GitHub token (one time, by the team lead)

Signed in to GitHub **as agent-odysseus**:

1. **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token.**
2. **Token name:** `claude-code-projects4me`. **Expiration:** 90 days (add a reminder to rotate).
3. **Resource owner:** `Projects4Me` (the organization, not the personal account).
   If the organization isn't offered, an org owner enables it first: *Projects4Me org → Settings →
   Personal access tokens → Settings → Allow access via fine-grained personal access tokens*.
   If the org requires approval, approve the request under *Personal access tokens → Pending requests*.
4. **Repository access:** *Only select repositories*: `gaia`, `prometheus`, `hermes`, `ares`, `dev-tools`, `Pr4M`.
5. **Repository permissions:**

   | Permission | Access | Why |
   |---|---|---|
   | Pull requests | Read and write | open PRs, reply to review comments |
   | Contents | Read-only | read branches and files for PRs (pushes use SSH, not the token) |
   | Commit statuses | Read-only | see CI results on PRs |
   | Actions | Read-only | read CI run logs when a check fails |
   | Metadata | Read-only | added automatically |

   Leave everything else at *No access* (no Administration, no Workflows, no Secrets).
6. **Generate**, copy the token, and paste it into `dev-tools/odysseus/odysseus.env` as `GH_TOKEN=…`.
   Don't paste it into chat.
7. Claude verifies with `./odysseus gh api user` (should print `agent-odysseus`).

**Rotation / revocation:** generate a new token and replace the line; revoking the token (or removing the
account from the repos) cuts the agent off without touching anyone's own credentials.
