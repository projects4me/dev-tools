"""ContainerRunner: an agent's Claude session in a container on a Docker host, instead of on this machine.

The image (talos/container/Dockerfile) holds the Claude CLI, node, python, git, gh and the docker CLI. Its entrypoint
(talos/container/entrypoint.py) clones the workspace, the dev-tools runtime and the issue's repo into the issue's
work volume, then runs the `claude -p` argv with the prompt on stdin, so stdout carries the same JSON result as a
local run. Talos stays the coordinator: it starts `docker run` from here and reads that result.

Credentials are never in the image or on the docker command line: `docker run -e NAME` copies NAME from the docker
CLI's environment, which Talos fills from the agent's environment and the credential files; key files are mounted
read-only.
"""

import os
import subprocess
from pathlib import Path

from . import runner
from .config import CONTAINER_DIR, LOCAL_DIR, TalosError

WORK = "/work"                                   # the issue's work volume: workspace, runtime and repo clones
CLAUDE_HOME = "/root/.claude"
SECRETS = "/run/secrets/talos"                   # key files, mounted read-only
MCP = WORK + "/.talos/mcp.json"                  # written by the entrypoint: the gaia server from the runtime clone
BASE_BRANCH = {"dev-tools": "main"}
CLAUDE_CREDENTIALS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
ENV_FILES = ("odysseus/odysseus.env", "talos/container.env")   # under <workspace>/dev-tools; missing ones are skipped
FILES = {"ODYSSEUS_SSH_KEY": "~/.ssh/agent_odysseus", "GAIA_CA_FILE": None}   # variable -> default host path
CLAUDE_HOMES = ("bind", "volume", "none")
GITHUB_TOKEN = "GH_TOKEN"                        # only from the credential files: always Odysseus's


def read_env_file(path):
    """KEY=VALUE lines (blank lines, # comments and `export ` prefixes allowed; quotes stripped)."""
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def volume(agent_name, issue, what):
    return f"talos-{agent_name}-{issue}-{what}"


def container_name(agent_name, issue):
    return f"talos-{agent_name}-{issue}"


def claude_home(agent_name, issue):
    """The host folder bind-mounted as the container's ~/.claude (claudeHome "bind"): sessions resume and
    runner.transcript finds the transcripts for the run stats and `talos status`."""
    return CONTAINER_DIR / f"{agent_name}-{issue}" / "claude"


