"""The Prometheus test slots an agent's runs lease while they test (prometheusTestSlots, adt-4372).

One place for what Talos knows about them: which repo is leased, the environment that makes a run's `ai test`
lease a slot, and how many slots are free now. The slots themselves are OS file locks (installer/testlease.py).
"""

from . import registry  # noqa: F401  (puts installer/ on sys.path)
from .config import LOCAL_DIR

import testlease

NO_SLOT_EXIT = testlease.NO_SLOT_EXIT


class TestSlots:
    REPO = "prometheus"     # the repo whose `ai test` leases a slot only while it tests

    def __init__(self, count):
        self.count = count  # 0 = no cap: nothing is leased

    def leases(self, repo):
        """Whether a run on `repo` leases a test slot while it tests."""
        return bool(self.count) and repo == self.REPO

    def env(self, repo):
        """The environment that makes the run's `ai test` lease a slot ({} when the repo is not leased)."""
        return testlease.env_for(self.count) if self.leases(repo) else {}

    def free(self):
        return testlease.free_count(testlease.lock_dir(LOCAL_DIR), self.REPO, self.count)
