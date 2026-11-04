"""Docker's state for Talos: whether the CLI and daemon are there, how much memory is used, which containers run.

The docker calls are the small functions at the top (argument list, no shell, a timeout); the parsing and the
verdict below them take plain strings and dicts, so they are unit tested without Docker."""

import json
import re
import subprocess

TEST_LABEL = "p4m.test"     # the label `p4m test` puts on its containers (installer/testenv.py LABEL)
TIMEOUT = 30                # seconds per docker call
DEFAULT_HIGH = 85           # used/total memory (%) at or above which Docker is pressured
DEFAULT_OK = 70             # once pressured, it stays so until used/total falls below this (%)
OK, PRESSURED, DOWN = "ok", "pressured", "down"

_UNITS = {"b": 1, "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4,
          "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4}


class DockerMissing(Exception):
    pass


def _docker(*args):
    """Run `docker <args>`; returns (returncode, stdout, stderr). Raises DockerMissing when there is no CLI."""
    try:
        r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=TIMEOUT, encoding="utf-8",
                           errors="replace")
    except FileNotFoundError as e:
        raise DockerMissing(str(e)) from e
    except subprocess.TimeoutExpired:
        return 124, "", f"docker {args[0]} timed out after {TIMEOUT}s"
    return r.returncode, r.stdout, r.stderr


def run_info():
    return _docker("info", "--format", "{{json .}}")


def run_stats():
    return _docker("stats", "--no-stream", "--format", "{{json .}}")


def run_test_containers():
    return _docker("ps", "-a", "--filter", f"label={TEST_LABEL}", "--format", "{{.Names}}")


def parse_size(text):
    """'1.5GiB' / '512MB' / '0B' -> bytes (int); None when it isn't a size."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([A-Za-z]+)\s*", str(text or ""))
    if not m or m.group(2).lower() not in _UNITS:
        return None
    return int(float(m.group(1)) * _UNITS[m.group(2).lower()])


def parse_info(text):
    """`docker info --format {{json .}}` output -> {"serverVersion", "memTotal", "errors"}; {} when unreadable."""
    try:
        info = json.loads(text)
    except (TypeError, ValueError):
        return {}
    if not isinstance(info, dict):
        return {}
    return {"serverVersion": info.get("ServerVersion") or "", "memTotal": int(info.get("MemTotal") or 0),
            "errors": [str(e) for e in info.get("ServerErrors") or []]}


def parse_stats(text):
    """`docker stats --no-stream --format {{json .}}` output (one JSON object per line) -> a list of
    {"name", "memory" (bytes), "cpu" (percent)}; unreadable lines are skipped."""
    containers = []
    for line in (text or "").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        used = parse_size(str(row.get("MemUsage", "")).split("/")[0])
        try:
            cpu = float(str(row.get("CPUPerc", "0")).strip().rstrip("%") or 0)
        except ValueError:
            cpu = 0.0
        containers.append({"name": row.get("Name") or row.get("Container") or "", "memory": used or 0, "cpu": cpu})
    return containers


def read():
    """Docker's state: {"cli", "daemon", "error", "serverVersion", "memTotal", "memUsed", "containers",
    "p4mTestContainers"}. Never raises."""
    state = {"cli": True, "daemon": False, "error": "", "serverVersion": "", "memTotal": 0, "memUsed": 0,
             "containers": [], "p4mTestContainers": 0}
    try:
        code, out, err = run_info()
    except DockerMissing as e:
        state.update(cli=False, error=str(e))
        return state
    info = parse_info(out) if code == 0 else {}
    if code != 0 or not info or info["errors"]:
        state["error"] = (err.strip() or "; ".join(info.get("errors", [])) or "docker info gave no usable output")
        return state
    state.update(daemon=True, serverVersion=info["serverVersion"], memTotal=info["memTotal"])
    try:
        code, out, _ = run_stats()
        if code == 0:
            state["containers"] = parse_stats(out)
            state["memUsed"] = sum(c["memory"] for c in state["containers"])
        code, out, _ = run_test_containers()
        if code == 0:
            state["p4mTestContainers"] = len(out.split())
    except DockerMissing:
        pass
    return state


def percent_used(state):
    total = state.get("memTotal") or 0
    return 100.0 * (state.get("memUsed") or 0) / total if total else 0.0


def verdict(state, high=DEFAULT_HIGH, low=DEFAULT_OK, was=None):
    """down: no CLI or the daemon is unreachable. pressured: used/total at or above `high`, or, when the previous
    verdict was pressured, still above `low` (hysteresis, so it doesn't flap around `high`). Otherwise ok."""
    if not state.get("cli") or not state.get("daemon"):
        return DOWN
    used = percent_used(state)
    if used >= high or (was == PRESSURED and used > low):
        return PRESSURED
    return OK


def gib(n):
    return f"{n / 1024 ** 3:.1f}GiB"


def status_line(state, verdict_):
    """The one line `talos status` prints."""
    if verdict_ == DOWN:
        why = "docker CLI not found" if not state.get("cli") else f"daemon unreachable: {state.get('error') or '?'}"
        return f"DOCKER  down ({why})"
    return (f"DOCKER  {verdict_}  memory {gib(state['memUsed'])} / {gib(state['memTotal'])} "
            f"({percent_used(state):.0f}%)  test containers {state['p4mTestContainers']}")
