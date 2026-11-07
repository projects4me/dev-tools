"""The CodeRabbit trigger comment after `gh pr create`. Run: python -m unittest discover -s tests"""

import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import workflow  # noqa: E402

URL = "https://github.com/projects4me/gaia/pull/9"
TRIGGER = "@coderabbitai full review"


def run(repo, trigger=TRIGGER, comment_fails=False):
    cfg = {"codeRabbitRepos": ["gaia"], "prFooter": "f"}
    if trigger is not None:
        cfg["codeRabbitTrigger"] = trigger
    calls = []

    def fake_ody(r, tool, *args, **kw):
        calls.append(args)
        if args[:2] == ("pr", "comment") and comment_fails:
            raise RuntimeError("boom")
        return URL

    with mock.patch.object(workflow, "config", return_value=cfg), \
            mock.patch("lib.coderabbit.config", return_value=cfg), \
            mock.patch.object(workflow, "ody", side_effect=fake_ody), \
            mock.patch.object(workflow, "git_dir", return_value=Path(__import__("tempfile").mkdtemp())), \
            mock.patch.object(workflow, "base_branch", return_value="dev"), \
            mock.patch.object(workflow, "say"):
        url = workflow.open_pr(repo, 1, "t", "b")
    return url, [c for c in calls if c[:2] == ("pr", "comment")]


class TriggerTest(unittest.TestCase):
    def test_posted_for_enabled_repo(self):
        url, comments = run("gaia")
        self.assertEqual(url, URL)
        self.assertEqual(len(comments), 1)
        self.assertIn(TRIGGER, comments[0])

    def test_not_posted_for_disabled_repo(self):
        self.assertEqual(run("ares")[1], [])

    def test_not_posted_when_trigger_empty_or_missing(self):
        self.assertEqual(run("gaia", trigger="")[1], [])
        self.assertEqual(run("gaia", trigger=None)[1], [])

    def test_failed_comment_still_returns_url(self):
        url, _ = run("gaia", comment_fails=True)
        self.assertEqual(url, URL)


if __name__ == "__main__":
    unittest.main()
