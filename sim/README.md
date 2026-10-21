# sim: stand-ins for Gaia's idea resources and Hermes' idea events

**Temporary.** Gaia has no ideas yet and Hermes carries no idea events, so this local process plays both parts
for the Nestor flow (Nestor UI ↔ Talos). Delete it when Gaia and Hermes support ideas; the UI then talks to Gaia
and Hermes, and Talos listens to Hermes for its ideas agent as it already does for Odysseus.

```bash
dev-tools/sim/sim serve                 # http://127.0.0.1:4791 until Ctrl+C (Windows: sim.cmd serve)
dev-tools/sim/sim serve --import-nestor 1 <agent id>   # first run only: ideas from the first Nestor build
python -m unittest discover -s dev-tools/sim/tests
```

## Sim Gaia: idea resources (state/sim.db)

CRUD resources, one per future Gaia model. The caller is the Gaia user id in `X-Sim-Actor` (Gaia would take it
from the OAuth token; this is local only, on 127.0.0.1). An idea has an **owner** (the engineer) and an **agent**
(the Gaia user an agent runs as, e.g. Agent Nestor); each side may only make its own changes:

| resource | owner (engineer) | agent |
|---|---|---|
| `idea` | create (with `agent`), rename, archive, `agent_state: stopping` | `agent_state: idle/queued/working`, `status: created` |
| `ideamessage` | `role: user` | `role: agent/system/error` (with `steps`, `plan_id`, `cost_usd`) |
| `ideaplan` | `status: approved` (latest version, from proposed/failed) | create (supersedes the proposed one); `creating`, `failed`, `created` |
| `ideaissue` | | record an issue created in Gaia |
| `ideascreen` | view the PNG (`GET /api/v1/ideascreen/<id>`) | add a rendered UI screen (PNG); the same key again is its next version |

Every change publishes a domain event to both participants (user-scoped `projectId: "user:<id>"`, like
`notification.created`): `idea.created`, `idea.updated`, `idea.message.created`, `idea.plan.created`,
`idea.plan.status.changed`, `idea.issue.created`, `idea.screen.created`. When Gaia gets these, they go in `EventNames.php` and a
LiveEvents component.

## Sim Hermes: the same contract as hermes/

- `POST /publish` with `X-Hermes-Secret`: the envelope (`schemaVersion` 1, `eventId`, `eventName` on the
  allowlist, `occurredAt`, `projectId`, `resource {type, id}`, `actorId`, `changes`, `meta`), validated like
  `hermes/src/contract/envelope.js`. Talos publishes `idea.agent.progress` (live text and steps) here; real
  Hermes has no path for agents to publish yet.
- `GET /hermes/events?intents=[{projectId, eventName}]` (SSE): `ready` with accepted/rejected intents (own user
  only, allowlisted, no duplicates, as `intents:set`), then `domain:event` envelopes. Like Hermes it keeps no
  history: subscribers reconcile after (re)connecting.
- `GET /hermes/presence?user=<id>`: whether anyone listens as that user (the UI shows whether Talos is running).

`config.json`: `host`, `port` (4791), `secret`; override in `config.local.json` (git-ignored).
