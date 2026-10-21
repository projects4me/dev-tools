"""Keeps the issue knowledge base (dev-tools/ai `kb`) fresh before an ideas agent searches it."""

import subprocess
import sys
import threading
import time

from ..config import DEV_TOOLS, WORKSPACE, TalosError

AI = DEV_TOOLS / "ai" / "ai.py"


def sync_as(env):
    """`ai kb sync` as the agent (only the changes since the last sync)."""
    def sync():
        out = subprocess.run([sys.executable, str(AI), "kb", "sync"], cwd=WORKSPACE, env=env, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
        if out.returncode != 0:
            raise TalosError((out.stderr or out.stdout).strip()[-300:])
    return sync


class KbSync:
    def __init__(self, sync, max_age_minutes=10):
        self.sync, self.max_age = sync, max_age_minutes * 60
        self._at = None
        self._lock = threading.Lock()

    def ensure_fresh(self, step):
        """Sync if the last sync is older than max_age; `step(text)` reports it. A failed sync is reported and
        the last index is used."""
        with self._lock:
            if self._at is not None and time.monotonic() - self._at < self.max_age:
                return
            step("Syncing the issue index")
            try:
                self.sync()
                self._at = time.monotonic()
            except Exception as e:
                step(f"Issue index sync failed ({e}); using the last index")
