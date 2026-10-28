"""What lets runs overlap: the queue order, the concurrency caps and the test-slot leases.

Pure scheduling, no tracker or Claude: the dispatcher decides what a job is, this decides when it may start.
"""

import heapq
import itertools
import json
import threading
from dataclasses import dataclass
from pathlib import Path

PRIORITY_RANK = {"blocker": 0, "critical": 1, "high": 2, "medium": 3, "low": 4}
DEFAULT_PRIORITY = PRIORITY_RANK["medium"]


def order_key(resumption, priority):
    """Sort key of a queued issue: resumptions (feedback, pending, in_review...) before new work, then the issue's
    priority; the queue's own sequence number makes the rest first in, first out."""
    return (0 if resumption else 1, PRIORITY_RANK.get((priority or "").lower(), DEFAULT_PRIORITY))


class RunQueue:
    """A blocking priority queue: get() returns the lowest key, ties in the order they were put."""

    def __init__(self):
        self._heap = []
        self._seq = itertools.count()
        self._cond = threading.Condition()

    def put(self, key, item):
        with self._cond:
            heapq.heappush(self._heap, (key, next(self._seq), item))
            self._cond.notify()

    def get(self):
        with self._cond:
            while not self._heap:
                self._cond.wait()
            return heapq.heappop(self._heap)[2]


@dataclass(frozen=True)
class Lease:
    """What one running job holds: its issue, repo, model and test slot."""
    issue: int
    repo: str
    model: str
    slot: int


class Gate:
    """Admission to a run: per-repo and per-model caps, and a free test slot (1..slots), leased until released.

    acquire() blocks until all three allow the run. A cap of 0 or less, or a repo/model without one, is no cap.
    Slots are only leased when `slots` > 0."""

    def __init__(self, slots=0, by_repo=None, by_model=None, on_change=None):
        self.slots = slots
        self.on_change = on_change      # called with the leases after every change, under the gate's lock
        self.by_repo = by_repo or {}
        self.by_model = by_model or {}
        self._leases = []
        self._cond = threading.Condition()

    def _count(self, field, value):
        return sum(1 for lease in self._leases if getattr(lease, field) == value)

    def _free_slot(self):
        taken = {lease.slot for lease in self._leases}
        return next((k for k in range(1, self.slots + 1) if k not in taken), None)

    def _admit(self, repo, model):
        """The slot to lease (0 when slots are off), or None when something is full."""
        for field, value, caps in (("repo", repo, self.by_repo), ("model", model, self.by_model)):
            cap = caps.get(value)
            if cap and cap > 0 and self._count(field, value) >= cap:
                return None
        return self._free_slot() if self.slots > 0 else 0

    def acquire(self, issue, repo, model):
        repo = repo or ""
        with self._cond:
            while (slot := self._admit(repo, model)) is None:
                self._cond.wait()
            lease = Lease(issue, repo, model, slot)
            self._leases.append(lease)
            self._changed()
            return lease

    def release(self, lease):
        with self._cond:
            if lease in self._leases:
                self._leases.remove(lease)
                self._changed()
            self._cond.notify_all()

    def _changed(self):
        if self.on_change:
            self.on_change(list(self._leases))

    def in_flight(self):
        with self._cond:
            return list(self._leases)


class LeaseFile:
    """The leases in flight, written for `talos status` (another process) to read."""

    def __init__(self, path):
        self.path = Path(path)

    def write(self, leases):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"issue": l.issue, "repo": l.repo, "model": l.model, "slot": l.slot} for l in leases]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows), encoding="utf-8")
        tmp.replace(self.path)

    def read(self):
        try:
            return {row["issue"]: row for row in json.loads(self.path.read_text(encoding="utf-8"))}
        except (OSError, ValueError, KeyError):
            return {}
