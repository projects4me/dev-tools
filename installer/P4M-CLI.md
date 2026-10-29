# p4m — Projects4Me dev CLI

One command-line tool to set up, run and manage the Projects4Me stack from this folder.
Pure Python standard library — nothing to `pip install`.

Everything is served over **HTTPS** with real hostnames under one configurable domain:

| URL | Service |
|---|---|
| https://app.local-projects4.me | prometheus (frontend) |
| https://api.local-projects4.me | gaia (API) |
| https://hermes.local-projects4.me | hermes (Socket.IO, `wss://`) |
| https://ares.local-projects4.me/docs | ares (AI service) |
| https://api-test.local-projects4.me | gaia-test (pr4m_test) |
| https://api-test-hermes-down.local-projects4.me | gaia-test-hermes-down |
| https://hermes-test.local-projects4.me | hermes-test |
| https://xhgui.local-projects4.me | xhgui profiler |
| `localhost:5432` | postgres (user/pass `nouman`/`nouman`, DBs `pr4m`, `pr4m_test`) |

The domain is the `P4M_DOMAIN` setting in `p4m.env`. Change it and every URL follows.

## Layout

```
Projects4Me\
├─ ares\  docker-images\  gaia\  hermes\  prometheus\   ← the repos (untouched)
├─ .dev-tools-runtime\         ← worktree of dev-tools' origin/main the tools run from (`p4m runtime`)
└─ dev-tools\
   ├─ installer\                ← p4m itself
   │  ├─ p4m.cmd / p4m / p4m.py    the CLI (Windows / bash launchers + code)
   │  ├─ configure.py              `p4m configure`: per-engineer settings and credentials
   │  ├─ runtime.py                `p4m runtime`: the runtime copy of dev-tools
   │  └─ P4M-CLI.md                this doc
   ├─ p4m.env                   your settings (created on first run)
   ├─ .p4m\                     generated proxy config, compose overrides, certs
   └─ logs\                     `p4m diag` reports
```

Run `installer/p4m` (or `installer\p4m.cmd` on Windows) from `dev-tools`, or by its full path from anywhere. The repos are found two folders above the script, in the workspace root.

## First run (Windows)

```powershell
cd D:\Claude\Workspace\Projects\Projects4Me\dev-tools
.\p4m setup
```

`p4m.cmd` installs Python 3 with winget if it's missing (then asks you to open a new terminal).
`setup` then:

1. Installs **Git** and **Docker Desktop** with winget if missing (WSL 2 must be enabled — `p4m doctor` tells you).
2. Clones/updates all repos and checks out the expected branch (`dev`; `docker-images` stays on `master`).
3. Creates `p4m.env` (your settings) and `.p4m/` (generated config).
4. Starts the HTTPS proxy, adds its local CA to your Windows trusted roots (**Windows asks you to confirm**),
   and adds the hostnames to the hosts file (**admin prompt**).
5. Pulls and builds images.
6. Installs prometheus `node_modules` into a Docker volume.
7. Starts Postgres (imports `gaia/db/Dump20260810.sql` on first start), runs gaia migrations, seeds `pr4m_test`.
8. Starts everything and prints a health table.

Already set up? Run `.\p4m trust` and `.\p4m hosts` once, then `.\p4m up`.

Put your key in `p4m.env` → `ANTHROPIC_API_KEY=...` to enable ares.
Firefox uses its own certificate store: set `security.enterprise_roots.enabled = true` in `about:config`.

## Everyday commands

