"""Deterministic mapping from a handback reason, the run summary and the log tail to an incident type (or "other")."""

import re

from .premise import OTHER

# (incident type, handback reasons, patterns over summary + log tail), checked in order: the first match wins.
RULES = (
    ("handback_failed", ("handoff_failed", "not_handed_back"), ()),
    ("docker_down", (), (r"cannot connect to the docker daemon", r"docker daemon is not running",
                         r"error during connect:.*docker")),
    ("merge_conflict", (), (r"merge conflict", r"automatic merge failed", r"unmerged paths")),
    ("test_contention", (), (r"tree changed", r"logs? (were|was|got) overwritten", r"runner (is )?hung",
                              r"no free (test )?slot")),
    ("ci_failed", ("ci_failed",), (r"\bci\b.*\b(failed|failing)\b", r"merge gate failed.*\bci\b")),
    ("dirty_generated", (), (r"generated files? (are |is )?modified",)),
)


def classify(reason, summary, log_tail):
    """The incident type for a handback, or OTHER."""
    text = f"{summary or ''}\n{log_tail or ''}".lower()
    reason = str(reason or "").strip().lower()
    for incident, reasons, patterns in RULES:
        if reason in reasons or any(re.search(p, text) for p in patterns):
            return incident
    return OTHER
