#!/usr/bin/env python3
"""
p4m - Projects4Me local development CLI.

Sets up and runs the Projects4Me stack (gaia, hermes, prometheus, ares + postgres)
from the folder this file lives in. Standard library only - no pip install needed.
Everything is served over HTTPS by a Caddy proxy with a local CA:
https://app.<P4M_DOMAIN>, https://api.<P4M_DOMAIN>, https://hermes.<P4M_DOMAIN>, ...

    p4m doctor            check prerequisites, repos, ports, hosts file, CA, docker
    p4m install           install missing prerequisites (Windows: winget)
    p4m setup             first-time setup: prereqs, repos, proxy+CA+hosts, images, deps, DB, start
    p4m up [svc...]       start services (default: everything)
    p4m down [svc...]     stop services
    p4m status            container state + HTTPS health per service
    p4m urls              URL map and the URL env vars passed to each service
    p4m hosts | trust     hosts-file entries (admin prompt) | trust the local CA
    p4m logs <svc> [-f]   container logs
    p4m shell <svc>       interactive shell inside a container
    p4m db ...            migrate | prepare-test | reset | psql | dump
    p4m test ...          api | phpunit | hermes
    p4m repos ...         status | sync | fix-eol
    p4m open [svc]        open service URLs in the browser

    p4m diag [svc]        write a diagnostics report to dev-tools/logs/

Run `p4m <command> -h` for details. All p4m files live in <workspace>/dev-tools/;
the repositories are cloned next to it, in <workspace>/.

The repositories are never modified by this tool (except repo-local git config and
CRLF->LF normalisation of shell scripts via `p4m repos fix-eol`). Workarounds are
applied through generated compose override files in .p4m/.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import dblock
import runtime
import testenv

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

INSTALLER_DIR = Path(__file__).resolve().parent    # <workspace>/dev-tools/installer
TOOLS_DIR = INSTALLER_DIR.parent                   # the dev-tools copy this runs from (checkout or runtime copy)
ROOT = TOOLS_DIR.parent                            # <workspace>: the repos live here
LOCAL_DIR = ROOT / "dev-tools"                     # local state lives here, whichever copy runs (`p4m runtime`)
STATE_DIR = LOCAL_DIR / ".p4m"
ENV_FILE = LOCAL_DIR / "p4m.env"
LOG_DIR = LOCAL_DIR / "logs"
CERT_DIR = STATE_DIR / "certs"
PROXY_DIR = STATE_DIR / "proxy"
GITHUB_ORG = "https://github.com/Projects4Me"
NETWORK = "backend"
IS_WINDOWS = os.name == "nt"
DEFAULT_DOMAIN = "local-projects4.me"
PROXY = "proxy"               # pseudo-repo for the generated Caddy proxy project
PROXY_CONTAINER = "p4m-proxy"
CADDY_IMAGE = "caddy:2.8-alpine"
CADDY_ROOT = "/data/caddy/pki/authorities/local/root.crt"

# repo name -> (branch, compose file or None)
REPOS: dict[str, tuple[str, str | None]] = {
    "gaia": ("dev", "docker-compose.yaml"),
    "hermes": ("dev", "docker-compose.yml"),
    "prometheus": ("dev", "docker-compose.yaml"),
    "ares": ("dev", "docker-compose.yml"),
    "docker-images": ("master", None),
    "dev-tools": ("main", None),
}

# Start order matters: proxy (CA) first, then postgres/gaia, frontend last.
STACK_ORDER = [PROXY, "gaia", "hermes", "ares", "prometheus"]

PG_CONTAINER = "postgres-dev"
PG_USER = "nouman"
PG_DB = "pr4m"


@dataclass(frozen=True)
class Service:
    name: str
    repo: str
    compose: str        # service name inside the compose file
    container: str
    group: str          # core | test | tools
    host: str | None = None       # subdomain under P4M_DOMAIN, routed by the proxy
    upstream: str | None = None   # container:port the proxy forwards to
    health_path: str = "/"
    port: int | None = None       # host port still published (postgres only)
    shell: str = "sh"
    desc: str = ""


SERVICES: list[Service] = [
    Service("proxy", PROXY, "proxy", PROXY_CONTAINER, "core", shell="sh",
            desc="Caddy HTTPS reverse proxy (local CA)"),
    Service("postgres", "gaia", "postgres", PG_CONTAINER, "core", port=5432,
            desc="PostgreSQL 16 (pr4m, pr4m_test)"),
    Service("gaia", "gaia", "gaia", "gaia-dev", "core", "api", "gaia-dev:80",
            shell="bash", desc="PHP/Phalcon REST API"),
    Service("hermes", "hermes", "hermes", "hermes", "core", "hermes", "hermes:9000",
            "/socket.io/?EIO=4&transport=polling", desc="Socket.IO realtime server"),
    Service("ares", "ares", "ares", "ares-dev", "core", "ares", "ares-dev:8000", "/docs",
            shell="bash", desc="FastAPI AI service"),
    Service("prometheus", "prometheus", "prometheus", "prometheus-dev", "core", "app",
            "prometheus-dev:4200", shell="bash", desc="Ember frontend"),
    Service("gaia-test", "gaia", "gaia-test", "gaia-test", "test", "api-test", "gaia-test:80",
            shell="bash", desc="Gaia on pr4m_test (api-tester)"),
    Service("gaia-test-hermes-down", "gaia", "gaia-test-hermes-down", "gaia-test-hermes-down",
            "test", "api-test-hermes-down", "gaia-test-hermes-down:80", shell="bash",
            desc="Gaia with unreachable Hermes (fail-open tests)"),
    Service("hermes-test", "hermes", "hermes-test", "hermes-test", "test", "hermes-test",
            "hermes-test:9001", "/socket.io/?EIO=4&transport=polling", desc="Hermes paired with gaia-test"),
    Service("xhgui", "gaia", "xhgui", "p4m-xhgui", "tools", "xhgui", "p4m-xhgui:80",
            desc="XHProf profiler UI"),
]
SVC = {s.name: s for s in SERVICES}

DEFAULT_ENV = """\
# p4m local settings. Loaded into the environment of every docker compose call.

# Base domain for all services (https://app.<domain>, https://api.<domain>, ...).
# Each name must resolve to 127.0.0.1 - `p4m hosts` maintains the Windows hosts file.
P4M_DOMAIN=local-projects4.me

# Required for ares (AI features). Leave empty to run ares without AI calls.
ANTHROPIC_API_KEY=

# Xdebug in gaia-dev. The repo config points Xdebug at port 9000, which is
# hermes' port; p4m moves it to 9003 (the IDE default). Set mode to "off" for speed.
GAIA_XDEBUG_PORT=9003
GAIA_XDEBUG_MODE=debug,develop

# File watching through Docker Desktop bind mounts does not deliver change events
# from Windows. "auto" enables polling watchers on Windows only. true|false|auto
FILE_WATCH_POLLING=auto

