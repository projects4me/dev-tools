"""Runs that ended with awaiting_test_slot: `ai test` found every test slot of their repo in use (adt-4372).

Each waits, out of the run slots, for a test slot to free; the oldest wait is resumed first. A resumed run keeps its
record (and its first wait time) until it ends, so a run that waits again keeps its place, and the slots it may be
about to lease are not handed to another waiting run.
"""

import threading
from dataclasses import dataclass


@dataclass
class Wait:
    """What resuming the run needs: its repo, model and session, whether it was shipping, and when it first waited."""
    repo: str
    model: str
    session_id: str
    shipping: bool
    since: object           # datetime of the first awaiting_test_slot
    resumed: bool = False


class SlotWaits:
    def __init__(self):
        self._waits = {}        # issue number -> Wait
        self._lock = threading.Lock()

    def park(self, number, repo, model, session_id, shipping, now):
        """Record that the issue's run waits for a test slot; a run that waited before keeps its first wait time."""
        with self._lock:
            earlier = self._waits.get(number)
            self._waits[number] = Wait(repo, model, session_id, shipping, earlier.since if earlier else now)

    def due(self, free):
        """Mark as resumed, and return, the oldest waiting issues that `free` test slots can take. Runs resumed
        earlier that have not ended yet are counted against the free slots: they may be about to lease one."""
        with self._lock:
            resumed = sum(1 for w in self._waits.values() if w.resumed)
            waiting = sorted((w.since, n) for n, w in self._waits.items() if not w.resumed)
            numbers = [n for _, n in waiting[:max(0, free - resumed)]]
            for n in numbers:
                self._waits[n].resumed = True
            return numbers

    def resumed(self, number):
        """The wait of a run due() resumed, or None."""
        with self._lock:
            wait = self._waits.get(number)
            return wait if wait and wait.resumed else None

    def drop(self, number):
        with self._lock:
            self._waits.pop(number, None)
