"""Talos container entrypoint: lay out the issue's workspace in /work, then run the `claude -p` argv.

Usage (ContainerRunner builds it): entrypoint.py -- claude -p ...   (the prompt on stdin)
Settings come from the environment (see talos/lib/container.py): TALOS_REPO, TALOS_ISSUE, TALOS_BASE_BRANCH,
TALOS_GIT_BASE, TALOS_PUSH_BASE, TALOS_AI_CONFIG, GH_TOKEN, ODYSSEUS_SSH_KEY.

/work is the issue's volume, laid out like a local per-issue workspace (talos/lib/workspaces.py), so a later run
finds its clones (and any uncommitted work) again:
  /work/.workspace         the workspace repo (Pr4M) at origin/main, without submodules
  /work/CLAUDE.md, .claude copied from it / a link to it
  /work/.dev-tools-runtime dev-tools at origin/main, the tooling `dev-tools/ai/ai` hands over to
  /work/dev-tools          a link to the runtime, unless dev-tools is the issue's repo
  /work/<repo>             the issue's repo on claude-issue-N, with the `odysseus` push remote
Only Claude's JSON result goes to stdout; everything else goes to stderr.

The image runs as the non-root user `talos`; Talos may start it as the host's uid:gid instead (`docker run --user`,
so the bind-mounted ~/.claude stays the engineer's). That uid has no /etc/passwd entry, which ssh refuses, so the
entrypoint gives it one through nss_wrapper for itself and everything it starts.
"""

import json
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

WORK = Path("/work")
WORKSPACE_REPO = "Pr4M"
WORKSPACE = WORK / ".workspace"
RUNTIME = WORK / ".dev-tools-runtime"
MCP = WORK / ".talos" / "mcp.json"
HOME = Path.home()


def say(msg):
    print(f"talos-container: {msg}", file=sys.stderr, flush=True)


def git(*args, cwd=None, check=True):
    cp = subprocess.run(["git", *args], cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True)
    if check and cp.returncode != 0:
        raise SystemExit(f"talos-container: git {' '.join(args)} failed: {cp.stderr.strip()}")
    return cp.stdout.strip()


def user():
    """A passwd (and group) entry for a uid the image doesn't know, through nss_wrapper (LD_PRELOAD)."""
    uid, gid = os.getuid(), os.getgid()
    try:
        pwd.getpwuid(uid)
        return
    except KeyError:
        pass
    nss = Path("/tmp/talos-nss")
    nss.mkdir(mode=0o700, exist_ok=True)
    passwd, group = nss / "passwd", nss / "group"
    passwd.write_text(Path("/etc/passwd").read_text(encoding="utf-8")
                      + f"talos-host:x:{uid}:{gid}:Talos:{HOME}:/bin/sh\n", encoding="utf-8")
    groups = Path("/etc/group").read_text(encoding="utf-8")
    if not any(line.split(":")[2:3] == [str(gid)] for line in groups.splitlines()):
        groups += f"talos-host:x:{gid}:\n"
    group.write_text(groups, encoding="utf-8")
    os.environ.update({"LD_PRELOAD": "libnss_wrapper.so", "NSS_WRAPPER_PASSWD": str(passwd),
                       "NSS_WRAPPER_GROUP": str(group)})


def writable():
    """/work and ~ must be the run's: a volume made by an older image that ran as root isn't."""
    for path in (WORK, HOME / ".claude"):
        if path.exists() and not os.access(path, os.W_OK):
            raise SystemExit(f"talos-container: {path} is not writable by uid {os.getuid()}; a volume or folder "
                             "made by an older image, which ran as root? Remove the issue's talos-<agent>-<issue>-* volumes "
                             "(or chown the bind-mounted claude folder) and run again.")