# Which services `p4m up` starts with no arguments: all | core
DEFAULT_SERVICES=all
"""


def domain() -> str:
    return settings().get("P4M_DOMAIN", DEFAULT_DOMAIN).strip().strip(".") or DEFAULT_DOMAIN


def fqdn(sub: str) -> str:
    return f"{sub}.{domain()}"


def svc_url(s: Service) -> str | None:
    return f"https://{fqdn(s.host)}" if s.host else None


def all_hostnames() -> list[str]:
    return [fqdn(s.host) for s in SERVICES if s.host]


def url_env() -> dict[str, dict[str, str]]:
    """Every service-to-service URL, derived from P4M_DOMAIN. Single source of truth for
    the compose overrides and `p4m urls`. The variable names are the ones the repos read."""
    D = domain()
    return {
        "gaia": {"FRONTEND_HOST": f"app.{D}", "HERMES_URL": f"https://hermes.{D}",
                 "LLM_SERVICE_URL": f"https://ares.{D}"},
        "gaia-test": {"FRONTEND_HOST": f"app.{D}", "HERMES_URL": f"https://hermes-test.{D}",
                      "LLM_SERVICE_URL": f"https://ares.{D}"},
        "gaia-test-hermes-down": {"FRONTEND_HOST": f"app.{D}", "LLM_SERVICE_URL": f"https://ares.{D}"},
        "hermes": {"GAIA_URL": f"https://api.{D}", "CORS_ORIGIN": f"https://app.{D}"},
        "hermes-test": {"GAIA_URL": f"https://api-test.{D}", "CORS_ORIGIN": f"https://app.{D}"},
        "prometheus": {"API_HOST_DEV": f"https://api.{D}", "HERMES_URL_DEV": f"https://hermes.{D}"},
    }


def _env_yaml(service: str, extra: dict[str, str] | None = None) -> str:
    items = {**url_env().get(service, {}), **(extra or {})}
    return "\n".join(f'      {k}: "{v}"' for k, v in items.items())


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #

if IS_WINDOWS:
    os.system("")  # enables ANSI escape processing in Windows consoles

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def info(msg: str) -> None:
    print(_c("36", "==> ") + msg, flush=True)


def ok(msg: str) -> None:
    print(_c("32", "  ✓ ") + msg, flush=True)


def warn(msg: str) -> None:
    print(_c("33", "  ! ") + msg, flush=True)


def err(msg: str) -> None:
    print(_c("31", "  ✗ ") + msg, file=sys.stderr, flush=True)


class P4MError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Process helpers
# --------------------------------------------------------------------------- #

def settings_key_entry() -> str:
    """A new random key for Gaia's secure settings, in SettingsCipher's format (32 bytes, base64)."""
    return ("\n# Encrypts Gaia's secure settings (e.g. the SMTP password). Generated for this machine: never commit it\n"
            "# or share it, and don't change it - settings saved with it can't be read with another key.\n"
            f"SETTINGS_ENCRYPTION_KEY={base64.b64encode(secrets.token_bytes(32)).decode('ascii')}\n")


def load_env() -> dict[str, str]:
    if not ENV_FILE.exists():
        ENV_FILE.write_text(DEFAULT_ENV, encoding="utf-8")
    else:
        text = ENV_FILE.read_text(encoding="utf-8")
        if "P4M_DOMAIN" not in text:   # upgrade older p4m.env files
            ENV_FILE.write_text(
                "# Base domain for all services (https://app.<domain>, https://api.<domain>, ...)\n"
                f"P4M_DOMAIN={DEFAULT_DOMAIN}\n\n" + text, encoding="utf-8")
    text = ENV_FILE.read_text(encoding="utf-8")
    if not re.search(r"^\s*SETTINGS_ENCRYPTION_KEY\s*=", text, re.M):   # added once; an existing key is kept
        ENV_FILE.write_text(text.rstrip("\n") + "\n" + settings_key_entry(), encoding="utf-8")
    values: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def settings() -> dict[str, str]:
    cfg = load_env()
    for k, v in cfg.items():          # real environment wins over the file
        cfg[k] = os.environ.get(k, v)
    return cfg


def polling_enabled(cfg: dict[str, str]) -> bool:
    v = cfg.get("FILE_WATCH_POLLING", "auto").lower()
    return IS_WINDOWS if v == "auto" else v in ("1", "true", "yes", "on")


def child_env() -> dict[str, str]:
    env = os.environ.copy()
    for k, v in settings().items():
        env.setdefault(k, v)
    # Git Bash on Windows rewrites /unix/paths in arguments; the gaia harness
    # passes container paths to `docker exec`, so turn that off.
    env["MSYS_NO_PATHCONV"] = "1"
    env["MSYS2_ARG_CONV_EXCL"] = "*"
    return env


def run(cmd: list[str], *, check: bool = True, capture: bool = False, cwd: Path | None = None,
        quiet: bool = False, input: str | None = None) -> subprocess.CompletedProcess:
    if not quiet and not capture:
        print(_c("90", "    $ " + " ".join(cmd)), flush=True)
    try:
        cp = subprocess.run(cmd, cwd=cwd, env=child_env(), text=True, input=input,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None,
                            encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise P4MError(f"command not found: {cmd[0]} (run `p4m doctor`)")
    if check and cp.returncode != 0:
        detail = (cp.stderr or "").strip() if capture else ""
        raise P4MError(f"command failed ({cp.returncode}): {' '.join(cmd)}" +
                       (f"\n{detail}" if detail else ""))
    return cp


def out(cmd: list[str]) -> str:
    cp = run(cmd, check=False, capture=True)
    return (cp.stdout or "").strip() if cp.returncode == 0 else ""


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


# --------------------------------------------------------------------------- #
# Docker / compose
# --------------------------------------------------------------------------- #

def docker_running() -> bool:
    return have("docker") and run(["docker", "info"], check=False, capture=True).returncode == 0


def ensure_docker(start: bool = True, timeout: int = 180) -> None:
    if not have("docker"):
        raise P4MError("Docker is not installed. Run `p4m install`.")
    if docker_running():
        return
    if start and IS_WINDOWS:
        exe = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Docker" / "Docker" / "Docker Desktop.exe"
        if exe.exists():
            info("Starting Docker Desktop (waiting up to %ds)..." % timeout)
            subprocess.Popen([str(exe)], close_fds=True)
            end = time.time() + timeout
            while time.time() < end:
                time.sleep(3)
                if docker_running():
                    ok("Docker is running")
                    return
    raise P4MError("Docker daemon is not running. Start Docker Desktop and retry.")


def _yaml_list(items: list[str], indent: int) -> str:
    pad = " " * indent
    return "\n".join(f"{pad}- {i}" for i in items)


def write_proxy() -> bool:
    """Generate the Caddy proxy project. Returns True if the Caddyfile changed."""
    PROXY_DIR.mkdir(parents=True, exist_ok=True)
    sites = []
    for s in SERVICES:
        if s.host and s.upstream:
            extra = ""
            if s.name == "prometheus":
                # ember live-reload runs its own server on 443 inside the container so the
                # injected snippet (which uses that port) works through https://app.<domain>
                extra = f"\treverse_proxy /_lr/* {s.container}:443\n"
            sites.append(f"{fqdn(s.host)} {{\n{extra}\treverse_proxy {s.upstream}\n}}\n")
    # Local CA certs: Caddy's defaults are 12h leaves from a 7-day intermediate, renewed while it runs.
    # After the PC sleeps past a leaf's expiry, HTTPS fails until Caddy's next maintenance pass, so
    # issue long-lived ones instead (same root, so nothing needs re-trusting).
    caddyfile = ("# generated by p4m - do not edit (edit p4m.env / p4m.py instead)\n"
                 "{\n\tskip_install_trust\n"
                 "\tpki {\n\t\tca local {\n\t\t\tintermediate_lifetime 180d\n\t\t}\n\t}\n"
                 "\tcert_issuer internal {\n\t\tlifetime 90d\n\t}\n}\n\n" + "\n".join(sites))
    compose_yml = f"""\
# generated by p4m - do not edit
name: p4m
services:
  proxy:
    image: {CADDY_IMAGE}
    container_name: {PROXY_CONTAINER}
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
      - "443:443/udp"
    volumes:
      - ./:/etc/caddy:ro
      - caddy_data:/data
      - caddy_config:/config
    networks:
      {NETWORK}:
        # containers resolve the public hostnames to the proxy too, so every
        # service can use the same https URLs as the browser
        aliases:
{_yaml_list(all_hostnames(), 10)}
networks:
  {NETWORK}:
    external: true
volumes:
  caddy_data:
  caddy_config:
"""
    changed = False
    for name, text in (("Caddyfile", caddyfile), ("docker-compose.yml", compose_yml)):
        f = PROXY_DIR / name
        if not f.exists() or f.read_text(encoding="utf-8") != text:
            f.write_text(text, encoding="utf-8", newline="\n")
            changed = changed or name == "Caddyfile"
    return changed


def write_overrides() -> dict[str, Path]:
    """Generate compose override files: URL config from P4M_DOMAIN, HTTPS-only routing via
    the proxy, local CA trust inside containers, and workarounds for repo issues.
    Repos themselves are never edited."""
    STATE_DIR.mkdir(exist_ok=True)
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    cfg = settings()
    poll = polling_enabled(cfg)
    D = domain()
    bundle = (CERT_DIR / "ca-bundle.crt").resolve().as_posix()
    certs = CERT_DIR.resolve().as_posix()
    # Mount the whole certs folder (single-file mounts over system files proved unreliable on
    # Docker Desktop) and point PHP curl/OpenSSL at the bundle via an extra php.ini scan dir.
    gaia_ca = f'      - "{certs}:/etc/ssl/p4m:ro"'
    node_ca = f'      - "{certs}:/p4m-certs:ro"'
    # xdebug.ini targets :9000 which is hermes; move it to the IDE port.
    php_ca = {"PHP_INI_SCAN_DIR": "/usr/local/etc/php/conf.d:/etc/ssl/p4m/php",
              "CURL_CA_BUNDLE": "/etc/ssl/p4m/ca-bundle.crt",
              "SSL_CERT_FILE": "/etc/ssl/p4m/ca-bundle.crt"}
    xdebug = {"XDEBUG_CONFIG": f"client_port={cfg.get('GAIA_XDEBUG_PORT', '9003')}",
              "XDEBUG_MODE": cfg.get("GAIA_XDEBUG_MODE", "debug,develop")}
    node_extra = {"NODE_EXTRA_CA_CERTS": "/p4m-certs/p4m-root.crt"}
    files: dict[str, str] = {}

    files["gaia"] = f"""\
# generated by p4m - do not edit
services:
  gaia:
    environment:
{_env_yaml("gaia", {**php_ca, **xdebug})}
    volumes:
{gaia_ca}
    ports: !reset []
  gaia-test:
    environment:
{_env_yaml("gaia-test", php_ca)}
    volumes:
{gaia_ca}
    ports: !reset []
  gaia-test-hermes-down:
    environment:
{_env_yaml("gaia-test-hermes-down", php_ca)}
    volumes:
{gaia_ca}
    ports: !reset []
  xhgui:
    container_name: p4m-xhgui
    ports: !reset []
"""

    hermes_cmd = '["npx", "nodemon", "--legacy-watch", "server.js"]' if poll else '["npm", "start"]'
    files["hermes"] = f"""\
# generated by p4m - do not edit
services:
  hermes:
    command: {hermes_cmd}
    environment:
{_env_yaml("hermes", node_extra)}
    volumes:
{node_ca}
    networks: [{NETWORK}]
    ports: !reset []
  hermes-test:
    command: {hermes_cmd}
    environment:
{_env_yaml("hermes-test", node_extra)}
    volumes:
{node_ca}
    networks: [{NETWORK}]
    ports: !reset []
networks:
  {NETWORK}:
    external: true
"""

    # Source is synced into the container fs by `docker compose watch` (see below), so the
    # native file watcher works and no polling over the slow Windows mount is needed.
    ember_cmd = '["ember", "serve", "--live-reload-port", "443"]'
    files["prometheus"] = f"""\
# generated by p4m - do not edit
services:
  prometheus:
    command: {ember_cmd}
    environment:
{_env_yaml("prometheus")}
    # No bind mount of the source: on Windows it makes the ember build crawl (15+ min).
    # The image already contains the source; `p4m` runs `docker compose watch`, which syncs
    # it on start (initial_sync) and copies every edit into the container within ~1s.
    volumes: !override
      - prometheus_node_modules:/prometheus/node_modules
    develop:
      watch:
        - action: sync
          path: .
          target: /prometheus
          initial_sync: true
          ignore:
            - node_modules/
            - dist/
            - tmp/
            - .git/
            - .idea/
            - docs/
            - coverage/
    networks: [{NETWORK}]
    ports: !reset []
networks:
  {NETWORK}:
    external: true
volumes:
  prometheus_node_modules:
"""

    files["ares"] = f"""\
# generated by p4m - do not edit
services:
  ares:
    # repo compose has `build: rananoman/ares:dev-latest` (an image name, not a path)
    build:
      context: .
    image: p4m/ares:dev
    environment:
      WATCHFILES_FORCE_POLLING: "{'true' if poll else 'false'}"
    healthcheck:
      # repo healthcheck uses curl (not in python:slim) against /health (no such route)
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/docs', timeout=5)"]
    # HTTP goes through the proxy; keep only the debugpy port
    ports: !override
      - "5678:5678"
