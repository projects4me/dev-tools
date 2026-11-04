"""Start or restart the Docker engine and wait until the daemon answers, on an engineer's machine (Docker Desktop) or
a server (configured commands).

The method, in order: the agent.json command (`docker.startCommand` / `docker.restartCommand`, an argument list run
without a shell and without a TTY, so nothing can prompt); the `docker desktop start|restart` CLI (Docker Desktop
4.37+); then per platform: on Windows `Docker Desktop.exe` (a restart stops its process tree first), on Linux the
`docker-desktop` systemd user unit. A rootful system `docker.service` is never started without a configured command.

The process calls are the small functions at the top; the rest takes their results, so it is unit tested without
Docker. This module only does what it is asked: deciding when to start or restart is the caller's."""

import os
import subprocess
import shutil
import time
from pathlib import Path

from . import docker_state

DEFAULT_TIMEOUT = 180       # seconds to wait for the daemon (agent.json docker.startTimeoutSeconds)
POLL = 3                    # seconds between `docker info` polls while waiting
CHECK_TIMEOUT = 30          # seconds for a probe (systemctl cat, docker desktop --help, taskkill, docker ps)
STACK = ("gaia-dev", "postgres-dev", "p4m-proxy")    # the p4m stack's containers (installer/p4m.py)
DESKTOP_EXE = "Docker Desktop.exe"
DESKTOP_PROCESSES = (DESKTOP_EXE, "com.docker.backend.exe")   # stopped, with their trees, before a Windows restart
TASKKILL_NOT_FOUND = 128    # taskkill's exit code when no such process runs

_clock = time.monotonic
_sleep = time.sleep


class Refused(Exception):
    """No safe way to start or restart Docker here; the message says which setting to add."""


class EngineError(Exception):
    """The start or restart command failed."""


def _is_windows():
    return os.name == "nt"


def _run(argv, timeout=CHECK_TIMEOUT):
    """Run `argv` without a shell, with no stdin and (on POSIX) in a new session without a controlling terminal, so
    sudo or a password prompt fails instead of waiting. Returns (returncode, stdout, stderr); 127 when the program
    isn't there, 124 on timeout."""
    extra = ({"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if _is_windows()
             else {"start_new_session": True})
    try:
        r = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, **extra)
    except FileNotFoundError as e:
        return 127, "", str(e)
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]} timed out after {timeout:.0f}s"
    return r.returncode, r.stdout, r.stderr


