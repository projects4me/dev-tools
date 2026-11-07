"""Deterministic mapping from a handback reason, the run summary and the log tail to an incident type (or "other")."""

import re

from .premise import OTHER

# (incident type, handback reasons, patterns over summary + log tail), checked in order: the first match wins.
RULES = (
    ("handback_failed", ("handoff_failed", "not_handed_back"), ()),
    ("docker_down", (), (r"cannot connect to the docker daemon", r"docker daemon is not running",
                         r"error during connect", r"docker desktop is (starting|stopped|not running|unable to start)",
                         r"docker desktop.{0,20}(starting|stopped)", r"docker_engine.*(not found|cannot find)",
                         r"(pipe|socket).{0,60}(not found|cannot find|no such file|does not exist)",
                         r"dockerdesktoplinuxengine", r"docker\.sock.{0,40}(no such file|not found)",
                         r"is the docker daemon running")),
    ("merge_conflict", (), (r"merge conflict", r"automatic merge failed", r"unmerged paths")),
    ("docker_pressure", (), (r"out of memory", r"oom[- ]?kill", r"oomkilled", r"exit(ed)?( with)? (status |code )?137\b",
                              r"(^|\n)\s*killed\s*(\n|$)", r"\bkilled\s+(npm|node|ember|php|phpunit|pytest|python)",
                              r"cannot allocate memory", r"no space left on device")),
    ("test_contention", (), (r"tree changed", r"logs? (were|was|got) overwritten", r"runner (is )?hung",
                              r"no free (test )?slot")),
    ("ci_failed", ("ci_failed",), (r"\bci\b.*\b(failed|failing)\b", r"merge gate failed.*\bci\b")),
    ("dirty_generated", (), (r"generated files? (are |is )?modified",)),
)


# The merge sweep's CI handbacks (orchestrator/merging.py `_ci_failed`), read from the handback's own text first: the
# worker's last run log, read with it, may mention Docker or a conflict from an earlier step and must not win.
CI_HANDBACKS = (r"^ci failed again after \d+ re-?runs?\b", r"^ci failed and has no job to re-?run\b",
                r"^re-running the failed ci failed \d+ times?\b")


def classify(reason, summary, log_tail, detail=None):
    """The incident type for a handback, or OTHER. detail: the handback's own text, read before the run's."""
    detail = str(detail or "").strip().lower()
    if any(re.search(p, detail) for p in CI_HANDBACKS):
        return "ci_failed"
    text = f"{detail}\n{summary or ''}\n{log_tail or ''}".lower()
    reason = str(reason or "").strip().lower()
    for incident, reasons, patterns in RULES:
        if reason in reasons or any(re.search(p, text) for p in patterns):
            return incident
    return OTHER
