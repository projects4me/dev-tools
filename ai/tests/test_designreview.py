"""The tree-bound design review (ai design-review), its ship line, and the merge gate that no longer reads it
(adt-5046). Run: python -m unittest discover -s tests"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import designreview, mergegate, reviewrecord, ship  # noqa: E402
from lib.common import AiError  # noqa: E402

CLEAN = "Looked at ai/lib/x.py.\n\nNo findings"
NITS = "- [nit] ai/lib/x.py:3: long line → wrap it\n"
OPEN = "- [should-fix] ai/lib/x.py:9: mixes I/O and logic → split\n- [nit] ai/lib/x.py:3: long line → wrap it\n"


class VerdictTest(unittest.TestCase):
    def test_no_findings_is_clean(self):
        self.assertEqual(reviewrecord.verdict(CLEAN), {"clean": True, "findings": [], "open": 0})

    def test_nits_alone_are_clean(self):
        found = reviewrecord.verdict(NITS)
        self.assertTrue(found["clean"])
        self.assertEqual(len(found["findings"]), 1)

    def test_blocker_or_should_fix_is_not_clean(self):
        self.assertEqual(reviewrecord.verdict(OPEN)["open"], 1)
        self.assertFalse(reviewrecord.verdict(OPEN)["clean"])
        self.assertFalse(reviewrecord.verdict("- [BLOCKER] a.py:1: x → y")["clean"])

    def test_an_empty_or_unreadable_report_is_not_clean(self):
        self.assertEqual(reviewrecord.verdict(""), {"clean": False, "findings": [], "open": None})
        self.assertFalse(reviewrecord.verdict("I ran out of turns.")["clean"])

    def test_open_findings_count_however_they_are_formatted(self):
        report = "- **[blocker]** a.py:1: x → y\n\nOtherwise no findings in the rest."
        self.assertEqual(reviewrecord.verdict(report)["open"], 1)
        self.assertFalse(reviewrecord.verdict(report)["clean"])
        self.assertFalse(reviewrecord.verdict("I found no findings worth noting, but ran out of turns")["clean"])

    def test_agent_file_frontmatter(self):
        text = "---\nname: design-reviewer\nmodel: sonnet\n---\n\nYou review a change.\n"
        self.assertEqual(designreview.parse_agent(text), ("sonnet", "You review a change."))
        self.assertEqual(designreview.parse_agent("Just a body"), (None, "Just a body"))


class TreeBoundRecordTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for patch in (mock.patch("lib.common.STATE_DIR", Path(self.dir.name)),
                      mock.patch.object(reviewrecord, "STATE_DIR", Path(self.dir.name))):
            patch.start()
            self.addCleanup(patch.stop)

    def line(self, tree):
        with mock.patch.object(reviewrecord.results, "tree_fingerprint", return_value=tree):
            return reviewrecord.ship_line("dev-tools")

    def test_clean_review_of_this_tree_gives_the_line(self):
        reviewrecord.record("dev-tools", "tree-1", reviewrecord.verdict(CLEAN), CLEAN)
        self.assertEqual(self.line("tree-1")[0], reviewrecord.REVIEW_LINE)

    def test_no_review_stale_tree_or_findings_give_no_line(self):
        self.assertIn("no recorded design review", self.line("tree-1")[1])
        reviewrecord.record("dev-tools", "tree-1", reviewrecord.verdict(CLEAN), CLEAN)
        line, why = self.line("tree-2")
        self.assertIsNone(line)
        self.assertIn("tree changed", why)
        reviewrecord.record("dev-tools", "tree-2", reviewrecord.verdict(OPEN), OPEN)
        self.assertEqual(self.line("tree-2"), (None, "the last design review is not clean (1 open finding(s))"))

    def test_an_unreadable_report_is_unclear(self):
        reviewrecord.record("dev-tools", "tree-1", reviewrecord.verdict("out of turns"), "out of turns")
        self.assertIn("not clean (unclear report", self.line("tree-1")[1])

    def test_a_reviewer_timeout_records_nothing(self):
        args = mock.Mock(repo="dev-tools")
        with mock.patch.object(reviewrecord.results, "tree_fingerprint", return_value="tree-1"), \
                mock.patch.object(designreview, "change", return_value=("main", "diff", [])), \
                mock.patch.object(designreview, "agent_file") as agent, \
                mock.patch.object(designreview.headless, "ask",
                                  side_effect=designreview.subprocess.TimeoutExpired("claude", 1)), \
                self.assertRaisesRegex(AiError, "no report within"):
            agent.return_value.read_text.return_value = "Review."
            designreview.cmd_design_review(args)
        self.assertIsNone(reviewrecord.last("dev-tools"))

    def test_a_tree_changed_during_the_review_is_never_clean(self):
        reviewrecord.record("dev-tools", "tree-1", reviewrecord.verdict(CLEAN), CLEAN, stale=True)
        self.assertEqual(self.line("tree-1"), (None, "the last design review is not clean (stale: the tree changed "
                                                     "during the review)"))

    def test_command_records_the_tree_it_started_on(self):
        args = mock.Mock(repo="dev-tools")
        with mock.patch.object(reviewrecord.results, "tree_fingerprint", return_value="tree-1"), \
                mock.patch.object(designreview, "change", return_value=("main", "diff", [])), \
                mock.patch.object(designreview, "agent_file") as agent, \
                mock.patch.object(designreview.headless, "ask", return_value=CLEAN) as ask, \
                mock.patch.object(designreview, "say"):
            agent.return_value.read_text.return_value = "---\nmodel: sonnet\n---\nReview."
            self.assertEqual(designreview.cmd_design_review(args), 0)
            self.assertEqual((ask.call_args[0][1], ask.call_args[0][-1]), ("Review.", "sonnet"))
            self.assertEqual(reviewrecord.ship_line("dev-tools")[0], reviewrecord.REVIEW_LINE)


class ShipLineTest(unittest.TestCase):
    SPEC = {"title": "t", "what": "w", "why": "y", "files": []}

    def test_the_line_goes_into_the_how_tested_block(self):
        body = ship.pr_body(self.SPEC, "adt-1", ["RESULT: PASS"], reviewrecord.REVIEW_LINE)
        self.assertIn("## How tested\n```\nRESULT: PASS\nDESIGN REVIEW: clean\n```", body)
        self.assertNotIn("DESIGN REVIEW", ship.pr_body(self.SPEC, "adt-1", ["RESULT: PASS"]))

    def test_every_repo_gets_the_line(self):
        with mock.patch.object(ship.reviewrecord, "ship_line", return_value=(reviewrecord.REVIEW_LINE, "clean")), \
                mock.patch.object(ship, "say"):
            for repo in ("gaia", "prometheus", "hermes", "ares", "dev-tools"):
                self.assertEqual(ship.review_line(repo), reviewrecord.REVIEW_LINE)

    def test_a_missing_review_still_ships_without_the_line(self):
        with mock.patch.object(ship.reviewrecord, "ship_line", return_value=(None, "no recorded design review")), \
                mock.patch.object(ship, "say") as say:
            self.assertIsNone(ship.review_line("dev-tools"))
            self.assertIn("opens without the design-review line", say.call_args[0][0])


class DevToolsGateTest(unittest.TestCase):
    """`ai mergecheck <repo> <pr>` with the shipped config.json: the design review is advisory, not a check."""
    BODY = "## How tested\n```\nRESULT: PASS\nDESIGN REVIEW: clean\n```"
    UNREVIEWED = "## How tested\n```\nRESULT: PASS\n```"

    def gate(self, body, files, repo="dev-tools"):
        pr = {"state": "open", "draft": False, "mergeable": True, "mergeable_state": "clean", "labels": [],
              "body": body, "head": {"sha": "abc1234def"}}
        answers = {"pulls/7": pr, "commits/abc1234def": {"commit": {"committer": {"date": "2026-11-05T10:00:00Z"}}},
                   "check-runs": {"check_runs": []}, "pulls/7/files": [{"filename": f} for f in files]}

        def api(repo, path):
            return next((v for k, v in answers.items() if path.endswith(k)), [])

        with mock.patch.object(mergegate, "_api", side_effect=api), \
                mock.patch.object(mergegate, "resolved_threads", return_value=set()):
            return mergegate.gate(repo, 7)

    def failed(self, result):
        return {c["name"]: c["detail"] for c in result["checks"] if c["state"] != "ok"}

    def test_ok_with_or_without_the_design_review_line(self):
        for body in (self.BODY, self.UNREVIEWED):
            result = self.gate(body, ["ai/lib/ship.py"])
            self.assertEqual((result["state"], self.failed(result)), ("ok", {}))
            self.assertNotIn("review", [c["name"] for c in result["checks"]])

    def test_protected_paths_still_fail(self):
        protected = self.gate(self.UNREVIEWED, ["ai/lib/mergegate.py"])
        self.assertEqual((protected["state"], list(self.failed(protected))), ("fail", ["self"]))

    def test_no_recorded_test_run_still_fails(self):
        result = self.gate("## How tested\nnothing", ["ai/lib/ship.py"])
        self.assertEqual((result["state"], list(self.failed(result))), ("fail", ["tests"]))

    def test_every_repo_passes_without_the_review_line(self):
        for repo in ("gaia", "prometheus", "hermes", "ares"):
            result = self.gate(self.UNREVIEWED, ["src/x.php"], repo=repo)
            self.assertEqual((repo, result["state"], self.failed(result)), (repo, "ok", {}))


if __name__ == "__main__":
    unittest.main()