| Command | What it does |
|---|---|
| `p4m up [svc…]` | Start services (default: all). `--build` to rebuild first |
| `p4m down [svc…]` | Stop services; no args = whole stack. `-v` also deletes volumes (DB!) — never the CA |
| `p4m restart <svc…>` | Restart services |
| `p4m status` | State, health and an HTTPS check for each service |
| `p4m urls` | URL map, plus the URL env vars passed to each service |
| `p4m open [svc]` | Open the app (or a service) in the browser |
| `p4m logs <svc> -f` | Follow container logs (`p4m logs proxy` for routing/TLS) |
| `p4m shell <svc> [cmd…]` | Shell or one-off command, e.g. `p4m shell gaia composer install` |
| `p4m build [svc…]` | Rebuild images (`--pull` for newer bases, `--deps` to reinstall node_modules) |
| `p4m hosts` | Add/refresh the `*.P4M_DOMAIN` block in the hosts file (`--show` to print it) |
| `p4m trust` | Trust the local CA in Windows (or macOS) |
| `p4m db migrate` | `php cli.php migration migrate` for the workspace gaia (in gaia-dev if it mounts this checkout, else a throwaway container); refused in a Talos run workspace (`P4M_WORKSPACE` set) |
| `p4m db prepare-test` | Create + seed `pr4m_test` for the api-tester |
| `p4m db psql [db]` / `dump [db] -o f.sql` | psql shell / dump |
| `p4m db reset [--with-test]` | Wipe the DB volume, re-import the dump, migrate |
| `p4m test api --mode backend,acl` | gaia api-tester against gaia-test (`--filter`, `--mode live`) |
| `p4m test phpunit [path]` / `test hermes` | Unit tests |
| `p4m test ember [--filter X] [--fresh]` | Prometheus tests in a test container: source copied in, incremental `ember build -e test --watch`, `ember test --path` (reruns in seconds) |
| `p4m test slots setup\|status\|reset [K…]` | Gaia test slots: `setup` (re)builds the template database if stale (`--force`: always) and clones it into the slots' databases, `reset` only re-clones it; `status` shows the template, each slot's database, container, checkout and lock (`--src`, `--force`) |
| `p4m test api\|phpunit --slot K` | Run in test slot K: container `p4m-test-gaia-slotK` on database `pr4m_test_K`; `api --prepare` resets the slot from the template instead of re-seeding |
| `p4m test clean` | remove the throwaway `p4m-test-*` containers (with `--src`, only that checkout's; without, the slot containers too) |

Every `p4m test` runs against **this workspace's checkout** (or `--src <path>`). The shared containers serve the
folder `p4m up` ran from; when that is another folder, a throwaway `p4m-test-<service>-<hash>` container is
started from the same image, env, network and mounts with the source pointed here, and kept for reuse.

Gaia suites share the pr4m_test database, so without `--slot` they run one at a time (a lock serialises them).
**Test slots** let several run at once: slot K (1..`TEST_SLOTS`) owns database `pr4m_test_K` and container
`p4m-test-gaia-slotK`, and has its own lock. The slot databases are cloned (`CREATE DATABASE … TEMPLATE`, seconds)
from a template the api-tester harness seeds once per key: the checkout's model metadata (`app/metadata`) and seeds
(`db/seeds`, the harness script). The schema is the checkout's, not pr4m's: a build dumps pr4m into a scratch
database `pr4m_src_<key>`, migrates it with the checkout's code in the slot's container, seeds the template from it
(`SOURCE_DB`) and drops it, so branches that add different models test in parallel and pr4m is never migrated.
The key tracks only metadata and seeds: after a change to the migration code itself, rebuild with
`p4m test slots setup <K> --force`.
Each key has its own template, `pr4m_test_tpl_<key>`, so a branch and its baseline
with different seeds each reuse theirs; at most `TEST_TEMPLATES` are kept and building one more drops the least
recently used. A slot takes its lock, then the templates' lock. A slot serves any checkout: one synced into a slot
that last held another is copied in from scratch. `ai test gaia --slot K` runs the gaia suites in slot K.
`phpunit --slot K` needs the slot's database (`p4m test slots setup K`). `p4m test slots status` lists the
templates with their keys.
| `p4m repos status` / `sync` / `fix-eol` | Git state; clone + checkout + pull; CRLF→LF for shell scripts |
| `p4m doctor` | Check tools, WSL, Docker, ports 80/443/5432, hosts file, CA trust, repos |
| `p4m watch start\|stop\|status` | File sync of the prometheus source into its container (started by `p4m up`) |
| `p4m diag [svc…]` | Write a full diagnostics report (status, probes, logs) to `dev-tools/logs/` |
| `p4m runtime setup\|update\|status` | The runtime copy of dev-tools (`.dev-tools-runtime`, a worktree of `origin/main`) that Talos and the agents' tooling run from; `update` fast-forwards it, refusing while a Talos run is active (`--force` for a stale one). See the Talos README |

Service names: `proxy postgres gaia hermes ares prometheus gaia-test gaia-test-hermes-down hermes-test xhgui`.
Groups: `all`, `core`, `test`, `tools`, or a repo name (`gaia` = all gaia services).

## How the URLs are wired

Each service already reads the other services' URLs from environment variables. p4m sets them all
from `P4M_DOMAIN`, and `p4m urls` prints the current values:

| Service | Variable | Value |
|---|---|---|
| prometheus | `API_HOST_DEV` | `https://api.<domain>` |
| prometheus | `HERMES_URL_DEV` | `https://hermes.<domain>` |
| gaia | `HERMES_URL` | `https://hermes.<domain>` (gaia-test: `hermes-test.`) |
| gaia | `LLM_SERVICE_URL` | `https://ares.<domain>` |
| gaia | `FRONTEND_HOST` | `app.<domain>` (links in emails) |
| hermes | `GAIA_URL` | `https://api.<domain>` (hermes-test: `api-test.`) |
| hermes | `CORS_ORIGIN` | `https://app.<domain>` |

A cloud environment sets the same variables to its own hostnames, for example `https://api.projects4.me`.

**Same URLs inside and outside Docker.** Your browser reaches `*.<domain>` through the hosts file
(→ 127.0.0.1 → proxy on ports 80/443). Containers reach the same names because the proxy holds Docker
network aliases for every hostname on the `backend` network. So gaia calls hermes at
`https://hermes.<domain>`, exactly like the browser does. No `localhost:<port>` or
`host.docker.internal` URLs remain.

**HTTPS everywhere.** Caddy issues certificates from its own local CA (stored in the `p4m_caddy_data`
volume). HTTP requests are redirected to HTTPS. Containers trust that CA as follows:

- gaia: a CA bundle mounted over `/etc/ssl/certs/ca-certificates.crt`
- hermes: `NODE_EXTRA_CA_CERTS`

Windows trusts it after `p4m trust`. The app services no longer publish ports of their own. Only
the proxy (80/443), postgres (5432) and ares' debugger (5678) are reachable from Windows.

**Prometheus source is synced, not mounted.** A Windows-drive bind mount made the first ember build
take 15+ minutes. Now the source lives inside the container, and `p4m up` starts
`docker compose watch` in the background, which copies every saved file in within about a second
(log: `dev-tools/logs/watch-prometheus.log`). `p4m status` shows `[file sync: on]`. If edits don't show up, run
`p4m watch start`. After changing `package.json`, run `p4m up prometheus`, which reinstalls dependencies.

**Live reload.** ember's live-reload server runs on port 443 inside the prometheus container, and the
proxy forwards `https://app.<domain>/_lr/*` to it. That way the reload snippet ember injects into
the page works through the proxy.

## Settings — `p4m.env`

| Key | Default | Purpose |
|---|---|---|
| `P4M_DOMAIN` | local-projects4.me | Base domain for every URL. After changing it, run `p4m hosts` then `p4m up` |
| `ANTHROPIC_API_KEY` | empty | ares AI calls |
| `SETTINGS_ENCRYPTION_KEY` | generated | Encrypts Gaia's secure settings (e.g. the SMTP password). A random key per machine, added on first use; never commit it, and don't change it (settings saved with it become unreadable). Recreate gaia after it's added: `p4m up gaia` |
| `GAIA_XDEBUG_PORT` | 9003 | Xdebug client port (repo uses 9000 = hermes) |
| `GAIA_XDEBUG_MODE` | debug,develop | Set to `off` for faster requests |
| `FILE_WATCH_POLLING` | auto | Polling watchers for ember/nodemon/uvicorn (needed on Windows bind mounts) |
| `DEFAULT_SERVICES` | all | What `p4m up` starts with no args (`all` or `core`) |
| `TEST_SLOTS` | 2 | Number of Gaia test slots (`p4m test slots`, `--slot K`), 1–16 |
| `TEST_TEMPLATES` | 3 | Slot templates (one per metadata + seeds) to keep, 1–16; the least recently used is dropped |

## Troubleshooting

- **Page keeps loading:** `ember serve` holds requests until its build finishes. Check
  `p4m logs prometheus -f` for `Build successful` / `Serving on`.
- **Anything else:** run `p4m diag`, then share the newest file in `dev-tools/logs/`.
- `http://localhost:4200` / `:8080` / `:9000` are intentionally gone. Use the `https://*.<domain>` URLs.

## What p4m generates (`dev-tools/.p4m/`) and what it leaves alone

- `.p4m/proxy/`: Caddyfile and compose project for the proxy.
- `.p4m/<repo>.override.yml`: layered on each repo's own compose file. These set the URL env vars,
  remove direct ports, attach services to `backend`, mount the CA, and work around the issues in
  `ISSUES.md`.
- `.p4m/certs/`: the exported root CA and the CA bundle.

The repos themselves are **not modified**. The only changes p4m makes inside them are a repo-local
`git config core.autocrlf input`, and CRLF→LF in `*.sh` files that have no other edits.