"""
    paths = {}
    for repo, text in files.items():
        p = STATE_DIR / f"{repo}.override.yml"
        if not p.exists() or p.read_text(encoding="utf-8") != text:
            p.write_text(text, encoding="utf-8", newline="\n")
        paths[repo] = p
    return paths


_STARTING = {"up", "run", "create", "start", "restart"}


def compose(repo: str, *args: str, check: bool = True, capture: bool = False,
            quiet: bool = False) -> subprocess.CompletedProcess:
    if repo == PROXY:
        write_proxy()
        cmd = ["docker", "compose", "-p", "p4m", "--project-directory", str(PROXY_DIR),
               "-f", str(PROXY_DIR / "docker-compose.yml"), *args]
        return run(cmd, check=check, capture=capture, quiet=quiet)
    branch, cfile = REPOS[repo]
    if not cfile:
        raise P4MError(f"{repo} has no compose file")
    rdir = ROOT / repo
    if not (rdir / cfile).exists():
        raise P4MError(f"{rdir / cfile} not found - run `p4m repos sync`")
    override = write_overrides()[repo]
    if args and args[0] in _STARTING and not (CERT_DIR / "php" / "p4m-ca.ini").exists():
        # the CA bundle is bind-mounted into containers; it must exist first
        ensure_proxy()
    cmd = ["docker", "compose", "-p", repo, "--project-directory", str(rdir),
           "-f", str(rdir / cfile), "-f", str(override), *args]
    return run(cmd, check=check, capture=capture, quiet=quiet)


# --------------------------------------------------------------------------- #
# Proxy, local CA and hosts file
# --------------------------------------------------------------------------- #

def ensure_proxy() -> None:
    ensure_network()
    changed = write_proxy()
    was_running = is_running(PROXY_CONTAINER)
    compose(PROXY, "up", "-d")
    if changed and was_running:
        run(["docker", "exec", PROXY_CONTAINER, "caddy", "reload", "--config",
             "/etc/caddy/Caddyfile", "--adapter", "caddyfile"], check=False, quiet=True)
    export_ca()


def export_ca(timeout: int = 60) -> Path:
    """Copy Caddy's local root CA out of the proxy and build a CA bundle for containers."""
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    end = time.time() + timeout
    root_pem = ""
    while time.time() < end:
        root_pem = out(["docker", "exec", PROXY_CONTAINER, "cat", CADDY_ROOT])
        if "BEGIN CERTIFICATE" in root_pem:
            break
        time.sleep(1)
    else:
        raise P4MError("proxy did not create its local CA - see `p4m logs proxy`")
    system = out(["docker", "exec", PROXY_CONTAINER, "cat", "/etc/ssl/certs/ca-certificates.crt"])
    root_pem = root_pem.strip() + "\n"
    bundle = system.strip() + "\n\n# Projects4Me local dev CA (Caddy)\n" + root_pem
    (CERT_DIR / "php").mkdir(exist_ok=True)
    php_ini = ("; generated by p4m - trust the local dev CA in PHP curl + openssl\n"
               "curl.cainfo=/etc/ssl/p4m/ca-bundle.crt\n"
               "openssl.cafile=/etc/ssl/p4m/ca-bundle.crt\n")
    for name, text in (("p4m-root.crt", root_pem), ("ca-bundle.crt", bundle),
                       ("php/p4m-ca.ini", php_ini)):
        f = CERT_DIR / name
        if not f.exists() or f.read_text(encoding="utf-8") != text:
            f.write_text(text, encoding="utf-8", newline="\n")
    return CERT_DIR / "p4m-root.crt"


def _root_thumbprint() -> str | None:
    import base64
    import hashlib
    f = CERT_DIR / "p4m-root.crt"
    if not f.exists():
        return None
    b64 = "".join(l for l in f.read_text().splitlines() if l and "CERTIFICATE" not in l)
    return hashlib.sha1(base64.b64decode(b64)).hexdigest().upper()


def ca_trusted() -> bool | None:
    tp = _root_thumbprint()
    if not tp:
        return None
    if IS_WINDOWS:
        return run(["certutil", "-user", "-verifystore", "Root", tp], check=False,
                   capture=True).returncode == 0
    marker = CERT_DIR / ".trusted"
    return marker.exists() and marker.read_text().strip() == tp


def trust_ca() -> None:
    root = CERT_DIR / "p4m-root.crt"
    if not root.exists():
        ensure_docker()
        ensure_proxy()
    if ca_trusted():
        ok("local CA already trusted")
        return
    info("Trusting the p4m local CA (Windows will ask you to confirm)")
    system = platform.system()
    if system == "Windows":
        run(["certutil", "-user", "-addstore", "Root", str(root)])
    elif system == "Darwin":
        run(["sudo", "security", "add-trusted-cert", "-d", "-r", "trustRoot", "-k",
             "/Library/Keychains/System.keychain", str(root)])
    else:
        # Linux (Debian/Ubuntu): system store for curl, Python and `node --use-system-ca`...
        run(["sudo", "cp", str(root), "/usr/local/share/ca-certificates/p4m-root.crt"])
        run(["sudo", "update-ca-certificates"])
        # ...and the NSS store Chrome/Chromium read on Linux (needs libnss3-tools for `certutil`).
        nssdb = Path.home() / ".pki" / "nssdb"
        if have("certutil") and nssdb.is_dir():
            run(["certutil", "-d", f"sql:{nssdb}", "-A", "-t", "C,,", "-n", "p4m-local-ca", "-i", str(root)],
                check=False)
        else:
            warn("browsers on Linux keep their own store: `sudo apt install libnss3-tools`, open Chrome once, "
                 "then run `p4m trust` again (Firefox: set security.enterprise_roots.enabled = true)")
    if not IS_WINDOWS:
        (CERT_DIR / ".trusted").write_text(_root_thumbprint() or "")
    ok("trusted. Restart the browser. (Firefox: set security.enterprise_roots.enabled = true)")


HOSTS_FILE = (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "drivers" / "etc" / "hosts"
              if IS_WINDOWS else Path("/etc/hosts"))
HOSTS_BEGIN = "# >>> p4m (Projects4Me local dev) >>>"
HOSTS_END = "# <<< p4m <<<"


def hosts_block() -> str:
    lines = [HOSTS_BEGIN] + [f"127.0.0.1 {h}" for h in all_hostnames()] + [HOSTS_END]
    return "\n".join(lines)


def hosts_current() -> str:
    try:
        return HOSTS_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def hosts_with_block(text: str) -> str:
    text = text.replace("\r\n", "\n")
    if HOSTS_BEGIN in text and HOSTS_END in text:
        pre, rest = text.split(HOSTS_BEGIN, 1)
        _, post = rest.split(HOSTS_END, 1)
        text = pre.rstrip("\n") + "\n" + post.lstrip("\n")
    return text.rstrip("\n") + "\n\n" + hosts_block() + "\n"


def hosts_ok() -> bool:
    return hosts_block() in hosts_current().replace("\r\n", "\n")


def is_admin() -> bool:
    if IS_WINDOWS:
        import ctypes
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return os.geteuid() == 0


def apply_hosts() -> None:
    new = hosts_with_block(hosts_current())
    if IS_WINDOWS:
        new = new.replace("\n", "\r\n")
    HOSTS_FILE.write_text(new, encoding="utf-8", newline="")
    if IS_WINDOWS:
        run(["ipconfig", "/flushdns"], check=False, capture=True)


def ensure_hosts() -> None:
    if hosts_ok():
        ok(f"hosts file has {len(all_hostnames())} *.{domain()} entries")
        return
    info(f"Adding *.{domain()} entries to {HOSTS_FILE}")
    if is_admin():
        apply_hosts()
    elif IS_WINDOWS:
        import ctypes
        params = f'"{Path(__file__).resolve()}" hosts --apply'
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, str(ROOT), 0)
        if rc <= 32:
            raise P4MError("elevation was cancelled - run `p4m hosts` again, or add these lines "
                           f"manually as administrator:\n{hosts_block()}")
        for _ in range(60):          # elevated process runs asynchronously
            time.sleep(1)
            if hosts_ok():
                break
    else:
        run(["sudo", sys.executable, str(Path(__file__).resolve()), "hosts", "--apply"])
    if not hosts_ok():
        raise P4MError(f"hosts file not updated. Add manually:\n{hosts_block()}")
    ok("hosts file updated")


def ensure_network() -> None:
    if run(["docker", "network", "inspect", NETWORK], check=False, capture=True).returncode != 0:
        run(["docker", "network", "create", NETWORK])
        ok(f"created docker network '{NETWORK}'")


def container_state(name: str) -> tuple[str, str]:
    """Return (status, health) for a container; ('missing', '') if absent."""
    fmt = "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}"
    s = out(["docker", "inspect", "-f", fmt, name])
    if not s:
        return "missing", ""
    status, _, health = s.partition("|")
    return status, health


def is_running(name: str) -> bool:
    return container_state(name)[0] == "running"


def resolve_services(names: list[str], default_all: bool = True) -> list[Service]:
    if not names:
        mode = settings().get("DEFAULT_SERVICES", "all").lower()
        if mode == "core" or not default_all:
            return [s for s in SERVICES if s.group == "core"]
        return list(SERVICES)
    result: list[Service] = []
    for n in names:
        if n in ("all",):
            result += SERVICES
        elif n in ("core", "test", "tools"):
            result += [s for s in SERVICES if s.group == n]
        elif (n in REPOS or n == PROXY) and n not in SVC:
            result += [s for s in SERVICES if s.repo == n]
        elif n in SVC:
            result.append(SVC[n])
        else:
            raise P4MError(f"unknown service '{n}'. Known: {', '.join(SVC)} (or all/core/test/tools)")
    seen, uniq = set(), []
    for s in result:
        if s.name not in seen:
            seen.add(s.name)
            uniq.append(s)
    return uniq


def by_repo(services: list[Service]) -> list[tuple[str, list[Service]]]:
    return [(r, [s for s in services if s.repo == r]) for r in STACK_ORDER
            if any(s.repo == r for s in services)]


def wait_for_postgres(timeout: int = 600) -> None:
    """Wait until the init scripts (dump import) are finished, not just the socket."""
    info("Waiting for postgres (first start imports the SQL dump - can take a few minutes)...")
    end = time.time() + timeout
    while time.time() < end:
        # TCP check: the temporary init server only listens on the unix socket
        cp = run(["docker", "exec", PG_CONTAINER, "pg_isready", "-h", "127.0.0.1",
                  "-U", PG_USER, "-d", PG_DB], check=False, capture=True)
        if cp.returncode == 0:
            ok("postgres is ready")
            return
        time.sleep(3)
    raise P4MError("postgres did not become ready in time - see `p4m logs postgres`")




def _ssl_ctx():
    import ssl
    root = CERT_DIR / "p4m-root.crt"
    ctx = ssl.create_default_context()
    if root.exists():
        ctx.load_verify_locations(cafile=str(root))
    return ctx


def resolves_local(host: str) -> bool:
    try:
        return socket.gethostbyname(host) == "127.0.0.1"
    except OSError:
        return False


