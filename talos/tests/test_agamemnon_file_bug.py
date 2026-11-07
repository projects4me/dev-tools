"""Unit tests for Agamemnon's file-bug verb: creating the Bug, its refusals, the dedupe by signature, the daily cap and
undo. Run: python -m unittest discover -s tests"""

import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import aga  # noqa: E402
from lib import registry  # noqa: E402
from lib.agamemnon import bugs, journal, premise  # noqa: E402
from lib.agamemnon.undo import UndoRefused  # noqa: E402
from lib.agamemnon.verbs import Refused  # noqa: E402
import test_agamemnon_aga as base  # noqa: E402
from test_agamemnon_aga import ISSUE, WORKER_ID  # noqa: E402

SIGNATURE = "phpunit: ImagePreviewTest::testThumbnail"
ARGS = {"title": "Image preview test fails on dev", "signature": SIGNATURE,
        "body": "ImagePreviewTest fails.\n\nFailed asserting that 0 is 1.\nat line 40"}


class FileBugTracker(base.FakeTracker):
    def __init__(self, issues):
        super().__init__(issues)
        self.created, self.statuses = [], []
        self.next_number = 100

    def create_issue(self, project, subject, description, type_, priority, complexity, assignee, milestone):
        number = self.next_number
        self.next_number += 1
        self.created.append({"number": number, "project": project, "subject": subject, "description": description,
                             "type": type_, "priority": priority, "complexity": complexity, "assignee": assignee,
                             "milestone": milestone})
        self.issues[number] = {"number": number, "status": "new", "assignee": assignee}
        return {"issueNumber": number, "url": f"http://prom/issue/{number}"}

    def set_status(self, number, status):
        self.statuses.append((number, status))
        self.issues[number]["status"] = status


