"""`p4m configure`: per-engineer settings and credentials for the Pr4M workspace tooling.

Asks once (re-running offers the previous answers as defaults) and then:
  - writes the shared Agent Odysseus credentials: gaia-mcp/gaia-mcp.env and odysseus/odysseus.env,
  - installs the agent's SSH key (~/.ssh/agent_odysseus), the `github-odysseus` host alias and the
    `odysseus` git remote in every repo,
  - writes the engineer's own settings: ai/config.local.json (reviewer, timezone label),
    talos/config.local.json (Hermes URL) and talos/agents/odysseus/agent.local.json (Talos acts only on
    issues this engineer assigns, and hands work back to them),
  - registers the gaia MCP server with Claude Code and installs Talos' listener dependency,
  - checks each piece and reports what works.

Non-secret answers are kept in dev-tools/workspace.local.json; secrets only in the git-ignored env files.
Pure standard library; works on Windows (Git Bash / PowerShell), macOS and Linux.
"""

import getpass
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

INSTALLER_DIR = Path(__file__).resolve().parent
TOOLS_DIR = INSTALLER_DIR.parent                 # <workspace>/dev-tools
ROOT = TOOLS_DIR.parent                          # <workspace> (Pr4M)
ANSWERS = TOOLS_DIR / "workspace.local.json"
GAIA_ENV = TOOLS_DIR / "gaia-mcp" / "gaia-mcp.env"
ODYSSEUS_ENV = TOOLS_DIR / "odysseus" / "odysseus.env"
AI_LOCAL = TOOLS_DIR / "ai" / "config.local.json"
TALOS_LOCAL = TOOLS_DIR / "talos" / "config.local.json"
AGENT_LOCAL = TOOLS_DIR / "talos" / "agents" / "odysseus" / "agent.local.json"
SSH_DIR = Path.home() / ".ssh"
AGENT_KEY = SSH_DIR / "agent_odysseus"
SSH_HOST = "github-odysseus"
GITHUB_ORG = "projects4me"
REPOS = {"gaia": "gaia", "prometheus": "prometheus", "hermes": "hermes", "ares": "ares",
         "docker-images": "docker-images", "dev-tools": "dev-tools", ".": "Pr4M"}

SECRET = object()   # marker: prompt without echo, never stored in workspace.local.json


def _say(msg=""):
    print(msg, flush=True)


def _ok(msg):
    _say(f"  ok    {msg}")


def _warn(msg):
    _say(f"  WARN  {msg}")