def http_status(url: str, timeout: float = 3.0) -> int | None:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                             urllib.request.HTTPSHandler(context=_ssl_ctx()))
        with opener.open(url, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


# --------------------------------------------------------------------------- #
# Git / repos
# --------------------------------------------------------------------------- #

def git(repo: str, *args: str, **kw) -> subprocess.CompletedProcess:
    return run(["git", "-C", str(ROOT / repo), *args], **kw)


def repo_branch(repo: str) -> str:
    return out(["git", "-C", str(ROOT / repo), "rev-parse", "--abbrev-ref", "HEAD"])


def repos_sync(pull: bool = True) -> None:
    """Bring the workspace to the latest: the Pr4M root, then every repo on the tip of its branch.
    In Pr4M the repos are submodules, but nobody works at the pinned commits; this keeps each on its branch."""
    if not have("git"):
        raise P4MError("git is not installed. Run `p4m install`.")
    if (ROOT / ".git").exists() and pull:
        info("Pr4M workspace")
        dirty = bool(out(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no",
                          "--ignore-submodules=all"]))
        if dirty:
            warn("local changes in the workspace root - skipping pull")
        elif run(["git", "-C", str(ROOT), "pull", "--ff-only", "--quiet"], check=False).returncode != 0:
            warn("could not fast-forward the workspace root - resolve manually")
    for repo, (branch, _) in REPOS.items():
        path = ROOT / repo
        info(f"{repo} ({branch})")
        if not (path / ".git").exists():
            run(["git", "clone", "--branch", branch, f"{GITHUB_ORG}/{repo}.git", str(path)])
            continue
        git(repo, "fetch", "--all", "--prune", "--quiet")
        current = repo_branch(repo)
        dirty = bool(out(["git", "-C", str(path), "status", "--porcelain", "--untracked-files=no"]))
        if current != branch:
            if dirty:
                warn(f"on '{current}' with local changes - not switching to '{branch}'")
                continue
            git(repo, "checkout", branch)
        if pull:
            if dirty:
                warn("local changes - skipping pull")
            else:
                cp = git(repo, "pull", "--ff-only", "--quiet", check=False)
                if cp.returncode != 0:
                    warn("could not fast-forward (diverged?) - resolve manually")
    fix_eol(quiet=True)


def repos_status() -> None:
    print(f"{'repo':<15}{'branch':<18}{'expected':<10}{'state':<12}{'ahead/behind':<14}last commit")
    for repo, (branch, _) in REPOS.items():
        path = ROOT / repo
        if not (path / ".git").exists():
            print(f"{repo:<15}{'-':<18}{branch:<10}{_c('31', 'missing')}")
            continue
        cur = repo_branch(repo)
        dirty = out(["git", "-C", str(path), "status", "--porcelain"])
        ab = out(["git", "-C", str(path), "rev-list", "--left-right", "--count", "HEAD...@{u}"])
        ab = "/".join(ab.split()) if ab else "-"
        last = out(["git", "-C", str(path), "log", "-1", "--format=%h %cr %s"])[:60]
        state = _c("33", "modified") if dirty else _c("32", "clean")
        br = cur if cur == branch else _c("33", cur)
        pad = " " * max(0, 18 - len(cur))
        spad = " " * (12 - len("modified" if dirty else "clean"))
        print(f"{repo:<15}{br}{pad}{branch:<10}{state}{spad}{ab:<14}{last}")


def fix_eol(quiet: bool = False) -> int:
    """Shell scripts checked out with CRLF break inside Linux containers.

    Sets core.autocrlf=input in each repo (repo-local config) and rewrites tracked
    *.sh files that contain CRLF, but only when that is the only difference.
    """
    fixed = sum(fix_eol_in(ROOT / repo, repo, quiet) for repo in REPOS if (ROOT / repo / ".git").exists())
    if not quiet:
        ok(f"{fixed} file(s) normalised") if fixed else ok("no CRLF shell scripts found")
    return fixed


def fix_eol_in(path: Path, label: str, quiet: bool = True) -> int:
    """fix_eol for one checkout (also used by `p4m test` on the checkout it tests)."""
    fixed = 0
    run(["git", "-C", str(path), "config", "core.autocrlf", "input"], quiet=True)
    files = out(["git", "-C", str(path), "ls-files", "*.sh"]).splitlines()
    for rel in files:
        f = path / rel
        if not f.exists():
            continue
        data = f.read_bytes()
        if b"\r\n" not in data:
            continue
        real_change = run(["git", "-C", str(path), "diff", "--quiet", "--ignore-cr-at-eol",
                           "--", rel], check=False, capture=True).returncode != 0
        if real_change:
            warn(f"{label}/{rel} has CRLF and local edits - left as is")
            continue
        f.write_bytes(data.replace(b"\r\n", b"\n"))
        # Same content as the (LF) blob, so this stages nothing; it refreshes the index entry, which
        # otherwise keeps showing the file as modified.
        run(["git", "-C", str(path), "add", "--", rel], check=False, quiet=True)
        fixed += 1
        if not quiet:
            ok(f"{label}/{rel} -> LF")
    return fixed


def find_bash() -> str:
    """Return a bash that can run the gaia harness scripts against docker."""
    if not IS_WINDOWS:
        b = shutil.which("bash")
        if b:
            return b
        raise P4MError("bash not found")
    # Prefer Git Bash - System32\bash.exe is WSL, which may not see Docker.
    candidates = []
    exec_path = out(["git", "--exec-path"])
    if exec_path:
        git_root = Path(exec_path).resolve().parents[2]
        candidates += [git_root / "bin" / "bash.exe", git_root / "usr" / "bin" / "bash.exe"]
    pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    candidates += [pf / "Git" / "bin" / "bash.exe"]
    for c in candidates:
        if c.exists():
            return str(c)
    raise P4MError("Git Bash not found. Install Git for Windows (`p4m install`).")


DLL_INIT_FAILED = (3221225794, -1073741502)     # 0xC0000142: intermittent on Windows when Git Bash starts
BASH_SPAWN_RETRIES = 2


