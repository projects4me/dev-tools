"""CodeRabbit budget policy and ledger; ody mocked, no network. Run: python -m unittest discover -s tests"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import common  # noqa: E402
from lib import rabbitbudget as rb  # noqa: E402

SETTINGS = {"everyPrUntil": 10, "limit": 25, "largePrFiles": 6}


class PolicyTest(unittest.TestCase):
    def test_every_pr_tier_boundary(self):
        self.assertEqual(rb.decide(9, 1, False, SETTINGS), "coderabbit")
        self.assertEqual(rb.decide(10, 1, False, SETTINGS), "human")

    def test_middle_tier_large_or_rereview(self):
        self.assertEqual(rb.decide(10, 6, False, SETTINGS), "coderabbit")
        self.assertEqual(rb.decide(10, 5, False, SETTINGS), "human")
        self.assertEqual(rb.decide(24, 1, True, SETTINGS), "coderabbit")

    def test_limit_boundary(self):
        self.assertEqual(rb.decide(24, 6, False, SETTINGS), "coderabbit")
        self.assertEqual(rb.decide(25, 50, True, SETTINGS), "human")

    def test_no_budget_means_no_limit(self):
        self.assertEqual(rb.decide(1000, 0, False, None), "coderabbit")


class LedgerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        for p in (mock.patch.object(common, "STATE_DIR", self.state),
                  mock.patch.object(rb.coderabbit, "enabled", return_value=True),
                  mock.patch.object(rb.coderabbit, "head_sha", return_value="abc123"),
                  mock.patch.object(rb, "config", return_value={"codeRabbitBudget": SETTINGS,
                                                                "codeRabbitTrigger": "@coderabbitai full review"})):
            p.start()
            self.addCleanup(p.stop)
        self.ody = mock.patch.object(rb, "ody").start()
        self.addCleanup(mock.patch.stopall)

    def seed(self, sent=(), deferred=()):
        rb._write({"sent": list(sent), "deferred": list(deferred)})

    def sends(self, n, hours_ago=1):
        at = rb._stamp(datetime.now(timezone.utc) - timedelta(hours=hours_ago))
        return [{"at": at, "repo": "gaia", "pr": i, "sha": "x", "source": "pr-opened"} for i in range(n)]

    def test_posts_one_trigger_and_records_it(self):
        r = rb.request("gaia", 7, "pr-opened", files=2)
        self.assertEqual(r["decision"], "coderabbit")
        self.assertEqual(r["used"], 1)
        self.ody.assert_called_once_with("gaia", "gh", "pr", "comment", "7", "--body", "@coderabbitai full review")
        sent = rb._read()["sent"]
        self.assertEqual([(s["repo"], s["pr"], s["sha"], s["source"]) for s in sent],
                         [("gaia", 7, "abc123", "pr-opened")])

    def test_small_pr_in_middle_tier_goes_to_human_and_is_deferred_once(self):
        self.seed(self.sends(10))
        for _ in range(2):
            r = rb.request("gaia", 8, "pr-opened", files=2, issue=99)
        self.assertEqual(r["decision"], "human")
        self.ody.assert_not_called()
        self.assertEqual(len(rb._read()["deferred"]), 1)

    def test_no_issue_no_deferred_entry(self):
        self.seed(self.sends(25))
        self.assertEqual(rb.request("gaia", 8, "backlog")["decision"], "human")
        self.assertEqual(rb._read()["deferred"], [])

    def test_coderabbit_clears_deferred(self):
        self.seed(self.sends(10), [{"at": "2026-01-01T00:00:00Z", "repo": "gaia", "pr": 8, "issue": 99, "files": 2}])
        r = rb.request("gaia", 8, "reenter", files=2, rereview=True, issue=99)
        self.assertEqual(r["decision"], "coderabbit")
        self.assertEqual(rb._read()["deferred"], [])

    def test_rolling_window_and_prune(self):
        self.seed(self.sends(10, hours_ago=25) + self.sends(1, hours_ago=60) + self.sends(3, hours_ago=2))
        self.assertEqual(rb.request("gaia", 1, "pr-opened")["used"], 4)
        self.assertEqual(len(rb._read()["sent"]), 14)   # the 60 h entry is pruned; the 25 h ones are kept but not counted

    def test_failed_comment_means_human_and_nothing_counted(self):
        self.ody.side_effect = common.AiError("boom")
        r = rb.request("gaia", 7, "pr-opened", issue=5)
        self.assertEqual(r["decision"], "human")
        self.assertIn("boom", r["reason"])
        ledger = rb._read()
        self.assertEqual(ledger["sent"], [])
        self.assertEqual(len(ledger["deferred"]), 1)

    def test_disabled_repo_is_absent(self):
        with mock.patch.object(rb.coderabbit, "enabled", return_value=False):
            r = rb.request("ares", 7, "pr-opened")
        self.assertEqual(r["decision"], "absent")
        self.ody.assert_not_called()

    def test_budget_reports_count_next_slot_and_deferred(self):
        sent = self.sends(2, hours_ago=3)
        self.seed(sent, [{"at": "2026-01-01T00:00:00Z", "repo": "gaia", "pr": 8, "issue": 99, "files": 2}])
        info = rb.budget()
        self.assertEqual((info["used"], info["limit"], info["tier"]), (2, 25, "every"))
        self.assertEqual(info["nextSlot"], rb._stamp(rb._parse(sent[0]["at"]) + timedelta(hours=24)))
        self.assertEqual(len(info["deferred"]), 1)


if __name__ == "__main__":
    unittest.main()
