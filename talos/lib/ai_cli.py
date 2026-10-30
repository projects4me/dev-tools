"""Running dev-tools/ai from Talos (the CodeRabbit loop's `ai rabbit` / `ai handoff`, the orchestrator's merges)."""

import json
import subprocess
import sys

from .config import DEV_TOOLS, WORKSPACE, TalosError

AI = DEV_TOOLS / "ai" / "ai.py"


def run_ai(env, *args, timeout=300):
    """`ai <args>` with this Python and env; the CompletedProcess. TalosError when it can't be started or times out."""
    try:
        return subprocess.run([sys.executable, str(AI), *args], cwd=WORKSPACE, env=env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise TalosError(f"ai {args[0]} failed: {e}")


def ai_json(env, *args, timeout=300):
    """The JSON object `ai <args> --json` prints last; TalosError when there is none."""
    r = run_ai(env, *args, "--json", timeout=timeout)
    lines = (r.stdout or "").strip().splitlines()
    try:
        return json.loads(lines[-1])
    except (IndexError, ValueError):
        raise TalosError(f"ai {' '.join(map(str, args))}: exit {r.returncode}: "
                         f"{(r.stderr or r.stdout or '')[-500:]}")