def read_env(path):
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def write_private(path, text):
    """Write a file only its owner can read (chmod 600 where the OS supports it)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def ask(label, default=None, kind=None, answers=None, key=None):
    """One answer: from --answers if given, else prompted (secrets without echo, defaults kept on Enter)."""
    if answers is not None and key in answers:
        return answers[key]
    # With --answers (or no terminal) nothing is prompted: missing keys take their defaults.
    # (On Windows the NUL device reports isatty() True, so --answers alone decides there.)
    if answers is not None or not sys.stdin.isatty():
        if default is None:
            raise SystemExit(f"p4m configure: '{key}' is required (add it to --answers)")
        return default
    shown = "(keep current)" if kind is SECRET and default else (default or "")
    prompt = f"  {label}" + (f" [{shown}]" if shown else "") + ": "
    try:
        value = getpass.getpass(prompt) if kind is SECRET else input(prompt)
    except EOFError:
        value = ""
    value = value.strip()
    if not value and default is None:
        raise SystemExit(f"p4m configure: '{key}' is required")
    return value or default


def local_domain():
    env = read_env(TOOLS_DIR / "p4m.env")
    return env.get("P4M_DOMAIN") or "local-projects4.me"


def collect(previous, answers):
    """All answers, with previous runs and existing files as defaults."""
    gaia_env, ody_env = read_env(GAIA_ENV), read_env(ODYSSEUS_ENV)
    dom = local_domain()
    a = {}
    _say("\nYou (the engineer this copy of the tools works for)")
    a["name"] = ask("Your name", previous.get("name"), answers=answers, key="name")
    a["email"] = ask("Your Projects4Me login email", previous.get("email"), answers=answers, key="email")
    a["timezoneLabel"] = ask("Timezone label for time logs (e.g. PKT)", previous.get("timezoneLabel", "UTC"),
                             answers=answers, key="timezoneLabel")

    _say("\nProjects4Me services the tools talk to (local p4m stack by default; use the live URLs for live)")
    a["gaiaUrl"] = ask("Gaia API URL", gaia_env.get("GAIA_URL") or previous.get("gaiaUrl") or f"https://api.{dom}",
                       answers=answers, key="gaiaUrl")
    a["appUrl"] = ask("Prometheus app URL", gaia_env.get("GAIA_UI_URL") or previous.get("appUrl") or f"https://app.{dom}",
                      answers=answers, key="appUrl")
    a["hermesUrl"] = ask("Hermes URL", previous.get("hermesUrl") or f"https://hermes.{dom}",
                         answers=answers, key="hermesUrl")
    a["aresUrl"] = ask("Ares URL", previous.get("aresUrl") or f"https://ares.{dom}", answers=answers, key="aresUrl")
    a["clientId"] = ask("OAuth client id", gaia_env.get("GAIA_CLIENT_ID") or "projects4me",
                        answers=answers, key="clientId")
    a["clientSecret"] = ask("OAuth client secret (prometheus/config/environment.js)", gaia_env.get("GAIA_CLIENT_SECRET"),
                            SECRET, answers, "clientSecret")

    _say("\nAgent Odysseus (the team's shared agent identity; the team lead shares these)")
    a["agentEmail"] = ask("Agent Projects4Me email", gaia_env.get("GAIA_EMAIL") or "agent.odysseus@projects4.me",
                          answers=answers, key="agentEmail")
    a["agentPassword"] = ask("Agent Projects4Me password", gaia_env.get("GAIA_PASSWORD"), SECRET, answers, "agentPassword")
    a["ghToken"] = ask("Agent GitHub token (fine-grained)", ody_env.get("GH_TOKEN"), SECRET, answers, "ghToken")
    have_key = AGENT_KEY.exists()
    a["sshKey"] = ask("Path to the agent's SSH private key" + (" (Enter keeps the installed one)" if have_key else ""),
                      "" if have_key else None, answers=answers, key="sshKey")
    return a


# -- steps -----------------------------------------------------------------------------------

def write_credentials(a):
    write_private(GAIA_ENV, "\n".join([
        "# Written by `dev-tools/installer/p4m configure` (shared Agent Odysseus credentials). Git-ignored.",
        f"GAIA_URL={a['gaiaUrl']}", f"GAIA_UI_URL={a['appUrl']}",
        f"GAIA_EMAIL={a['agentEmail']}", f"GAIA_PASSWORD={a['agentPassword']}",
        f"GAIA_CLIENT_ID={a['clientId']}", f"GAIA_CLIENT_SECRET={a['clientSecret']}", ""]))
    _ok(f"gaia MCP credentials -> {GAIA_ENV.relative_to(TOOLS_DIR)}")
    keep = {k: v for k, v in read_env(ODYSSEUS_ENV).items() if k != "GH_TOKEN"}
    lines = ["# Written by `dev-tools/installer/p4m configure` (shared Agent Odysseus). Git-ignored.",
             f"GH_TOKEN={a['ghToken']}"] + [f"{k}={v}" for k, v in keep.items()]
    write_private(ODYSSEUS_ENV, "\n".join(lines) + "\n")
    _ok(f"GitHub token -> {ODYSSEUS_ENV.relative_to(TOOLS_DIR)}")


def install_ssh_key(a):
    SSH_DIR.mkdir(mode=0o700, exist_ok=True)
    src = a.get("sshKey")
    if src:
        src = Path(os.path.expanduser(src))
        if not src.exists():
            raise SystemExit(f"p4m configure: SSH key {src} not found")
        if src.resolve() != AGENT_KEY.resolve():
            shutil.copyfile(src, AGENT_KEY)
            src_pub = Path(str(src) + ".pub")
            if src_pub.exists():
                shutil.copyfile(src_pub, Path(str(AGENT_KEY) + ".pub"))
    if not AGENT_KEY.exists():
        raise SystemExit("p4m configure: the agent's SSH key is required")
    try:
        os.chmod(AGENT_KEY, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    pub = Path(str(AGENT_KEY) + ".pub")
    if not pub.exists() and shutil.which("ssh-keygen"):
        out = subprocess.run(["ssh-keygen", "-y", "-f", str(AGENT_KEY)], capture_output=True, text=True)
        if out.returncode == 0:
            pub.write_text(out.stdout, encoding="utf-8")
    _ok(f"agent SSH key at {AGENT_KEY}" + ("" if pub.exists() else " (WARN: no .pub; commit signing needs it)"))

    config = SSH_DIR / "config"
    text = config.read_text(encoding="utf-8") if config.exists() else ""
    if not re.search(rf"^\s*Host\s+{SSH_HOST}\s*$", text, re.M):
        block = (f"\n# Agent Odysseus (added by p4m configure)\nHost {SSH_HOST}\n  HostName github.com\n  User git\n"
                 f"  IdentityFile ~/.ssh/agent_odysseus\n  IdentitiesOnly yes\n")
        config.write_text(text.rstrip("\n") + "\n" + block, encoding="utf-8")
        _ok(f"ssh host alias '{SSH_HOST}' added to {config}")
    else:
        _ok(f"ssh host alias '{SSH_HOST}' already in {config}")


def add_remotes():
    for folder, repo in REPOS.items():
        path = ROOT / folder if folder != "." else ROOT
        if not (path / ".git").exists():
            continue
        url = f"git@{SSH_HOST}:{GITHUB_ORG}/{repo}.git"
        have = subprocess.run(["git", "-C", str(path), "remote", "get-url", "odysseus"], capture_output=True, text=True)
        cmd = ["set-url", "odysseus", url] if have.returncode == 0 else ["add", "odysseus", url]
        subprocess.run(["git", "-C", str(path), "remote", *cmd], check=True)
        _ok(f"remote odysseus -> {url}  ({repo})")


def gaia_client(a):
    sys.path.insert(0, str(TOOLS_DIR / "gaia-mcp"))
    import server  # noqa: E402  (dev-tools/gaia-mcp/server.py)
    g = server.Gaia()
    g.base, g.email, g.password = a["gaiaUrl"].rstrip("/"), a["agentEmail"], a["agentPassword"]
    g.client_id, g.client_secret = a["clientId"], a["clientSecret"]
    return server, g


def resolve_engineer(a):
    """The engineer's Gaia user id, found by email while signed in as the agent."""
    server, g = gaia_client(a)
    if not re.fullmatch(r"[\w.+-]+@[\w.-]+", a["email"]):
        raise SystemExit(f"p4m configure: '{a['email']}' doesn't look like an email address")
    try:
        users = g.find("user", f"(User.email : {a['email']})", limit=1)
    except server.GaiaError as e:
        raise SystemExit(f"p4m configure: could not sign in to {a['gaiaUrl']} as {a['agentEmail']}: {e}")
    if not users:
        raise SystemExit(f"p4m configure: no Projects4Me user with email {a['email']} on {a['gaiaUrl']}")
    _ok(f"you are Gaia user {users[0]['id']} ({users[0].get('name')}); signed in as {a['agentEmail']}")
    return str(users[0]["id"])