def _spawn(argv):
    """Start a GUI program detached from Talos (it keeps running after this returns). Raises OSError."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True)


def _which(name):
    return shutil.which(name)


def _is_file(path):
    return os.path.isfile(path)


def find_desktop_exe(env=None):
    """`Docker Desktop.exe`: next to the docker CLI (<root>\\Docker\\Docker\\resources\\bin\\docker.exe), else under
    each Program Files folder the environment names. None when not found."""
    env = os.environ if env is None else env
    candidates = []
    cli = _which("docker")
    if cli:
        parents = Path(cli).parents
        if len(parents) > 2:
            candidates.append(parents[2] / DESKTOP_EXE)
    for key in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        if env.get(key):
            candidates.append(Path(env[key]) / "Docker" / "Docker" / DESKTOP_EXE)
    return next((str(c) for c in candidates if _is_file(c)), None)


def _check(argv, timeout):
    code, out, err = _run(argv, timeout)
    if code != 0:
        raise EngineError(f"{' '.join(argv)} exited {code}: {(err or out).strip() or 'no output'}")


def _windows_restart(exe):
    for image in DESKTOP_PROCESSES:
        code, out, err = _run(["taskkill", "/F", "/T", "/IM", image])
        if code not in (0, TASKKILL_NOT_FOUND):
            raise EngineError(f"taskkill {image} exited {code}: {(err or out).strip() or 'no output'}")
    _spawn([exe])


def _setting(action):
    return f"docker.{action}Command"


def plan(action, command):
    """(method, launch) for `action` ("start" or "restart"): `launch(timeout)` runs it, given the seconds left of the
    start budget, and raises EngineError on failure.
    Raises Refused when there is no safe default here."""
    if command:
        return "command", lambda t: _check(list(command), t)
    if _run(["docker", "desktop", "--help"])[0] == 0:
        return "docker-desktop-cli", lambda t: _check(["docker", "desktop", action], t)
    if _is_windows():
        exe = find_desktop_exe()
        if not exe:
            raise Refused(f"{DESKTOP_EXE} not found next to the docker CLI or under Program Files; "
                          f"set agent.json {_setting(action)}")
        if action == "restart":
            return "desktop-exe", lambda t: _windows_restart(exe)
        return "desktop-exe", lambda t: _spawn([exe])
    if _run(["systemctl", "--user", "cat", "docker-desktop"])[0] == 0:
        return "systemd-user", lambda t: _check(["systemctl", "--user", action, "docker-desktop"], t)
    if _run(["systemctl", "cat", "docker.service"])[0] == 0:
        raise Refused(f"Docker here is the system docker.service, which Talos doesn't {action} on its own; set "
                      f'agent.json {_setting(action)}, e.g. ["sudo", "-n", "systemctl", "{action}", "docker"] '
                      f"with a sudoers rule (see the Talos README)")
    raise Refused(f"no Docker Desktop CLI, docker-desktop user unit or docker.service found; "
                  f"set agent.json {_setting(action)}")


def wait_ready(timeout=DEFAULT_TIMEOUT, poll=POLL):
    """Poll docker_state.read() until the daemon answers: (True, "") or, after `timeout` seconds, (False, why)."""
    deadline = _clock() + timeout
    while True:
        state = docker_state.read()
        if state["daemon"]:
            return True, ""
        left = deadline - _clock()
        if left <= 0:
            return False, f"Docker did not answer within {timeout:.0f}s: {state.get('error') or 'no error text'}"
        _sleep(min(poll, left))


def missing_stack():
    """The p4m stack containers that aren't running (`docker ps`); None when docker ps fails."""
    code, out, _ = _run(["docker", "ps", "--format", "{{.Names}}"])
    if code != 0:
        return None
    running = set(out.split())
    return [name for name in STACK if name not in running]


def _act(action, command, timeout):
    started = _clock()
    result = {"action": action, "ok": False, "seconds": 0.0, "method": "", "error": "", "stackMissing": None}
    if action == "start" and docker_state.read()["daemon"]:
        result.update(ok=True, method="already-running")
    else:
        try:
            left = lambda: max(0.0, timeout - (_clock() - started))    # probes and launch count toward the budget
            result["method"], launch = plan(action, command)
            launch(max(1.0, left()))
            result["ok"], result["error"] = wait_ready(left())
        except Refused as e:
            result.update(method=result["method"] or "refused", error=str(e))
        except (EngineError, OSError) as e:
            result["error"] = str(e)
    if result["ok"]:
        result["stackMissing"] = missing_stack()
    result["seconds"] = round(_clock() - started, 1)
    return result


def start(command=None, timeout=DEFAULT_TIMEOUT):
    """Start the engine (nothing to do when the daemon already answers) and wait until it answers.
    Returns {action, ok, seconds, method, error, stackMissing}; never raises."""
    return _act("start", command, timeout)


def restart(command=None, timeout=DEFAULT_TIMEOUT):
    """Restart the engine and wait until it answers. Returns {action, ok, seconds, method, error, stackMissing}."""
    return _act("restart", command, timeout)


def for_agent(agent, action):
    """start or restart with the agent's agent.json `docker` settings."""
    command = agent.docker_restart_command if action == "restart" else agent.docker_start_command
    return _act(action, command, agent.docker_start_timeout_seconds)


def describe(result):
    """One line for a log or `talos` output."""
    if not result["ok"]:
        return f"DOCKER  {result['action']} failed ({result['method'] or '-'}, {result['seconds']}s): {result['error']}"
    line = f"DOCKER  {result['action']} ok ({result['method']}, {result['seconds']}s)"
    missing = result.get("stackMissing")
    if missing is None:
        return line + "; p4m stack unknown (docker ps failed)"
    return line + (f"; p4m stack not running: {', '.join(missing)}" if missing else "; p4m stack running")
