"""Which p4m test containers are in use, by any session on this machine.

A `p4m test` run holds <state>/in-use/<container>.<pid>.lock while it uses a container and touches
<container>.last when it ends, so housekeeping (Talos' idle sweep) can tell a container in use from an idle one.
One lock per process, so parallel runs on one container (gaia api and phpunit) don't serialise. Stdlib only: p4m
and talos both import it from here.
"""

import os
from contextlib import contextmanager
from pathlib import Path

import filelock

USE_DIR = "in-use"


def state_dir(workspace):
    """The p4m test state folder of a workspace (per machine: `<workspace>/dev-tools/.p4m/test`, like p4m's)."""
    return Path(workspace) / "dev-tools" / ".p4m" / "test"


@contextmanager
def using(state, name):
    """Mark the container `name` in use by this process for the block, and record the time of its last use.

    The bookkeeping never replaces the block's own outcome: a mark that can't be removed or a last use that can't
    be recorded is left as it is (a mark no process holds is not in use)."""
    folder = Path(state) / USE_DIR
    mark = folder / f"{name}.{os.getpid()}.lock"
    try:
        with filelock.held(mark):
            yield
    finally:
        try:
            mark.unlink(missing_ok=True)      # Windows: fails while a usage() probe has the file open
        except OSError:
            pass
        try:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{name}.last").touch()
        except OSError:
            pass


def usage(state, name):
    """(in use now, time of the last use as epoch seconds or None) of the container `name`. Removes the marks of
    runs that died without cleaning up."""
    folder = Path(state) / USE_DIR
    busy = False
    for mark in folder.glob(f"{name}.*.lock"):
        try:
            if filelock.is_held(mark):
                busy = True
            else:
                mark.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        last = (folder / f"{name}.last").stat().st_mtime
    except OSError:
        last = None
    return busy, last