def write_engineer_settings(a, user_id):
    write_json(AI_LOCAL, {"reviewer": a["name"], "timezoneLabel": a["timezoneLabel"]})
    _ok(f"PR reviewer and time-log timezone -> {AI_LOCAL.relative_to(TOOLS_DIR)}")
    talos = json.loads(TALOS_LOCAL.read_text(encoding="utf-8")) if TALOS_LOCAL.exists() else {}
    talos["hermesUrl"] = a["hermesUrl"]
    write_json(TALOS_LOCAL, talos)
    write_json(AGENT_LOCAL, {"acceptAssignmentsFrom": [user_id], "handoffTo": user_id, "reviewerName": a["name"]})
    _ok(f"Talos acts on issues you assign and hands back to you -> {AGENT_LOCAL.relative_to(TOOLS_DIR)}")


def register_mcp():
    claude = shutil.which("claude")
    server = TOOLS_DIR / "gaia-mcp" / "server.py"
    cmd = ["claude", "mcp", "add", "--scope", "user", "gaia", "--", sys.executable, str(server)]
    if not claude:
        _warn("Claude Code CLI not on PATH; register the gaia MCP server yourself:\n        " + " ".join(cmd))
        return
    if subprocess.run([claude, "mcp", "get", "gaia"], capture_output=True).returncode == 0:
        _ok("gaia MCP server already registered with Claude Code (`claude mcp remove gaia -s user` to redo)")
        return
    subprocess.run([claude, *cmd[1:]], check=False)
    _ok("gaia MCP server registered with Claude Code (restart Claude Code to load it)")


