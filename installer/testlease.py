"""Test slots leased only while a suite runs, shared by every process on the machine (adt-4372).

Talos caps how many runs may test a repo at once (a Prometheus test container needs 2-3 GiB at its peak) by giving
the run's `ai test` a slot count in P4M_TEST_LEASE_SLOTS. `ai test` leases one of those slots for the suites and
the baseline they may need, and gives it back when it ends; a run that is not testing holds nothing. A slot is an
OS file lock (filelock.py), so a crashed holder frees it and Talos, `ai` and Agamemnon all see the same slots.

When every slot is taken `ai test` does not wait inside the session: it prints NO_SLOT and exits with
NO_SLOT_EXIT, the session ends with outcome awaiting_test_slot, and Talos resumes it when a slot is free.
"""

import os
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

import filelock

ENV = "P4M_TEST_LEASE_SLOTS"    # slots `ai test` leases from (set by Talos for the repos it caps); unset = no lease
NO_SLOT = "RESULT: NO TEST SLOT"
NO_SLOT_EXIT = 75               # EX_TEMPFAIL: try again later
RETRY_SECONDS = 0.5             # a probe (free_count) holds a free slot for an instant: look once more


def lock_dir(local_dir):
    """Where the slot locks live: next to p4m's Gaia slot locks (<workspace>/dev-tools/.p4m)."""
    return Path(local_dir) / ".p4m"


def lock_path(folder, repo, slot):
    return Path(folder) / f"test-lease-{repo}-{slot}.lock"


def slots_from_env(environ=None):
    """The slot count Talos set for this run, or 0 (no lease) when it set none."""
    value = (os.environ if environ is None else environ).get(ENV, "")
    return int(value) if value.isdigit() else 0


def env_for(count):
    """The environment that makes `ai test` lease one of `count` slots ({} for none)."""
    return {ENV: str(count)} if count > 0 else {}


def _take(folder, repo, count, stack):
    for slot in range(1, count + 1):
        if stack.enter_context(filelock.held_if_free(lock_path(folder, repo, slot))):
            return slot
    return None


@contextmanager
def leased(folder, repo, count, sleep=time.sleep):
    """Hold the first free of slots 1..count for the block and yield its number; yield None when all are held."""
    with ExitStack() as stack:
        slot = _take(folder, repo, count, stack)
        if slot is None:
            sleep(RETRY_SECONDS)
            slot = _take(folder, repo, count, stack)
        yield slot


def free_count(folder, repo, count):
    """How many of slots 1..count nobody holds now."""
    return sum(1 for slot in range(1, count + 1) if not filelock.is_held(lock_path(folder, repo, slot)))
