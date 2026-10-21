# Nestor: the UI for brainstorming ideas with Agent Nestor

Only the UI. You describe what you want to build; **Agent Nestor**, an agent in **Talos**, reads the code and the
existing issues, asks what matters, and proposes an implementation plan split into issues. When you approve a
version, Talos creates those issues in Gaia as Agent Nestor, assigned to you, in dependency order.

The flow is the one Prometheus, Gaia and Hermes will run once they support ideas; the parts that don't exist yet
are local stand-ins in [`../sim`](../sim/README.md):

```
 Nestor UI ──REST──▶ sim Gaia (idea, ideamessage, ideaplan, ideaissue) ──publish──▶ sim Hermes
   ▲  (later: Prometheus)   (later: Gaia)                                      (later: Hermes)   │
   │                                                                                             ▼
   └──── domain events + live progress (SSE) ◀── sim Hermes ◀── replies, plans, progress ── Talos (agents/nestor)
                                                                                     │ claude -p in the workspace
                                                                                     └─ gaia MCP ─▶ Gaia (issues)
```

## Run

Three processes (each until Ctrl+C):

```bash
dev-tools/sim/sim serve            # sim Gaia + sim Hermes on 127.0.0.1:4791
dev-tools/talos/talos run          # Talos: Agent Odysseus on Hermes, Agent Nestor on sim Hermes
dev-tools/nestor/nestor serve --open   # the UI on 127.0.0.1:4790 (Windows: nestor.cmd)
python -m unittest discover -s dev-tools/nestor/tests
```

The sidebar shows whether Agent Nestor is listening ("Offline (start Talos)" when Talos isn't running). Messages
sent while Talos is down are answered when it starts (it reconciles on connect).

**Setup:** `config.local.json` (git-ignored) needs `agentUserId`, Agent Nestor's Gaia user id
(`dev-tools/talos/talos agents --agent nestor` shows it). You are the engineer from `workspace.local.json`
(`p4m configure`), or set `engineerUserId` and `engineerName`. Agent Nestor's own setup is in Talos
(`talos/agents/nestor`, its Gaia login in `gaia-mcp/nestor.env`).

## What the UI does

- Reads and writes the idea resources (`/sim/api/v1/...`) and listens to the domain events (`/sim/hermes/events`,
  user-scoped intents for you), the way a Prometheus page uses Gaia and Hermes. Its server only serves the page
  and forwards `/sim/*` as you (Prometheus' OAuth token will play that part); it forwards nothing else, refuses
  other `Host` headers and needs `X-Nestor: 1` on writes.
- Ask Nestor for a UI screen and its PNG (rendered by Talos in the Projects4Me look) appears under his reply;
  click it for full size. Each plan issue shows its milestone (Claude-v1 unless you asked for another) and the
  screens that get attached to it.
- Sending a message creates an `ideamessage`; Stop sets the idea's `agent_state` to `stopping`; Approve sets the
  plan's status to `approved`. Talos hears each as an event and acts. Nestor's text and steps appear live from
  the `idea.agent.progress` events Talos publishes.

## Look

Prometheus' design system (`prometheus/docs/ui/design-system.md`): AdminLTE 2 layout with the `skin-black-light`
sidebar, the crimson palette from `variables.scss`, boxes with the gradient top bar, square edges, Source Sans Pro
and Font Awesome 4.7 (the same files Prometheus serves). An idea uses the detail-page pattern: the conversation in
the main column, Plan, Created Issues and Summary boxes beside it.

| file | responsibility |
|---|---|
| `nestor.py` | CLI: `serve` |
| `lib/server.py` | static files, `/api/info`, `/sim/*` forwarded as the engineer, request guards |
| `lib/config.py` | settings: the stand-ins' URL, the engineer, the agent |
| `web/` | the page (`index.html`, `app.js`, `app.css`, `img/logo.png` from Prometheus; `vendor/`: marked 12.0.2, DOMPurify 3.1.6, Font Awesome 4.7.0, unmodified) |