class FileBugTest(base.AgaTest):
    def setUp(self):
        super().setUp()
        self.tracker = FileBugTracker([base.an_issue(project="A Gaia", milestone="aga-v1")])
        self.ports.tracker = self.tracker
        self.settings.premise = premise.parse({
            "tiers": {"read": "", "file": "", "retry": ""},
            "incidents": {"ci_failed": {"tiers": ["read", "retry"]},
                          "base_broken": {"tiers": ["read", "file"]}}})
        self.orch.aga_file_bugs_per_day = 2
        self.orch.aga_max_actions = 10
        self.merger.failed = [{"name": "phpunit", "url": "u", "run": "99"},
                              {"name": "lint", "url": "u", "run": "99"}]
        self.base_failing("phpunit")

    def base_failing(self, *jobs):
        self.merger.base = {"run": "50", "sha": "basesha", "conclusion": "failure" if jobs else "success",
                            "jobs": [{"name": "phpunit", "conclusion": "failure" if "phpunit" in jobs else "success"},
                                     {"name": "lint", "conclusion": "failure" if "lint" in jobs else "success"}]}

    def file(self, args=None, **kwargs):
        return self.aga("base_broken", "file-bug", args or dict(ARGS), **kwargs)

    # -- creation --------------------------------------------------------------------------

    def test_files_a_high_bug_for_the_worker_in_the_blocked_issues_project_and_milestone(self):
        out = self.file()
        self.assertEqual((out["bug"], out["linked"]), (100, False))
        made = self.tracker.created[0]
        self.assertEqual((made["project"], made["milestone"], made["type"], made["priority"], made["assignee"]),
                         ("A Gaia", "aga-v1", "Bug", "high", WORKER_ID))
        self.assertEqual(made["subject"], ARGS["title"])
        self.assertTrue(made["description"].startswith(f"<p>Filed by Agent Agamemnon: blocks #{ISSUE}</p>"))
        self.assertIn(SIGNATURE, made["description"])
        self.assertIn("Failed asserting that 0 is 1.<br>at line 40", made["description"])
        self.assertIn("basesha", made["description"])
        self.assertTrue(made["description"].endswith(
            "<p><strong>Acceptance:</strong> the <code>phpunit</code> job passes on <code>dev</code>, so "
            f"#{ISSUE} can merge <code>dev</code> and carry on.</p>"))

    def test_journals_it_and_links_it_on_the_blocked_issue(self):
        out = self.file()
        action = journal.action(out["action"])
        self.assertEqual(action["state_after"]["bug"], 100)
        self.assertTrue(action["state_after"]["created"])
        self.assertEqual(action["undo"], {"op": "wont_fix", "issue": ISSUE, "bug": 100})
        number, title, body = self.tracker.comments[-1]
        self.assertEqual((number, title), (ISSUE, "Agent Agamemnon: file-bug"))
        self.assertIn("Bug #100 (http://prom/issue/100)", body)

    # -- refusals --------------------------------------------------------------------------

    def test_refused_when_the_job_does_not_fail_on_the_base_branch(self):
        self.base_failing("lint")
        with self.assertRaisesRegex(Refused, "not a bug on the base branch"):
            self.file()
        self.assertEqual((self.tracker.created, journal.actions()), ([], []))

    def test_refused_without_a_base_run(self):
        self.merger.base = None
        with self.assertRaisesRegex(Refused, "base-ci does not show"):
            self.file()

    def test_refused_when_the_signature_names_another_job(self):
        with self.assertRaises(Refused):
            self.file({**ARGS, "signature": "phpunitx: ImagePreviewTest"})
        self.assertEqual(self.tracker.created, [])

    def test_refused_without_title_signature_or_body(self):
        for missing in ("title", "signature", "body"):
            with self.assertRaisesRegex(Refused, "needs --title"):
                self.file({**ARGS, missing: " "})
        self.assertEqual(self.tracker.created, [])

    def test_refused_when_the_premise_does_not_grant_the_file_tier(self):
        with self.assertRaisesRegex(Refused, "tier 'file'"):
            self.aga("ci_failed", "file-bug", dict(ARGS))

    # -- dedupe by signature -----------------------------------------------------------------

    def test_an_open_bug_for_the_signature_is_linked_instead(self):
        self.file()
        out = self.file({**ARGS, "signature": "  phpunit:   ImagePreviewTest::testThumbnail ", "title": "again"})
        self.assertEqual((out["bug"], out["linked"]), (100, True))
        self.assertEqual(len(self.tracker.created), 1)
        action = journal.action(out["action"])
        self.assertEqual((action["state_after"]["bug"], action["state_after"]["created"]), (100, False))
        self.assertEqual(action["undo"], {"op": "none"})
        self.assertIn("filed earlier for the same signature", self.tracker.comments[-1][2])

    def test_another_signature_files_another_bug(self):
        self.file()
        self.assertEqual(self.file({**ARGS, "signature": "phpunit: OtherTest"})["bug"], 101)

    def test_a_bug_in_another_project_does_not_dedupe(self):
        self.file()
        self.tracker.issues[ISSUE]["project"] = "A Prometheus"
        out = self.file()
        self.assertEqual((out["bug"], out["linked"]), (101, False))
        self.assertEqual(self.tracker.created[-1]["project"], "A Prometheus")

    def test_a_closed_or_undone_bug_does_not_dedupe(self):
        self.orch.aga_file_bugs_per_day = 3
        self.file()
        self.tracker.issues[100]["status"] = "done"
        self.assertEqual(self.file()["bug"], 101)
        self.undoer().undo(journal.actions_of("file-bug")[-1]["id"])
        self.assertEqual(self.file()["bug"], 102)

    # -- daily cap -------------------------------------------------------------------------

    def test_daily_cap(self):
        self.file()
        self.file({**ARGS, "signature": "phpunit: B"})
        with self.assertRaisesRegex(Refused, "agaFileBugsPerDay"):
            self.file({**ARGS, "signature": "phpunit: C"})
        self.assertEqual(len(self.tracker.created), 2)

    def test_links_do_not_count_and_the_cap_is_per_day(self):
        self.file()
        self.file()                                         # a link
        self.file({**ARGS, "signature": "phpunit: B"})
        self.clock[0] += timedelta(days=1)
        self.assertFalse(self.file({**ARGS, "signature": "phpunit: C"})["linked"])

    # -- undo ------------------------------------------------------------------------------

    def test_undo_sets_the_bug_to_wont_fix_with_a_comment(self):
        out = self.file()
        note = self.undoer().undo(out["action"])
        self.assertEqual(note, "#100 set to won't fix")
        self.assertEqual(self.tracker.statuses, [(100, "wont_fix")])
        number, title, body = self.tracker.comments[-1]
        self.assertEqual((number, title), (100, "Agent Agamemnon: won't fix"))
        self.assertIn(f"for #{ISSUE}", body)
        self.assertIsNotNone(journal.action(out["action"])["undone_at"])

    def test_undo_refused_when_the_bug_is_closed(self):
        out = self.file()
        self.tracker.issues[100]["status"] = "won't fix"
        with self.assertRaisesRegex(UndoRefused, "already"):
            self.undoer().undo(out["action"])
        self.assertEqual(self.tracker.statuses, [])

    def test_undo_of_a_link_changes_nothing(self):
        self.file()
        out = self.file()
        self.undoer().undo(out["action"])
        self.assertEqual(self.tracker.statuses, [])


class BugsTest(unittest.TestCase):
    def test_job_of_prefers_the_longest_match(self):
        self.assertEqual(bugs.job_of("test (8.2): A", ["test", "test (8.2)"]), "test (8.2)")
        self.assertEqual(bugs.job_of("test", ["test"]), "test")
        self.assertIsNone(bugs.job_of("tests: A", ["test"]))

    def test_is_closed(self):
        for status in ("wont_fix", "won't fix", "Won't Fix", "done", "Closed"):
            self.assertTrue(bugs.is_closed(status, ("done", "closed")), status)
        for status in ("new", "in_progress", None):
            self.assertFalse(bugs.is_closed(status, ("done", "closed")), status)


class ConfigTest(unittest.TestCase):
    def test_cli_takes_the_bug_fields(self):
        args = aga.build_parser().parse_args(["file-bug", "--title", "t", "--signature", "s", "--body", "b",
                                              "--why", "w"])
        self.assertEqual((args.title, args.signature, args.body), ("t", "s", "b"))

    def test_daily_cap_defaults_to_three(self):
        self.assertEqual(registry.Orchestration(worker="odysseus").aga_file_bugs_per_day, 3)

    def test_premise_lists_the_file_tier(self):
        self.assertIn("file", premise.load().tiers)


if __name__ == "__main__":
    unittest.main()
