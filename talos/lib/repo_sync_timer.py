"""Keeps every local repo's dev level with origin while `talos run` is up: `ai sync --all` every few minutes, which
catches PRs merged by hand on GitHub. It never switches branches, so it is safe beside running agents."""

import json
import os
import re
import threading
from datetime import datetime, timedelta

from .ai_cli import run_ai
from .config import STATE_DIR, log

DEFAULT_MINUTES = 10
WARN_AFTER = timedelta(hours=1)
DRIFT_FILE = STATE_DIR / "repo-drift.json"      # repo -> when it was first seen behind origin
BEHIND = re.compile(r"^(\S+): behind origin/\S+: (\d+)$")


def _env():
    return {k: v for k, v in os.environ.items() if k != "P4M_WORKSPACE"}


def drift():
    """Repos behind origin after the last sync, as {repo: behind_since}; read by `talos status`."""
    try:
        return json.loads(DRIFT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def stale(drifting, now, limit=WARN_AFTER):
    """The repos that have been behind for longer than the limit."""
    return sorted(r for r, since in drifting.items() if now - datetime.fromisoformat(since) > limit)


def record_drift(check_output, now):
    """Keep when each repo was first seen behind (kept across syncs); a repo that caught up is forgotten."""
    previous = drift()
    behind = {m[1] for m in map(BEHIND.match, check_output.splitlines()) if m and int(m[2]) > 0}
    DRIFT_FILE.write_text(json.dumps({r: previous.get(r, now.isoformat()) for r in sorted(behind)}), encoding="utf-8")


def sync_all():
    """One `ai sync --all` from the workspace itself (not a run worktree); warnings are logged, never raised."""
    env = _env()
    try:
        result = run_ai(env, "sync", "--all")
    except Exception as e:      # noqa: BLE001  (a failed sync never stops Talos)
        log(f"repo sync: {e}")
        return
    for line in (result.stdout or "").splitlines():
        if line.startswith("WARNING"):
            log(f"repo sync: {line}")
    if result.returncode != 0:
        log(f"repo sync: ai sync --all exit {result.returncode}: {(result.stderr or '')[-300:]}")
        return
    try:
        record_drift(run_ai(env, "sync", "--all", "--check").stdout or "", datetime.now())
    except Exception as e:      # noqa: BLE001
        log(f"repo sync: drift not recorded: {e}")


def start(minutes=DEFAULT_MINUTES):
    """Run sync_all now and then every `minutes`; returns the Event that stops the thread."""
    stop = threading.Event()

    def loop():
        while True:
            sync_all()
            if stop.wait(minutes * 60):
                return

    threading.Thread(target=loop, name="repo-sync", daemon=True).start()
    return stop
