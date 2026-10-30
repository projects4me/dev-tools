"""Dependency gating: which issues wait on "Depends on" issues, and the memory that goes with waiting."""

import re
import threading

DONE_STATUSES = ("done", "closed")   # a dependency in one of these is finished and merged (agent.json can override)

DEPENDS_LINE = re.compile(r"Depends\s+on\s*:?\s*([^\n]*)", re.IGNORECASE)
# "#4160", or a project shortcode reference such as "aprom-4160" (issue numbers are one sequence across projects)
ISSUE_REF = re.compile(r"(?:#|\b[a-z]+-)(\d+)\b", re.IGNORECASE)


def parse_dependencies(description):
    """Issue numbers named on "Depends on" lines of a description (HTML tolerated), sorted and unique.

    "Depends on: #4160, aprom-4161" gives [4160, 4161]; "Depends on: none" gives []."""
    text = re.sub(r"<br\s*/?>|</p>|</li>|</div>", "\n", description or "", flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    numbers = set()
    for line in DEPENDS_LINE.findall(text):
        numbers.update(int(n) for n in ISSUE_REF.findall(line))
    return sorted(numbers)


def unmet_dependencies(dependencies, statuses, done_statuses=DONE_STATUSES):
    """The dependencies not yet done, given {issue number: status name or None}; no status counts as unmet."""
    done = {s.lower() for s in done_statuses}
    return [n for n in dependencies if str(statuses.get(n) or "").lower() not in done]


class DependencyGate:
    """The issues held back until their dependencies are done, and the waiting sets already commented on.

    Thread-safe: the workers hold and clear, the release worker reads."""

    def __init__(self, done_statuses=DONE_STATUSES):
        self.done_statuses = tuple(done_statuses)
        self._lock = threading.Lock()
        self._held = {}        # issue number -> (reconcile flag, dependencies, readable)
        self._notified = {}    # issue number -> waiting-on numbers last commented, so it isn't repeated

    def unmet(self, dependencies, statuses):
        return unmet_dependencies(dependencies, statuses, self.done_statuses)

    def hold(self, number, reconcile, dependencies, waiting_on=None):
        """Keep the issue held. waiting_on is the unmet set; None means the statuses could not be read, which holds
        the issue for a retry without a comment. Returns True when the waiting set is new, so it should be commented."""
        with self._lock:
            self._held[number] = (reconcile, tuple(dependencies), waiting_on is not None)
            if waiting_on is None:
                return False
            first = self._notified.get(number) != waiting_on
            self._notified[number] = waiting_on
            return first

    def clear(self, number):
        """Forget the issue; True when it was held."""
        with self._lock:
            self._notified.pop(number, None)
            return self._held.pop(number, None) is not None

    def any_held(self):
        with self._lock:
            return bool(self._held)

    def is_held(self, number):
        with self._lock:
            return number in self._held

    def candidates(self, changed=None):
        """[(number, reconcile, dependencies)] to re-check after `changed` moved: the held issues that depend on it, and
        those whose statuses could not be read. All of them when it is unknown (reconnect)."""
        with self._lock:
            return [(n, reconcile, deps) for n, (reconcile, deps, readable) in self._held.items()
                    if changed is None or not readable or changed in deps]
