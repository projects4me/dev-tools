"""The Claude usage-limit pause shared by an agent's worker threads: one pause however many workers hit the limit."""

import threading
from datetime import datetime, timedelta

from . import policy

CHECK_SECONDS = 30      # how often a waiting worker looks at the clock again


def _now():
    return datetime.now().astimezone()


class UsagePause:
    """Owns the limit streak, the pause deadline and the backoff, behind one lock.

    Workers that hit the limit together get one pause (the first to report it); the others see it already running.
    The wait is interruptible: stop() releases every waiting worker at once."""

    def __init__(self, now=_now):
        self._now = now
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._until = None      # datetime: no dispatching until then
        self._streak = 0        # consecutive limits without a successful run in between

    @property
    def until(self):
        with self._lock:
            return self._until

    def hit(self, reset_at):
        """A run hit the limit. The new deadline (a datetime) when this call started the pause, None when one is
        already running (another worker reported the same limit)."""
        with self._lock:
            now = self._now()
            if self._until is not None and self._until > now:
                later = now + timedelta(seconds=policy.limit_pause_seconds(reset_at, self._streak, now))
                self._until = max(self._until, later)       # a later reset extends the pause, still one comment
                return None
            self._streak += 1
            self._until = now + timedelta(seconds=policy.limit_pause_seconds(reset_at, self._streak, now))
            return self._until

    def succeeded(self):
        """A run finished without hitting the limit: the backoff starts over."""
        with self._lock:
            self._streak = 0

    def wait(self):
        """Block while paused. True when this call saw the pause end (one waiter gets it), False when there was
        nothing to wait for or stop() interrupted it."""
        while not self._stopped.is_set():
            with self._lock:
                if self._until is None:
                    return False
                remaining = (self._until - self._now()).total_seconds()
                if remaining <= 0:
                    self._until = None
                    return True
            self._stopped.wait(min(remaining, CHECK_SECONDS))
        return False

    def stop(self):
        self._stopped.set()