def install_talos_deps():
    node, npm = shutil.which("node"), shutil.which("npm")
    if not node or not npm:
        _warn("Node.js not found: Talos needs Node >= 22.15 (for --use-system-ca) and npm")
        return
    ver = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip().lstrip("v")
    major, minor = (int(x) for x in ver.split(".")[:2])
    if (major, minor) < (22, 15):
        _warn(f"Node {ver} is too old for Talos (needs >= 22.15 for --use-system-ca)")
    subprocess.run([npm, "install", "--no-audit", "--no-fund", "--silent"], cwd=TOOLS_DIR / "talos", check=False,
                   shell=(os.name == "nt"))
    _ok(f"Talos listener dependency installed (node {ver})")


def verify(a):
    _say("\nChecks")
    ody = TOOLS_DIR / "odysseus" / "odysseus"
    bash = shutil.which("bash")
    if bash:
        r = subprocess.run([bash, str(ody), "gh", "api", "user", "--jq", ".login"], capture_output=True, text=True)
        (_ok if r.returncode == 0 else _warn)(f"GitHub API as {r.stdout.strip() or 'agent'}: "
                                              f"{'works' if r.returncode == 0 else (r.stderr.strip() or 'failed')[:200]}")
    else:
        _warn("bash not found; the odysseus wrapper needs bash (Git Bash on Windows)")
    if shutil.which("ssh"):
        r = subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                            f"git@{SSH_HOST}"], capture_output=True, text=True, timeout=30)
        good = "successfully authenticated" in (r.stdout + r.stderr)
        (_ok if good else _warn)(f"SSH to GitHub as the agent: {'works' if good else (r.stderr.strip() or 'failed')[:200]}")


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(prog="p4m configure", description=__doc__.splitlines()[0])
    p.add_argument("--answers", help="JSON file with answers (keys as in workspace.local.json plus "
                                     "clientSecret, agentPassword, ghToken, sshKey) for a non-interactive run")
    p.add_argument("--skip-checks", action="store_true", help="don't verify GitHub/SSH access at the end")
    args = p.parse_args(argv)
    answers = json.loads(Path(args.answers).read_text(encoding="utf-8")) if args.answers else None
    previous = json.loads(ANSWERS.read_text(encoding="utf-8")) if ANSWERS.exists() else {}

    _say("p4m configure: per-engineer settings for the Pr4M workspace (Enter keeps the value in brackets)")
    a = collect(previous, answers)
    _say("\nSetting up")
    write_credentials(a)
    install_ssh_key(a)
    add_remotes()
    user_id = resolve_engineer(a)
    write_engineer_settings(a, user_id)
    register_mcp()
    install_talos_deps()
    write_json(ANSWERS, {k: v for k, v in a.items() if k not in ("clientSecret", "agentPassword", "ghToken", "sshKey")}
               | {"gaiaUserId": user_id})
    if not args.skip_checks:
        verify(a)
    _say(f"\nDone. Answers kept in {ANSWERS.relative_to(ROOT)} (no secrets). Re-run any time to change them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
