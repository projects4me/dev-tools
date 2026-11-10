"""The Docker hold shared by an agent's worker threads: no job is taken while Docker is down or under memory pressure."""

import threading
import time

from . import docker_state

CHECK_SECONDS = 30      # how long a verdict is trusted before docker is read again
START_GRACE_SECONDS = 60    # a down Docker may still be booting: try to start it only after this long


class DockerPause:
    """Holds the last Docker verdict (re-read at most every `check_seconds`), behind one lock.

    wait() blocks while the verdict is down or pressured, so one hold covers every worker. When Docker has been down
    for `grace_seconds` of a hold and `start` is given, start() is called once for that hold. The wait is
    interruptible: stop() releases every waiting worker."""

    def __init__(self, read=docker_state.read, high=docker_state.DEFAULT_HIGH, low=docker_state.DEFAULT_OK,
                 check_seconds=CHECK_SECONDS, auto_start=True, start=None, grace_seconds=START_GRACE_SECONDS,
                 clock=time.monotonic, on_change=None, log=None, poll_seconds=5, free=None,
                 pause_one=None, resume=None, max_containers=0):
        self._read, self._high, self._low = read, high, low
        self._max = max_containers
        self._check, self._grace, self._poll = check_seconds, grace_seconds, poll_seconds
        self._auto_start, self._start = auto_start, start
        self._clock, self._on_change = clock, on_change
        self._log = log or (lambda text: None)
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._verdict = docker_state.OK
        self._read_at = None        # clock reading of the last docker read
        self._since = None          # clock reading when the current hold began (None = not holding)
        self._since_text = None     # the same moment as an ISO time, for `talos status`
        self._tried = False         # start() already called in this hold
        self._free = free           # stops idle test containers; called once per pressured hold
        self._freed = False         # free() already called in this hold
        self._pause_one = pause_one  # pause_one() -> names of the one container it paused (none when it can't)
        self._resume = resume       # resume(names): unpauses them when the hold ends
        self._paused = []           # names paused in this hold
        self._pause_tried = False   # pause_one() already called in this hold

    @property
    def stopped(self):
        return self._stopped.is_set()

    @property
    def verdict(self):
        with self._lock:
            return self._verdict

    @property
    def since(self):
        """When the current hold began (ISO text), or None."""
        with self._lock:
            return self._since_text

    def _refresh(self):
        """Re-read docker when the verdict is stale, and open or close the hold. Call with the lock held.
        Returns True when this call ended a hold (one waiter gets it)."""
        now = self._clock()
        if self._read_at is not None and now - self._read_at < self._check:
            return False
        self._read_at = now
        self._verdict = docker_state.verdict(self._read(), self._high, self._low, was=self._verdict,
                                             max_containers=self._max)
        holding = self._verdict != docker_state.OK
        if holding and self._since is None:
            self._since, self._tried, self._freed, self._pause_tried = now, False, False, False
            self._since_text = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            self._log(f"Docker {self._verdict}: dispatching held")
            self._changed()
        elif not holding and self._since is not None:
            self._since = self._since_text = None
            self._unpause()
            self._log("Docker ok again: dispatching resumes")
            self._changed()
            return True
        return False

    def _changed(self):
        if self._on_change:
            self._on_change(self._verdict, self._since_text)

    def _maybe_start(self):
        """After the grace period of a down hold, start the engine once. Call with the lock held."""
        if (self._verdict != docker_state.DOWN or not self._auto_start or self._start is None or self._tried
                or self._clock() - self._since < self._grace):
            return
        self._tried = True
        try:
            self._log(f"Docker down for {self._grace:.0f}s: starting the engine")
            self._start()
        except Exception as e:      # a failed start must not stop the worker: keep waiting
            self._log(f"Docker start failed: {e}")
        self._read_at = None        # read again at once to see whether it answers now

    def _maybe_free(self):
        """Once per pressured hold, let free() stop idle containers, then read docker again. Call with the lock held."""
        if self._verdict != docker_state.PRESSURED or self._free is None or self._freed:
            return
        self._freed = True
        try:
            self._free()
        except Exception as e:      # freeing is best effort: the hold carries on
            self._log(f"Docker free failed: {e}")
        self._read_at = None

    def _maybe_pause(self):
        """Once per pressured hold, after free() had its go, pause one container so the others can finish. Call with
        the lock held."""
        if (self._verdict != docker_state.PRESSURED or self._pause_one is None or self._pause_tried
                or (self._free is not None and not self._freed) or self._read_at is None):
            return
        self._pause_tried = True
        try:
            self._paused = list(self._pause_one() or [])
        except Exception as e:      # best effort: the hold carries on
            self._log(f"Docker pause failed: {e}")
            return
        if self._paused:
            self._log(f"Docker pressured: paused {', '.join(self._paused)} until it is ok again")

    def _unpause(self):
        """Unpause what this hold paused (also when stop() ends the wait). Call with the lock held."""
        names, self._paused = self._paused, []
        if not names or self._resume is None:
            return
        try:
            self._resume(names)
            self._log(f"unpaused {', '.join(names)}")
        except Exception as e:
            self._log(f"Docker unpause failed: {e}")
            self._paused = names

    def wait(self):
        """Block while Docker is down or pressured. True when this call saw the hold end, False when there was
        nothing to wait for or stop() interrupted it."""
        while not self._stopped.is_set():
            with self._lock:
                if self._refresh():
                    return True
                if self._since is None:
                    return False
                self._maybe_start()
                self._maybe_free()
                self._maybe_pause()
            self._stopped.wait(min(self._check, self._poll))
        return False

    def stop(self):
        self._stopped.set()
        with self._lock:
            self._unpause()
