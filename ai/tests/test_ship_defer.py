"""`ai ship --defer-handoff`: the hand-off waits for Talos in every repo. Run: python -m unittest discover -s tests"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import ship, workflow  # noqa: E402

URL = "https://github.com/projects4me/gaia/pull/9"
CFG = {"reviewer": "hammad"}


class DeferTest(unittest.TestCase):
    def run_ship(self, repo, defer):
        args = mock.Mock(repo=repo, issue=5, file="f", no_tests="docs", dry_run=False, defer_handoff=defer)
        spec = {"title": "t", "files": ["a", "b", "c"], "what": "w", "why": "y"}
        hand_off = mock.Mock()
        with mock.patch.object(ship, "load_spec", return_value=spec), \
                mock.patch.object(ship, "size_result", return_value={"count": 3, "line": "l", "warning": None,
                                                                     "reason": None}), \
                mock.patch.object(ship, "config", return_value=CFG), \
                mock.patch.object(ship, "load_progress", return_value={"commit": "abc", "pr": URL,
                                                                       "testLines": ["ok"]}), \
                mock.patch.object(ship, "save_progress"), mock.patch.object(ship, "issue_ref", return_value="r-5"), \
                mock.patch.object(ship, "git", return_value="abc"), mock.patch.object(ship, "say"), \
                mock.patch.object(ship, "cost_warning", return_value=None), \
                mock.patch.object(ship, "cost_note", return_value=""), \
                mock.patch.object(ship, "base_branch", return_value="dev"), \
                mock.patch.object(ship, "_progress_file"), \
                mock.patch.object(ship, "require_matching_workspace"), \
                mock.patch.object(ship, "announce_checkout"), \
                mock.patch.object(workflow, "pr_hand_off", hand_off):
            ship.cmd_ship(args)
        return hand_off.call_args.kwargs

    def test_every_repo_defers_when_asked(self):
        for repo in ("gaia", "prometheus", "hermes", "ares", "dev-tools"):
            self.assertEqual(self.run_ship(repo, True)["defer"], repo)

    def test_without_the_flag_it_hands_off(self):
        self.assertIsNone(self.run_ship("gaia", False)["defer"])


if __name__ == "__main__":
    unittest.main()