class ContainerRunner(runner.Runner):
    """agent.json `runner`: {"type": "container", "image": ..., and optionally
    "dockerHost" (the Docker host to run on; default the docker CLI's own), "network", "addHosts" (["host:ip"]),
    "claudeHome" ("bind" a host folder, default; "volume" a named volume, for a remote Docker host; "none": no
    sessions kept, every run starts cold), "testDockerHost" (DOCKER_HOST inside, for the test suites; default the
    host's /var/run/docker.sock mounted in), "envFiles" (credential files under <workspace>/dev-tools),
    "gitBase" / "pushBase" (where repos are cloned from and pushed to), "dockerArgs" (extra `docker run` args)}."""

    def __init__(self, agent):
        self.agent_name = agent.name
        self.agent_keys = set(agent.env) - {GITHUB_TOKEN}
        self.gaia_env = agent.gaia_env
        self.settings = agent.runner
        self.home = self.settings.get("claudeHome", "bind")
        if self.home not in CLAUDE_HOMES:
            raise TalosError(f"{agent.name}: runner.claudeHome must be one of {', '.join(CLAUDE_HOMES)}")
        self.resumable = self.home != "none"

    def claude_bin(self, host_claude):
        return "claude"

    def mcp_config(self, host_mcp):
        return MCP if host_mcp else None

    def execute(self, job):
        if not job.repo or job.repo == "pr4m":
            raise TalosError("the container runner works on a service or dev-tools repo; the workspace repo (pr4m) "
                             "runs locally")
        values, files = self.environment(job)
        if self.home == "bind":
            claude_home(self.agent_name, job.issue).mkdir(parents=True, exist_ok=True)
        cli_env = {**os.environ, **values}
        if self.settings.get("dockerHost"):
            cli_env["DOCKER_HOST"] = self.settings["dockerHost"]
        cmd = docker_command(self.settings, self.agent_name, job, sorted(values), files, self.home)
        # a container left running by a stopped Talos would hold the name; --rm removes it once it exits
        subprocess.run(["docker", "rm", "-f", container_name(self.agent_name, job.issue)], env=cli_env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return runner.run(cmd, cli_env, job.agent_name, job.issue, job.prompt)

    def environment(self, job):
        """(variables the container gets, key files to mount as {host path: container path}).
        Only what the run needs crosses over: credentials, the agent's GAIA_* (from its gaiaEnv file, never this
        process's), agent.json env, the test slot and the entrypoint's settings; never the rest of this process's
        environment."""
        forwarded = {*CLAUDE_CREDENTIALS, *(k for k in FILES if not k.startswith("GAIA_")), *self.agent_keys,
                     "P4M_TEST_SLOT"}
        values = {k: v for k, v in job.env.items() if k in forwarded}
        gaia_env = Path(self.gaia_env)
        if gaia_env.is_file():
            values.update({k: v for k, v in read_env_file(gaia_env).items() if k.startswith("GAIA_")})
        for name in self.settings.get("envFiles", ENV_FILES):   # after: GH_TOKEN is Odysseus's, not the engineer's
            path = LOCAL_DIR / name
            if path.is_file():
                values.update({k: v for k, v in read_env_file(path).items() if not k.startswith("GAIA_")})
        values = {k: v for k, v in values.items() if v != ""}
        if not any(values.get(k) for k in CLAUDE_CREDENTIALS):
            raise TalosError(f"no Claude credential for the container: set {' or '.join(CLAUDE_CREDENTIALS)} "
                             f"in {LOCAL_DIR / ENV_FILES[1]}")
        if not values.get(GITHUB_TOKEN):
            raise TalosError(f"no {GITHUB_TOKEN} for the container: set Agent Odysseus's token in "
                             f"{LOCAL_DIR / ENV_FILES[0]}")
        files = {}
        for name, default in FILES.items():
            host = values.get(name) or default
            path = Path(os.path.expanduser(host)) if host else None
            values.pop(name, None)
            if not path:
                continue
            if not path.is_file():                        # pushes or Gaia calls would fail late and obscurely
                raise TalosError(f"{name} {path} not found; the container mounts it from the Docker host")
            values[name] = f"{SECRETS}/{name.lower()}"
            files[str(path)] = values[name]
            if path.with_name(path.name + ".pub").is_file():          # the SSH signing key's public half
                files[str(path) + ".pub"] = values[name] + ".pub"
        ai_config = LOCAL_DIR / "ai" / "config.local.json"
        values.update({
            "TALOS_REPO": job.repo,
            "TALOS_ISSUE": str(job.issue),
            "TALOS_BASE_BRANCH": BASE_BRANCH.get(job.repo, "dev"),
            "TALOS_GIT_BASE": self.settings.get("gitBase", "https://github.com/projects4me"),
            "TALOS_PUSH_BASE": self.settings.get("pushBase", "git@github-odysseus:projects4me"),
            "TALOS_AI_CONFIG": ai_config.read_text(encoding="utf-8") if ai_config.is_file() else "{}",
            "P4M_WORKSPACE": WORK,
        })
        return values, files


def docker_command(settings, agent_name, job, names, files, home):
    """The `docker run` argv. Variables go by name only (`-e NAME`), so no value is on the command line."""
    cmd = ["docker", "run", "--rm", "-i", "--name", container_name(agent_name, job.issue)]
    if settings.get("network"):
        cmd += ["--network", settings["network"]]
    for host in settings.get("addHosts", []):
        cmd += ["--add-host", host]
    for name in names:
        cmd += ["-e", name]
    cmd += ["-v", f"{volume(agent_name, job.issue, 'work')}:{WORK}"]
    if home == "bind":
        cmd += ["-v", f"{claude_home(agent_name, job.issue)}:{CLAUDE_HOME}"]
    elif home == "volume":
        cmd += ["-v", f"{volume(agent_name, job.issue, 'claude')}:{CLAUDE_HOME}"]
    if settings.get("testDockerHost"):
        cmd += ["-e", f"DOCKER_HOST={settings['testDockerHost']}"]
    else:
        cmd += ["-v", "/var/run/docker.sock:/var/run/docker.sock"]
    for host, inside in files.items():
        cmd += ["-v", f"{host}:{inside}:ro"]
    cmd += [str(a) for a in settings.get("dockerArgs", [])]
    return cmd + [settings["image"], "--", *job.cmd]

