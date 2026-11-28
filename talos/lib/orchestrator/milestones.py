"""An orchestrator's auto-merge milestones, live: `autoMergeMilestones` is read again from its agent.json and
agent.local.json whenever either file changes, so adding or removing a milestone needs no restart of `talos run`.
Only this setting is live; every other one is read at startup.

A file that can't be read or holds an invalid list keeps the last good milestones (logged once per change), so a
half-saved edit neither stops merges nor merges the wrong milestones."""

import threading

from .. import registry
from ..config import TalosError, log


def _stamp(files):
    """The files' modification times and sizes (None for a missing one): any change means read again. The size
    catches two saves within one timestamp tick."""
    stamps = []
    for path in files:
        try:
            st = path.stat()
            stamps.append((st.st_mtime_ns, st.st_size))
        except OSError:
            stamps.append(None)
    return tuple(stamps)


class LiveMilestones:
    """live() -> the orchestrator's auto-merge milestones as its config files say now."""

    def __init__(self, name, initial, files, read, log=log):
        self.name, self.files, self.read, self.log = name, list(files), read, log
        self._value = tuple(initial)
        self._stamp = _stamp(self.files)
        self._lock = threading.Lock()

    def __call__(self):
        stamp = _stamp(self.files)
        if stamp == self._stamp:
            return self._value
        with self._lock:
            if stamp != self._stamp:
                try:
                    value = tuple(self.read())
                except TalosError as e:
                    self.log(f"auto-merge milestones unchanged ({', '.join(self._value) or 'none'}): {e}", self.name)
                else:
                    if value != self._value:
                        self.log(f"auto-merge milestones now {', '.join(value) or 'none'} "
                                 f"(were {', '.join(self._value) or 'none'})", self.name)
                    self._value = value
                self._stamp = stamp     # after the value, so the lock-free check never sees a new stamp with an
                                        # old value; an invalid file is reported once, not on every read
            return self._value


def live(agent):
    """A live list for an orchestrator agent (registry.Agent of kind orchestrator), started from the value it was
    loaded with. Each consumer (the merge sweep, the worker's route) holds its own; they read the same files."""
    return LiveMilestones(agent.name, agent.orchestration.auto_merge_milestones, registry.config_files(agent.name),
                          lambda: registry.read_auto_merge_milestones(agent.name))