def run_bash(args: list, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run `find_bash()` with `args` (never raises on its exit code). A Git Bash that fails to start
    (STATUS_DLL_INIT_FAILED) is retried."""
    bash = find_bash()
    for attempt in range(BASH_SPAWN_RETRIES + 1):
        cp = run([bash, *args], cwd=cwd, check=False)
        if cp.returncode not in DLL_INIT_FAILED or attempt == BASH_SPAWN_RETRIES:
            return cp
        warn(f"Git Bash failed to start (exit {cp.returncode}, STATUS_DLL_INIT_FAILED); retrying")
        time.sleep(2)


# --------------------------------------------------------------------------- #
# Prerequisites
# --------------------------------------------------------------------------- #

PREREQS_WINGET = [
    # (command to detect, winget id, friendly name)
    ("git", "Git.Git", "Git for Windows"),
    ("docker", "Docker.DockerDesktop", "Docker Desktop"),
]


def wsl_ok() -> bool:
    if not IS_WINDOWS:
        return True
    cp = run(["wsl", "--status"], check=False, capture=True)
    return cp.returncode == 0


def cmd_install(args) -> None:
    system = platform.system()
    if system == "Windows":
        if not have("winget"):
            raise P4MError("winget not found. Install 'App Installer' from the Microsoft Store.")
        if not wsl_ok():
            warn("WSL 2 is not set up (Docker Desktop needs it).")
            print("    Run in an *administrator* terminal, then reboot:  wsl --install --no-distribution")
        missing = [(c, i, n) for c, i, n in PREREQS_WINGET if not have(c)]
        if not missing:
            ok("Git and Docker Desktop are already installed")
        for c, wid, name in missing:
            info(f"Installing {name}")
            run(["winget", "install", "--id", wid, "-e", "--source", "winget",
                 "--accept-package-agreements", "--accept-source-agreements"], check=False)
        if missing:
            warn("Open a NEW terminal so PATH changes apply. Docker Desktop may require a "
                 "sign-out/reboot and must be started once to finish setup.")
    elif system == "Darwin":
        if not have("brew"):
            raise P4MError("Homebrew not found: https://brew.sh")
        if not have("git"):
            run(["brew", "install", "git"])
        if not have("docker"):
            run(["brew", "install", "--cask", "docker"])
        ok("done - start Docker.app once to finish setup")
    else:
        print("Install with your package manager:\n"
              "  git, docker engine + compose plugin  (https://docs.docker.com/engine/install/)\n"
              "and add yourself to the docker group.")


def cmd_doctor(args) -> int:
    problems = 0
    info("Tools")
    print(f"  python   {platform.python_version()}")
    for c in ("git", "docker"):
        if have(c):
            ok(f"{c}: " + out([c, "--version"]))
        else:
            err(f"{c} not found - run `p4m install`"); problems += 1
    if have("docker"):
        v = out(["docker", "compose", "version", "--short"])
        nums = [int(x) for x in v.lstrip("v").split("-")[0].split(".")[:3] if x.isdigit()] if v else []
        if v and nums and tuple(nums) < (2, 24, 4):
            err(f"docker compose {v} is too old (need >= 2.24.4) - update Docker Desktop"); problems += 1
        elif v:
            ok(f"docker compose {v}")
        else:
            err("docker compose v2 plugin missing"); problems += 1
    if IS_WINDOWS:
        if wsl_ok():
            ok("WSL available")
        else:
            err("WSL 2 not set up: `wsl --install --no-distribution` (admin) + reboot"); problems += 1
        try:
            find_bash(); ok("Git Bash found (used for DB test harness)")
        except P4MError as e:
            warn(str(e))

    info("Docker")
    if docker_running():
        ok("daemon running")
        if run(["docker", "network", "inspect", NETWORK], check=False, capture=True).returncode == 0:
            ok(f"network '{NETWORK}' exists")
        else:
            warn(f"network '{NETWORK}' missing (created by `p4m up`)")
    else:
        err("daemon not reachable - start Docker Desktop"); problems += 1

    info("Repositories")
    for repo, (branch, _) in REPOS.items():
        if not (ROOT / repo / ".git").exists():
            err(f"{repo}: not cloned - run `p4m repos sync`"); problems += 1
            continue
        cur = repo_branch(repo)
        (ok if cur == branch else warn)(f"{repo}: on '{cur}'" + ("" if cur == branch else f" (expected '{branch}')"))
        crlf = [f for f in out(["git", "-C", str(ROOT / repo), "ls-files", "*.sh"]).splitlines()
                if (ROOT / repo / f).exists() and b"\r\n" in (ROOT / repo / f).read_bytes()]
        if crlf:
            warn(f"{repo}: {len(crlf)} shell script(s) have CRLF line endings - run `p4m repos fix-eol`")

    info("Ports")
    for port, owner in ((80, PROXY_CONTAINER), (443, PROXY_CONTAINER), (5432, PG_CONTAINER)):
        if port_open(port) and not is_running(owner):
            err(f"port {port} is used by another program (needed by {owner})"); problems += 1
    ok("checked")

    info(f"Domain: {domain()}")
    if hosts_ok():
        ok("hosts file entries present")
    else:
        err("hosts file entries missing/outdated - run `p4m hosts`"); problems += 1
    bad = [h for h in all_hostnames() if not resolves_local(h)]
    if bad:
        warn(f"{len(bad)} hostname(s) don't resolve to 127.0.0.1 yet: {', '.join(bad[:3])}...")
    t = ca_trusted()
    if t is None:
        warn("local CA not created yet (created by `p4m up`)")
    elif t:
        ok("local HTTPS CA trusted")
    else:
        err("local HTTPS CA not trusted - run `p4m trust`"); problems += 1

    info("Settings (p4m.env)")
    cfg = settings()
    if cfg.get("ANTHROPIC_API_KEY"):
        ok("ANTHROPIC_API_KEY set")
    else:
        warn("ANTHROPIC_API_KEY empty - ares will start but AI endpoints will fail")
    if cfg.get("SETTINGS_ENCRYPTION_KEY"):
        ok("SETTINGS_ENCRYPTION_KEY set")
    else:
        warn("SETTINGS_ENCRYPTION_KEY empty - Gaia can't save secure settings (e.g. the SMTP password)")
    print()
    (ok if not problems else err)(f"{problems} problem(s) found")
    return 1 if problems else 0


# --------------------------------------------------------------------------- #
# Stack commands
# --------------------------------------------------------------------------- #

def migrate() -> None:
    """Migrate pr4m with this workspace's gaia metadata. A shared gaia-dev that mounts another checkout would
    drop the columns its older metadata doesn't know, so then a throwaway container is used instead."""
    src = ROOT / "gaia"
    info(f"Running gaia migrations (pr4m) from {src}")
    if not is_running("gaia-dev"):
        compose("gaia", "up", "-d", "gaia")
    container = _test_container("gaia-dev", src, "/var/www/html")
    run(["docker", "exec", "-w", "/var/www/html", container, "php", "cli.php", "migration", "migrate"])


def prepare_test_db(src: Path | None = None) -> None:
    """Seed pr4m_test with the api-tester fixtures of `src` (default: the workspace's gaia)."""
    src = src or ROOT / "gaia"
    info(f"Preparing pr4m_test (api-tester fixtures from {src})")
    if not is_running(PG_CONTAINER):
        raise P4MError("postgres is not running - `p4m up postgres` first")
    fix_eol_in(src, src.name)
    script = "tools/api-tester/harness/prepare-test-db.sh"
    rc = run_bash([script], cwd=src).returncode
    if rc != 0:
        raise P4MError(f"command failed ({rc}): {find_bash()} {script}")


def ensure_prometheus_deps(force: bool = False) -> None:
    """Keep the node_modules volume in sync with prometheus/package.json.

    A fresh named volume is pre-filled by Docker with the (old) node_modules baked
    into the base image, so "is it empty?" is not a valid check. Instead a stamp with
    the hash of package.json(+lock) is stored in the volume after a successful install.
    """
    import hashlib
    h = hashlib.sha256()
    for name in ("package.json", "package-lock.json", "yarn.lock"):
        f = ROOT / "prometheus" / name
        if f.exists():
            h.update(f.read_bytes().replace(b"\r\n", b"\n"))
    want = h.hexdigest()
    stamp = "node_modules/.p4m-deps-stamp"
    if not force:
        cp = compose("prometheus", "run", "--rm", "--no-deps", "--entrypoint", "sh", "prometheus",
                     "-c", f"cat {stamp} 2>/dev/null || true", check=False, capture=True)
        if (cp.stdout or "").strip() == want:
            return
    info("Installing prometheus npm dependencies into its docker volume (can take several minutes)")
    pkg = (ROOT / "prometheus" / "package.json").resolve().as_posix()
    compose("prometheus", "run", "--rm", "--no-deps", "-v", f"{pkg}:/prometheus/package.json:ro",
            "--entrypoint", "sh", "prometheus", "-c",
            f"npm install --no-audit --no-fund && echo {want} > {stamp}")


# --- `docker compose watch` (file sync into containers) ---------------------- #

WATCHED = {"prometheus": ["prometheus"]}      # repo -> services with develop.watch


def _watch_pidfile(repo: str) -> Path:
    return STATE_DIR / f"watch-{repo}.pid"


def _pid_alive(pid: int) -> bool:
    if IS_WINDOWS:
        o = out(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"])
        return f'"{pid}"' in o
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def watch_running(repo: str) -> int | None:
    f = _watch_pidfile(repo)
    try:
        pid = int(f.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if _pid_alive(pid) else None


def start_watch(repo: str) -> None:
    if watch_running(repo):
        return
    branch, cfile = REPOS[repo]
    rdir = ROOT / repo
    LOG_DIR.mkdir(exist_ok=True)
    log = open(LOG_DIR / f"watch-{repo}.log", "a", encoding="utf-8")
    log.write(f"\n--- watch started {time.ctime()} ---\n"); log.flush()
    cmd = ["docker", "compose", "-p", repo, "--project-directory", str(rdir),
           "-f", str(rdir / cfile), "-f", str(write_overrides()[repo]),
           "watch", "--no-up", *WATCHED[repo]]
    kw: dict = dict(stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=child_env())
    if IS_WINDOWS:
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
    else:
        kw["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kw)
    _watch_pidfile(repo).write_text(str(proc.pid))
    ok(f"file sync started for {repo} (log: dev-tools/logs/watch-{repo}.log)")


def stop_watch(repo: str) -> None:
    pid = watch_running(repo)
    if pid:
        if IS_WINDOWS:
            run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False, capture=True)
        else:
            try:
                os.killpg(pid, 15)
            except OSError:
                pass
        ok(f"file sync stopped for {repo}")
    _watch_pidfile(repo).unlink(missing_ok=True)


def cmd_watch(args) -> None:
    repos = [args.repo] if args.repo else list(WATCHED)
    for r in repos:
        if args.action == "start":
            start_watch(r)
        elif args.action == "stop":
            stop_watch(r)
        else:
            pid = watch_running(r)
            print(f"{r:<12}{'running (pid %d)' % pid if pid else 'stopped'}")


def cmd_up(args) -> None:
    ensure_docker()
    ensure_network()
    services = [s for s in resolve_services(args.services) if s.repo != PROXY]
    info("Starting proxy (https://*.%s)" % domain())
    ensure_proxy()
    if not hosts_ok():
        warn(f"hosts file has no/old *.{domain()} entries - run `p4m hosts`")
    if ca_trusted() is False:
        warn("browser will not trust the local CA yet - run `p4m trust`")
    if any(s.name == "ares" for s in services) and not settings().get("ANTHROPIC_API_KEY"):
        warn("ANTHROPIC_API_KEY is empty in p4m.env - ares AI endpoints will fail")
    first_db = any(s.repo == "gaia" for s in services) and container_state(PG_CONTAINER)[0] == "missing"
    if any(s.name == "prometheus" for s in services):
        ensure_prometheus_deps()
    for repo, svcs in by_repo(services):
        info(f"Starting {repo}: {', '.join(s.name for s in svcs)}")
        extra = ["--build"] if args.build else []
        compose(repo, "up", "-d", *extra, *[s.compose for s in svcs])
    for repo in WATCHED:
        if any(s.repo == repo for s in services):
            start_watch(repo)
    if first_db:
        wait_for_postgres()
        warn("fresh database - run `p4m db migrate` (and `p4m db prepare-test` for api tests)")
    print()
    cmd_status(argparse.Namespace(services=["proxy"] + [s.name for s in services], wait=args.wait))


def cmd_down(args) -> None:
    ensure_docker(start=False)
    for repo in WATCHED:
        if not args.services or any(s.repo == repo for s in resolve_services(args.services)):
            stop_watch(repo)
    if not args.services:
        for repo in reversed(STACK_ORDER):
            info(f"Stopping {repo}")
            # never drop the proxy volume: it holds the local CA you trusted
            vol = ["-v"] if args.volumes and repo != PROXY else []
            compose(repo, "down", *vol, check=False)
        return
    for repo, svcs in by_repo(resolve_services(args.services)):
        compose(repo, "stop", *[s.compose for s in svcs])
        compose(repo, "rm", "-f", *[s.compose for s in svcs])


def cmd_restart(args) -> None:
    ensure_docker(start=False)
    for repo, svcs in by_repo(resolve_services(args.services)):
        compose(repo, "restart", *[s.compose for s in svcs])


def cmd_build(args) -> None:
    ensure_docker()
    services = resolve_services(args.services)
    for repo, svcs in by_repo(services):
        info(f"Building {repo}")
        if repo == PROXY:
            continue
        compose(repo, "build", *(["--pull"] if args.pull else []), *[s.compose for s in svcs if s.name != "postgres" and s.name != "xhgui"])
    if any(s.name == "prometheus" for s in services) and args.deps:
        ensure_prometheus_deps(force=True)


def cmd_status(args) -> None:
    services = resolve_services(getattr(args, "services", []) or [])
    if not getattr(args, "services", None) and SVC["proxy"] not in services:
        services = [SVC["proxy"]] + services
    wait = getattr(args, "wait", 0) or 0
    if wait:
        end = time.time() + wait
        for s in services:
            if s.host and is_running(s.container):
                while time.time() < end and http_status(svc_url(s) + s.health_path) is None:
                    time.sleep(2)
    print(f"{'service':<24}{'container':<24}{'state':<20}{'https':<7}url")
    for s in services:
        status, health = container_state(s.container)
        state = status + (f" ({health})" if health else "")
        col = "32" if status == "running" and health in ("", "healthy") else ("33" if status == "running" else "90")
        code = ""
        if status == "running":
            if s.host:
                if not resolves_local(fqdn(s.host)):
                    code = "dns?"
                else:
                    c = http_status(svc_url(s) + s.health_path)
                    code = str(c) if c else "down"
            elif s.port:
                code = "tcp" if port_open(s.port) else "down"
            elif s.name == "proxy":
                code = "ok" if port_open(443) else "down"
        where = svc_url(s) or (f"localhost:{s.port}" if s.port else "")
        if s.repo in WATCHED and s.name in WATCHED[s.repo] and status == "running":
            where += "   [file sync: " + ("on" if watch_running(s.repo) else "OFF - `p4m watch start`") + "]"
        print(f"{s.name:<24}{s.container:<24}{_c(col, f'{state:<20}')}{code:<7}{where}")


def cmd_logs(args) -> None:
    s = resolve_services([args.service])[0]
    cmd = ["docker", "logs", "--tail", str(args.tail)]
    if args.follow:
        cmd.append("-f")
    run(cmd + [s.container], check=False, quiet=True)


def cmd_shell(args) -> None:
    s = resolve_services([args.service])[0]
    sh = args.cmd or [s.shell]
    tty = ["-it"] if sys.stdin.isatty() else ["-i"]
    rc = subprocess.call(["docker", "exec", *tty, s.container, *sh], env=child_env())
    sys.exit(rc)


def cmd_open(args) -> None:
    for s in resolve_services(args.services or ["prometheus"]):
        if s.host:
            webbrowser.open(svc_url(s) + (s.health_path if s.name == "ares" else ""))
            ok(f"{s.name}: {svc_url(s)}")


def cmd_db(args) -> None:
    ensure_docker(start=False)
    a = args.db_cmd
    if a == "migrate":
        migrate()
    elif a == "prepare-test":
        with test_db_lock():
            prepare_test_db(Path(args.src).resolve() if args.src else None)
    elif a == "psql":
        db = args.name or PG_DB
        tty = ["-it"] if sys.stdin.isatty() else ["-i"]
        sys.exit(subprocess.call(["docker", "exec", *tty, PG_CONTAINER, "psql", "-U", PG_USER, "-d", db]))
    elif a == "dump":
        db = args.name or PG_DB
        target = Path(args.out or f"{db}-{time.strftime('%Y%m%d-%H%M%S')}.sql").resolve()
        info(f"Dumping {db} -> {target}")
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            rc = subprocess.call(["docker", "exec", PG_CONTAINER, "pg_dump", "-U", PG_USER,
                                  "--no-owner", db], stdout=fh)
        if rc:
            raise P4MError("pg_dump failed")
        ok("done")
    elif a == "reset":
        if not args.yes:
            ans = input("This deletes ALL local database data and re-imports the dump. Continue? [y/N] ")
            if ans.strip().lower() not in ("y", "yes"):
                return
        info("Resetting postgres volume")
        compose("gaia", "rm", "-sf", "postgres")
        run(["docker", "volume", "rm", "-f", "gaia_postgres_db"])
        ensure_network()
        compose("gaia", "up", "-d", "postgres")
        wait_for_postgres()
        migrate()
        if args.with_test:
            prepare_test_db()


ARES_PROBE = r"""
import os, sys
try:
    import anthropic
except Exception as e:
    print("ERROR importing anthropic:", e); sys.exit(1)
key = os.getenv("ANTHROPIC_API_KEY", "")
print("ANTHROPIC_API_KEY:", ("set, %d chars, starts %s..." % (len(key), key[:7])) if key else "NOT SET")
print("anthropic SDK:", getattr(anthropic, "__version__", "?"))
model = sys.argv[1]
c = anthropic.Anthropic()
def attempt(label, messages):
    try:
        r = c.messages.create(model=model, max_tokens=30, messages=messages)
        print("OK   ", label, "->", r.model, "|", r.content[0].text.strip()[:60].replace("\n", " "))
        return True
    except Exception as e:
        print("FAIL ", label, "->", type(e).__name__ + ":", str(e)[:500])
        return False
attempt("plain message      ", [{"role": "user", "content": "Reply with the word OK"}])
# ares/planissue ends the conversation with an assistant message (prefill)
attempt("assistant prefill  ", [{"role": "user", "content": "Reply with JSON {\"ok\": true}"},
                                {"role": "assistant", "content": "```json"}])
"""


def ares_probe_output(model: str) -> str:
    return _cap(["docker", "exec", "ares-dev", "python", "-c", ARES_PROBE, model], timeout=90)


def test_db_lock():
    """Exclusive use of pr4m_test (every api run re-seeds it)."""
    try:
        return dblock.held(STATE_DIR / "pr4m_test.lock",
                           on_wait=lambda p: info(f"waiting for another pr4m_test user to finish ({p}, held by "
                                                  f"{dblock.describe_holder(p)}; kill it if it is orphaned)"))
    except dblock.LockTimeout as e:
        raise P4MError(str(e))


def _test_checkout(args, repo: str) -> Path:
    """The checkout a suite runs against: --src, else the workspace's repo."""
    src = Path(getattr(args, "src", None) or ROOT / repo).resolve()
    if not (src / ".git").exists():
        raise P4MError(f"{src} is not a git checkout")
    return src


def _test_container(base: str, src: Path, dest: str, wait_port: int | None = None) -> str:
    """The container to run `src`'s tests in: `base` if it serves `src`, else a throwaway one."""
    try:
        name, note = testenv.BindTestContainer(base, src, dest, wait_port).resolve()
    except testenv.TestEnvError as e:
        raise P4MError(str(e))
    if note:
        info(note)
    return name


def _gaia_test_box(src: Path) -> testenv.GaiaTestContainer:
    """`src`'s own Gaia test container (source copied in, OPcache on, Xdebug off), created if needed."""
    box = testenv.GaiaTestContainer(src, STATE_DIR / "test")
    try:
        if box.ensure():
            info(f"started {box.name} for {src} (source copied in; the first sync copies everything)")
    except testenv.TestEnvError as e:
        raise P4MError(str(e))
    return box


def _sync_gaia(box: testenv.GaiaTestContainer) -> None:
    """Copy `box`'s checkout changes in. Run it after anything on the host that writes the checkout."""
    try:
        changed = box.sync()
    except testenv.TestEnvError as e:
        raise P4MError(str(e))
    box.make_writable()
    info(f"{len(changed)} file(s) synced into {box.name}")


def cmd_test(args) -> None:
    ensure_docker(start=False)
    t = args.test_cmd
    if t == "clean":
        removed = testenv.clean(getattr(args, "src", None))
        ok(f"removed {len(removed)} test container(s)" + (f": {', '.join(removed)}" if removed else ""))
        return
    if t in ("api", "phpunit"):
        src = _test_checkout(args, "gaia")
        fix_eol_in(src, src.name)                 # CRLF scripts break bash in the container
        box = _gaia_test_box(src)
        name = box.name
    if t == "api":
        cmd = ["docker", "exec", "-w", "/var/www/html", name, "bash",
               "tools/api-tester/harness/run-api-tests.sh", "--base-uri", "http://127.0.0.1",
               "--mode", args.mode]
        if "live" in args.mode:
            cmd += ["--hermes-url", f"https://hermes-test.{domain()}"]
        if args.filter:
            cmd += ["--filter", args.filter]
        with test_db_lock():                       # the run (and its seed) owns pr4m_test until it ends
            if args.prepare:
                prepare_test_db(src)               # raises on failure: never test a half-seeded DB
            _sync_gaia(box)                        # after prepare: it writes the fixtures into the checkout
            if args.regenerate_backend:            # backend.json is generated from the metadata and routes
                gen = run(["docker", "exec", "-w", "/var/www/html", name, "php",
                           "tools/api-tester/bin/generate-backend-apis.php"], check=False, capture=True)
                if gen.returncode != 0:
                    raise P4MError("generate-backend-apis.php failed: " + ((gen.stdout or "") + (gen.stderr or ""))[-400:])
                box.fetch("tools/api-tester/apis/backend.json")
                print(next((l for l in (gen.stdout or "").splitlines() if l.startswith("Wrote ")), "").replace(
                    "Wrote ", "regenerated backend.json (").split(" to ")[0] + ")")
            box.clear_reports()
            try:
                rc = run(cmd, check=False).returncode
            finally:
                try:
                    box.fetch_reports()            # `ai test --explain` reads them from the checkout
                except testenv.TestEnvError as e:  # never hide the run's own result
                    warn(f"could not copy the reports back: {e}")
        ok(f"reports in {src / 'output'}") if rc == 0 else err(f"api tests failed - reports in {src / 'output'}")
        sys.exit(rc)
    if t == "phpunit":
        target = args.path or "tests"
        _sync_gaia(box)
        rc = run(["docker", "exec", "-w", "/var/www/html", "-e", "DB_NAME=pr4m_test", "-e", "XDEBUG_MODE=off",
                  name, "./vendor/bin/phpunit", "-c", "tests/phpunit.xml", "--do-not-cache-result", target],
                 check=False).returncode
        sys.exit(rc)
    if t == "ember":
        src = _test_checkout(args, "prometheus")
        box = testenv.PrometheusTestContainer(src, STATE_DIR / "test")
        try:
            if box.ensure(fresh=args.fresh):
                info(f"started {box.name} for {src} (the first build takes a few minutes)")
            before = box.build_count()      # before the copy: a fast rebuild would otherwise be missed
            changed = box.sync()
            built, message = box.build(changed, before)
        except testenv.TestEnvError as e:
            raise P4MError(str(e))
        if not built:
            raise P4MError(message)
        info(f"{len(changed)} file(s) synced, {message}")
        sys.exit(box.run_tests(args.filter))
    if t == "ares":
        if not is_running("ares-dev"):
            raise P4MError("ares is not running - `p4m up ares`")
        info(f"Calling the Anthropic API from inside ares-dev (model {args.model})")
        print(ares_probe_output(args.model))
        return
    if t == "hermes":
        name = _test_container("hermes", _test_checkout(args, "hermes"), "/app")
        rc = run(["docker", "exec", name, "npm", "test"], check=False).returncode
        sys.exit(rc)


def cmd_setup(args) -> None:
    info("Step 1/8  prerequisites")
    if not have("git") or not have("docker"):
        cmd_install(args)
        if not have("git") or not have("docker"):
            raise P4MError("Prerequisites were installed - open a new terminal and run `p4m setup` again.")
    ok("git + docker present")
    ensure_docker()

    info("Step 2/8  repositories")
    if not args.skip_repos:
        repos_sync(pull=not args.no_pull)

    info("Step 3/8  configuration")
    load_env()
    write_overrides()
    ok(f"settings: {ENV_FILE.name}   generated: {STATE_DIR.name}/   domain: {domain()}")
    if not settings().get("ANTHROPIC_API_KEY"):
        warn(f"Add ANTHROPIC_API_KEY to {ENV_FILE.name} to enable ares AI features")

    info("Step 4/8  HTTPS proxy, local CA, hosts file")
    ensure_proxy()
    trust_ca()
    ensure_hosts()

    info("Step 5/8  images")
    for repo in STACK_ORDER[1:]:
        compose(repo, "pull", "--ignore-buildable", "--quiet", check=False)
        compose(repo, "build", *(["--pull"] if args.pull else []))

    info("Step 6/8  frontend dependencies")
    ensure_prometheus_deps(force=args.reinstall)

    info("Step 7/8  database")
    compose("gaia", "up", "-d", "postgres")
    wait_for_postgres()
    migrate()
    try:
        prepare_test_db()
    except P4MError as e:
        warn(f"test DB not prepared: {e}  (retry: `p4m db prepare-test`)")

    info("Step 8/8  start everything")
    cmd_up(argparse.Namespace(services=[], build=False, wait=90))
    ensure_runtime()
    print()
    ok(f"Setup complete. Open https://app.{domain()}  (API: https://api.{domain()})")
    if not (LOCAL_DIR / "workspace.local.json").exists():
        info("Next: `dev-tools/installer/p4m configure` for your settings and the Agent Odysseus credentials")


def cmd_hosts(args) -> None:
    if args.apply:                       # internal: runs elevated
        apply_hosts()
        return
    if args.show:
        print(hosts_block())
        return
    ensure_hosts()


def cmd_configure(args) -> int:
    """Per-engineer settings and credentials (see configure.py)."""
    sys.path.insert(0, str(INSTALLER_DIR))
    import configure  # noqa: E402  (dev-tools/installer/configure.py)
    rc = configure.main(args.rest)
    if not rc and not {"-h", "--help"} & set(args.rest):
        ensure_runtime()
    return rc


def ensure_runtime() -> None:
    """Create the runtime copy of dev-tools if it is missing; a failure is a warning (the tools still run from the
    checkout), with the command to retry."""
    info("Runtime copy of dev-tools (Talos and the agents' tooling run from it)")
    try:
        runtime.setup(LOCAL_DIR, ROOT, say=ok)
    except (runtime.RuntimeCopyError, OSError) as e:
        warn(f"runtime copy not created: {e}  (retry: `p4m runtime setup`)")


def cmd_runtime(args) -> None:
    """The runtime copy of dev-tools: setup, update (fast-forward to origin/main), status. See runtime.py."""
    try:
        if args.action == "setup":
            runtime.setup(LOCAL_DIR, ROOT, say=ok)
        elif args.action == "update":
            runtime.update(ROOT, LOCAL_DIR, force=args.force, say=ok)
        else:
            runtime.status(ROOT, LOCAL_DIR, say=print)
    except runtime.RuntimeCopyError as e:
        raise P4MError(str(e)) from e


def cmd_trust(args) -> None:
    ensure_docker()
    ensure_proxy()
    trust_ca()


def cmd_urls(args) -> None:
    D = domain()
    print(f"Domain: {D}   (P4M_DOMAIN in {ENV_FILE.name})\n")
    print(f"{'url':<48}{'service':<24}upstream")
    for s in SERVICES:
        if s.host:
            print(f"{svc_url(s):<48}{s.name:<24}{s.upstream}")
    print(f"{'postgres://localhost:5432':<48}{'postgres':<24}{PG_CONTAINER}:5432")
    print("\nEnvironment passed to each service:")
    for svc, env in url_env().items():
        for k, v in env.items():
            print(f"  {svc:<24}{k:<18}{v}")
    print("\nInside containers the same hostnames resolve to the proxy (docker network aliases).")


def cmd_repos(args) -> None:
    if args.repos_cmd == "status":
        repos_status()
    elif args.repos_cmd == "sync":
        repos_sync(pull=not args.no_pull)
    elif args.repos_cmd == "fix-eol":
        fix_eol()


def cmd_services(args) -> None:
    print(f"{'service':<24}{'group':<8}{'repo':<12}{'url':<48}description")
    for s in SERVICES:
        where = svc_url(s) or (f"localhost:{s.port}" if s.port else "80/443")
        print(f"{s.name:<24}{s.group:<8}{s.repo:<12}{where:<48}{s.desc}")


# --------------------------------------------------------------------------- #
# Housekeeping and diagnostics
# --------------------------------------------------------------------------- #

LEGACY_FILES = ["p4m.py", "p4m.cmd", "p4m", "P4M-CLI.md"]  # new copies live in dev-tools/installer/


def migrate_legacy_layout() -> None:
    """Earlier versions lived in the workspace root. Move settings into dev-tools/ and remove
    the old copies (new versions of all of them live in dev-tools/)."""
    if TOOLS_DIR == ROOT or not (ROOT / "p4m.py").exists() and not (ROOT / "p4m.env").exists() \
            and not (ROOT / ".p4m").exists():
        return
    import shutil as _sh
    moved = []
    old_env = ROOT / "p4m.env"
    if old_env.exists():
        if not ENV_FILE.exists():
            _sh.move(str(old_env), str(ENV_FILE)); moved.append("p4m.env -> dev-tools/p4m.env")
        else:
            old_env.unlink(); moved.append("removed old p4m.env (dev-tools/p4m.env exists)")
    old_ps1 = ROOT / "setup-repos.ps1"
    if old_ps1.exists():                 # superseded by `p4m repos sync`
        old_ps1.unlink(); moved.append("removed old setup-repos.ps1 (use `p4m repos sync`)")
    if (ROOT / ".p4m").is_dir():
        _sh.rmtree(ROOT / ".p4m", ignore_errors=True); moved.append("removed old .p4m/ (regenerated)")
    for name in LEGACY_FILES:
        f = ROOT / name
        if f.exists() and (INSTALLER_DIR / name).exists():
            f.unlink(); moved.append(f"removed old {name}")
    if moved:
        info("Moved p4m into dev-tools/")
        for m in moved:
            ok(m)


def _cap(cmd: list[str], timeout: int = 30) -> str:
    try:
        cp = subprocess.run(cmd, env=child_env(), capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout)
        return (cp.stdout or "") + (cp.stderr or "") + (f"[exit {cp.returncode}]" if cp.returncode else "")
    except subprocess.TimeoutExpired:
        return f"[timed out after {timeout}s]"
    except FileNotFoundError:
        return f"[not found: {cmd[0]}]"


def cmd_diag(args) -> None:
    """Collect everything needed to debug the stack into one text file."""
    import contextlib
    import io
    LOG_DIR.mkdir(exist_ok=True)
    target = LOG_DIR / f"diag-{time.strftime('%Y%m%d-%H%M%S')}.txt"
    focus = resolve_services(args.services) if args.services else list(SERVICES)
    buf: list[str] = []

    def section(title: str, body: str) -> None:
        buf.append(f"\n{'=' * 78}\n## {title}\n{'=' * 78}\n{body.rstrip()}\n")
        print(_c("36", "==> ") + title, flush=True)

    def captured(fn, *a) -> str:
        f = io.StringIO()
        global _COLOR
        prev, _COLOR = _COLOR, False
        try:
            with contextlib.redirect_stdout(f), contextlib.redirect_stderr(f):
                try:
                    fn(*a)
                except (P4MError, SystemExit) as e:
                    print(f"[{e}]")
        finally:
            _COLOR = prev
        return f.getvalue()

    section("environment", f"time: {time.ctime()}\nplatform: {platform.platform()}\npython: {sys.version}\n"
            f"tools dir: {TOOLS_DIR}\nworkspace: {ROOT}\ndomain: {domain()}\n"
            + _cap(["docker", "version", "--format", "client {{.Client.Version}} / server {{.Server.Version}}"])
            + "\n" + _cap(["docker", "compose", "version"]))
    section("doctor", captured(cmd_doctor, argparse.Namespace()))
    section("status", captured(cmd_status, argparse.Namespace(services=[], wait=0)))
    section("docker ps -a", _cap(["docker", "ps", "-a", "--format",
                                  "table {{.Names}}\t{{.Status}}\t{{.Image}}\t{{.Ports}}"]))
    section("hosts file p4m block", hosts_current().replace("\r\n", "\n").split(HOSTS_BEGIN)[-1][:2000]
            if HOSTS_BEGIN in hosts_current() else "[no p4m block]")
    section("generated Caddyfile", (PROXY_DIR / "Caddyfile").read_text() if (PROXY_DIR / "Caddyfile").exists() else "[missing]")
    section("proxy logs (last 150)", _cap(["docker", "logs", "--tail", "150", PROXY_CONTAINER]))

    for s in focus:
        if s.name == "proxy":
            continue
        state, health = container_state(s.container)
        body = f"state: {state} {health}\n"
        if state != "missing":
            body += _cap(["docker", "inspect", "-f",
                          "started={{.State.StartedAt}} exit={{.State.ExitCode}} restarts={{.RestartCount}} "
                          "cmd={{json .Config.Cmd}}", s.container]) + "\n"
            if s.upstream and is_running(PROXY_CONTAINER):
                host, port = s.upstream.split(":")
                body += "-- proxy -> upstream probe:\n" + _cap(
                    ["docker", "exec", PROXY_CONTAINER, "wget", "-S", "-T", "15", "-O", "/dev/null",
                     f"http://{host}:{port}{s.health_path}"], timeout=25) + "\n"
            if s.host and is_running(PROXY_CONTAINER):
                body += "-- full https path inside docker (alias -> proxy -> upstream):\n" + _cap(
                    ["docker", "exec", PROXY_CONTAINER, "wget", "-S", "-T", "15", "-O", "/dev/null",
                     "--no-check-certificate", f"{svc_url(s)}{s.health_path}"], timeout=25) + "\n"
            if s.host:
                body += f"-- from Windows: {svc_url(s)}{s.health_path} -> {http_status(svc_url(s) + s.health_path, timeout=15)}"
                body += f"  (resolves to 127.0.0.1: {resolves_local(fqdn(s.host))})\n"
            if s.name == "prometheus" and state == "running":
                body += "-- processes:\n" + _cap(["docker", "exec", s.container, "sh", "-c",
                                                 "ps aux 2>/dev/null || ps"]) + "\n"
                body += "-- node/ember versions:\n" + _cap(["docker", "exec", s.container, "sh", "-c",
                                                          "node -v; npm -v; ls node_modules | wc -l; cat node_modules/.p4m-deps-stamp 2>/dev/null"]) + "\n"
                body += "-- listening sockets:\n" + _cap(["docker", "exec", s.container, "sh", "-c",
                                                        "cat /proc/net/tcp /proc/net/tcp6 2>/dev/null | awk 'NR>1 && $4==\"0A\"{print $2}'"]) + "\n"
            if state == "running" and s.name in url_env():
                # call every URL this service is configured with, from inside the container
                body += "-- outbound URLs from inside the container:\n"
                for var, val in url_env()[s.name].items():
                    if not val.startswith("http"):
                        continue
                    probe = val.rstrip("/") + ("/docs" if "ares." in val else
                                               "/socket.io/?EIO=4&transport=polling" if "hermes" in val else "/")
                    res = _cap(["docker", "exec", s.container, "sh", "-c",
                                f"if command -v curl >/dev/null; then curl -sS -o /dev/null -w '%{{http_code}}' "
                                f"--max-time 15 '{probe}' 2>&1; else wget -q -T 15 -O /dev/null '{probe}' 2>&1 "
                                f"&& echo ok; fi"], timeout=25)
                    body += f"   {var:<16} {probe} -> {res.strip()}\n"
                body += "-- configured URL env inside the container:\n" + _cap(
                    ["docker", "exec", s.container, "sh", "-c",
                     "env | grep -E '" + "|".join(url_env()[s.name]) + "' | sort"]) + "\n"
            if state == "running" and s.name == "ares":
                body += "-- Anthropic API probe (model hard-coded in ares):\n" + ares_probe_output(
                    "claude-sonnet-4-20250514") + "\n"
            if state == "running" and s.repo == "gaia" and s.name in url_env():
                body += "-- CA trust inside the container:\n" + _cap(["docker", "exec", s.container, "sh", "-c",
                    "ls -la /etc/ssl/p4m /etc/ssl/p4m/php 2>&1; grep -c 'BEGIN CERT' /etc/ssl/p4m/ca-bundle.crt; "
                    "php -r 'echo ini_get(\"curl.cainfo\"),\" | \",ini_get(\"openssl.cafile\"),PHP_EOL;'; "
                    "php -r 'print_r(openssl_get_cert_locations());' | head -8"]) + "\n"
            if s.repo in WATCHED:
                wl = LOG_DIR / f"watch-{s.repo}.log"
                body += f"-- file sync: {'running' if watch_running(s.repo) else 'stopped'}; log tail:\n"
                body += ("\n".join(wl.read_text(errors="replace").splitlines()[-40:]) if wl.exists() else "[no log]") + "\n"
            body += f"-- logs (last {args.lines}):\n" + _cap(["docker", "logs", "--tail", str(args.lines), s.container])
        section(f"service {s.name} ({s.container})", body)

    text = "".join(buf)
    target.write_text(text, encoding="utf-8")
    print()
    ok(f"report written: {target}")




def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="p4m", description="Projects4Me local development CLI",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="Service names: " + ", ".join(SVC) +
                                       "\nGroups: all, core, test, tools, or a repo name (gaia, hermes, ...)")
    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    s = sub.add_parser("doctor", help="check prerequisites, repos, docker, ports"); s.set_defaults(fn=cmd_doctor)
    s = sub.add_parser("install", help="install missing prerequisites (git, docker)"); s.set_defaults(fn=cmd_install)

    s = sub.add_parser("setup", help="full first-time setup, then start everything")
    s.add_argument("--skip-repos", action="store_true", help="don't clone/update repositories")
    s.add_argument("--no-pull", action="store_true", help="don't git pull existing repos")
    s.add_argument("--pull", action="store_true", help="pull newer base images when building")
    s.add_argument("--reinstall", action="store_true", help="reinstall prometheus node_modules")
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("up", help="start services (default: all)")
    s.add_argument("services", nargs="*")
    s.add_argument("--build", action="store_true", help="rebuild images first")
    s.add_argument("--wait", type=int, default=60, metavar="SEC", help="wait for HTTP before status (default 60)")
    s.set_defaults(fn=cmd_up)

    s = sub.add_parser("down", help="stop services (no args: whole stack)")
    s.add_argument("services", nargs="*")
    s.add_argument("-v", "--volumes", action="store_true", help="also delete volumes (DATABASE!)")
    s.set_defaults(fn=cmd_down)

    s = sub.add_parser("restart", help="restart services"); s.add_argument("services", nargs="*"); s.set_defaults(fn=cmd_restart)

    s = sub.add_parser("build", help="rebuild images")
    s.add_argument("services", nargs="*")
    s.add_argument("--pull", action="store_true")
    s.add_argument("--deps", action="store_true", help="also reinstall prometheus node_modules")
    s.set_defaults(fn=cmd_build)

    s = sub.add_parser("status", aliases=["ps"], help="show service state and health")
    s.add_argument("services", nargs="*"); s.set_defaults(fn=cmd_status, wait=0)

    s = sub.add_parser("services", help="list known services"); s.set_defaults(fn=cmd_services)
    s = sub.add_parser("urls", help="show the URL map and the env vars passed to services"); s.set_defaults(fn=cmd_urls)

    s = sub.add_parser("hosts", help="add *.P4M_DOMAIN entries to the hosts file (admin prompt)")
    s.add_argument("--show", action="store_true", help="only print the block")
    s.add_argument("--apply", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_hosts)

    s = sub.add_parser("trust", help="trust the local HTTPS CA (Windows, macOS, Ubuntu/Debian)"); s.set_defaults(fn=cmd_trust)
    s = sub.add_parser("configure", help="your settings + the shared Agent Odysseus credentials (re-runnable)",
                       add_help=False)
    s.add_argument("rest", nargs=argparse.REMAINDER); s.set_defaults(fn=cmd_configure)

    s = sub.add_parser("runtime", help="the runtime copy of dev-tools Talos and the agents run from: "
                                       "setup|update|status")
    s.add_argument("action", choices=["setup", "update", "status"])
    s.add_argument("--force", action="store_true", help="update even if talos.db lists an active run (stale)")
    s.set_defaults(fn=cmd_runtime)

    s = sub.add_parser("watch", help="file sync into containers (prometheus): start|stop|status")
    s.add_argument("action", choices=["start", "stop", "status"])
    s.add_argument("repo", nargs="?", choices=list(WATCHED))
    s.set_defaults(fn=cmd_watch)

    s = sub.add_parser("diag", help="write a diagnostics report to dev-tools/logs/")
    s.add_argument("services", nargs="*", help="limit service sections (default: all)")
    s.add_argument("-n", "--lines", type=int, default=300, help="log lines per service")
    s.set_defaults(fn=cmd_diag)

    s = sub.add_parser("logs", help="show container logs")
    s.add_argument("service"); s.add_argument("-f", "--follow", action="store_true")
    s.add_argument("-n", "--tail", type=int, default=200)
    s.set_defaults(fn=cmd_logs)

    s = sub.add_parser("shell", aliases=["exec"], help="shell (or command) inside a service container")
    s.add_argument("service"); s.add_argument("cmd", nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_shell)

    s = sub.add_parser("open", help="open service URLs in the browser")
    s.add_argument("services", nargs="*"); s.set_defaults(fn=cmd_open)

    s = sub.add_parser("db", help="database tasks")
    dsub = s.add_subparsers(dest="db_cmd", required=True, metavar="<task>")
    dsub.add_parser("migrate", help="php cli.php migration migrate (pr4m)")
    d = dsub.add_parser("prepare-test", help="create + seed pr4m_test for api tests")
    d.add_argument("--src", help="gaia checkout whose fixtures to load (default: the workspace's)")
    d = dsub.add_parser("psql", help="interactive psql"); d.add_argument("name", nargs="?")
    d = dsub.add_parser("dump", help="pg_dump to a file"); d.add_argument("name", nargs="?"); d.add_argument("-o", "--out")
    d = dsub.add_parser("reset", help="wipe DB volume, re-import dump, migrate")
    d.add_argument("-y", "--yes", action="store_true"); d.add_argument("--with-test", action="store_true")
    s.set_defaults(fn=cmd_db)

    s = sub.add_parser("test", help="run test suites")
    tsub = s.add_subparsers(dest="test_cmd", required=True, metavar="<suite>")
    src_help = "checkout to test (default: the workspace's repo); a throwaway container is used when the " \
               "shared one serves another folder"
    t = tsub.add_parser("api", help="gaia api-tester against gaia-test")
    t.add_argument("--mode", default="backend", help="client|backend|acl|live (comma separated)")
    t.add_argument("--filter")
    t.add_argument("--src", help=src_help)
    t.add_argument("--regenerate-backend", action="store_true",
                   help="regenerate api-tester backend.json from the metadata and routes first")
    t.add_argument("--prepare", action="store_true", help="seed pr4m_test from --src first (under the same lock)")
    t = tsub.add_parser("phpunit", help="gaia phpunit"); t.add_argument("path", nargs="?")
    t.add_argument("--src", help=src_help)
    t = tsub.add_parser("ember", help="prometheus ember tests (source copied in, incremental test build)")
    t.add_argument("--filter", help="QUnit filter: text, or /regex/")
    t.add_argument("--src", help="checkout to test (default: the workspace's prometheus)")
    t.add_argument("--fresh", action="store_true", help="recreate the test container (full rebuild)")
    t = tsub.add_parser("hermes", help="hermes node --test"); t.add_argument("--src", help=src_help)
    t = tsub.add_parser("clean", help="remove the throwaway test containers")
    t.add_argument("--src", help="only the containers for this checkout")
    t = tsub.add_parser("ares", help="call the Anthropic API from inside ares (shows the real error)")
    t.add_argument("--model", default="claude-sonnet-4-20250514",
                   help="model id to try (default: the one hard-coded in ares)")
    s.set_defaults(fn=cmd_test)

    s = sub.add_parser("repos", help="git repositories")
    rsub = s.add_subparsers(dest="repos_cmd", required=True, metavar="<task>")
    rsub.add_parser("status", help="branch / dirty / ahead-behind per repo")
    r = rsub.add_parser("sync", help="clone missing, checkout expected branch, pull")
    r.add_argument("--no-pull", action="store_true")
    rsub.add_parser("fix-eol", help="convert CRLF shell scripts to LF")
    s.set_defaults(fn=cmd_repos)
    return p


def main(argv: list[str] | None = None) -> int:
    # Piped output on Windows defaults to cp1252, which can't print ✓/✗ and crashed the command.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["configure"]:        # configure.py owns its own options
        return cmd_configure(argparse.Namespace(rest=argv[1:])) or 0
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        parser.print_help()
        return 0
    try:
        migrate_legacy_layout()
        rc = args.fn(args)
        return rc or 0
    except P4MError as e:
        err(str(e))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