def credentials():
    """GitHub over https with GH_TOKEN (read from the environment when git asks, never written down), and the
    odysseus SSH alias with the mounted key (copied so it can be chmod 600)."""
    if os.environ.get("GH_TOKEN"):
        git("config", "--global", "credential.https://github.com.helper",
            '!f() { echo username=x-access-token; echo "password=$GH_TOKEN"; }; f')
    key = os.environ.get("ODYSSEUS_SSH_KEY")
    if key and Path(key).is_file():
        ssh = HOME / ".ssh"
        ssh.mkdir(mode=0o700, exist_ok=True)
        for suffix in ("", ".pub"):
            if Path(key + suffix).is_file():
                shutil.copyfile(key + suffix, ssh / f"agent_odysseus{suffix}")
        (ssh / "agent_odysseus").chmod(0o600)
        os.environ["ODYSSEUS_SSH_KEY"] = str(ssh / "agent_odysseus")
        (ssh / "config").write_text("Host github-odysseus\n  HostName github.com\n  User git\n"
                                    f"  IdentityFile {ssh / 'agent_odysseus'}\n  IdentitiesOnly yes\n"
                                    "  StrictHostKeyChecking accept-new\n", encoding="utf-8")


def clone(path, url):
    """A clone at path; path may already exist empty (a submodule folder, the volume root)."""
    if not (path / ".git").exists():
        path.mkdir(parents=True, exist_ok=True)
        git("init", "-q", cwd=path)
        git("remote", "add", "origin", url, cwd=path)
    git("fetch", "-q", "--no-recurse-submodules", "origin", cwd=path)


def issue_branch(path, name, base, push_base, branch):
    """Check out claude-issue-N: the local branch, else the pushed one on `odysseus`, else a new one off
    origin/<base>. An existing checkout is left as it is, with its uncommitted work."""
    if git("remote", cwd=path).split().count("odysseus") == 0:
        git("remote", "add", "odysseus", f"{push_base}/{name}.git", cwd=path)
    if git("rev-parse", "--abbrev-ref", "HEAD", cwd=path, check=False) == branch:
        return
    if git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", cwd=path, check=False):
        git("checkout", "-q", branch, cwd=path)
    elif subprocess.run(["git", "fetch", "-q", "odysseus", branch], cwd=path, stdin=subprocess.DEVNULL,
                        capture_output=True).returncode == 0:
        git("checkout", "-q", "-b", branch, "FETCH_HEAD", cwd=path)
    else:
        git("checkout", "-q", "-b", branch, f"origin/{base}", cwd=path)


def layout():
    repo, issue = os.environ["TALOS_REPO"], os.environ["TALOS_ISSUE"]
    base, git_base = os.environ.get("TALOS_BASE_BRANCH", "dev"), os.environ["TALOS_GIT_BASE"]
    push_base, branch = os.environ["TALOS_PUSH_BASE"], f"claude-issue-{issue}"
    for path, name in ((WORKSPACE, WORKSPACE_REPO), (RUNTIME, "dev-tools")):   # read-only copies at main
        clone(path, f"{git_base}/{name}.git")
        git("checkout", "-q", "-f", "--detach", "origin/main", cwd=path)
    clone(WORK / repo, f"{git_base}/{repo}.git")
    issue_branch(WORK / repo, repo, base, push_base, branch)
    shutil.copyfile(WORKSPACE / "CLAUDE.md", WORK / "CLAUDE.md")
    links = {".claude": WORKSPACE / ".claude"}
    if repo != "dev-tools":
        links["dev-tools"] = RUNTIME
    for name, target in links.items():
        if not (WORK / name).is_symlink():
            (WORK / name).symlink_to(target, target_is_directory=True)
    tools = WORK / "dev-tools"
    (tools / "ai" / "config.local.json").write_text(os.environ.get("TALOS_AI_CONFIG", "{}"), encoding="utf-8")
    MCP.parent.mkdir(exist_ok=True)
    MCP.write_text(json.dumps({"mcpServers": {"gaia": {
        "type": "stdio", "command": "python3", "args": [str(RUNTIME / "gaia-mcp" / "server.py")]}}}),
        encoding="utf-8")
    say(f"workspace ready for #{issue} ({repo}, {branch})")


def main(argv):
    if "--" not in argv or argv.index("--") == len(argv) - 1:
        raise SystemExit("usage: entrypoint.py -- claude -p ...")
    command = argv[argv.index("--") + 1:]
    user()
    writable()
    credentials()
    layout()
    os.chdir(WORK)
    os.execvp(command[0], command)       # stdin (the prompt) and stdout (the result) pass straight through


if __name__ == "__main__":
    main(sys.argv[1:])
