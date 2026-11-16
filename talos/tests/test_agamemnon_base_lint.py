"""Unit tests for Agamemnon's base_lint_failed incident: a worker's lint failure it blames on the base branch. The
classifier (with the aprom-4609 handback as the fixture; test_agamemnon_triage replays it), the lint-base verb,
file-bug and hold-until-done for a lint error on the base, and the `ai test --suite lint` wiring.
Run: python -m unittest discover -s tests"""

import json
import sys
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import aga  # noqa: E402
from lib.config import TalosError  # noqa: E402
from lib.agamemnon import classifier, journal, premise, wiring  # noqa: E402
from lib.agamemnon.verbs import Refused  # noqa: E402
import test_agamemnon_aga as base  # noqa: E402
from test_agamemnon_aga import ISSUE, MY_ID, WORKER_ID, git  # noqa: E402
from test_agamemnon_base_test import commit  # noqa: E402
from test_agamemnon_file_bug import FileBugTracker  # noqa: E402

INCIDENT = "base_lint_failed"

# Agent Achilles' handback on aprom-4609 (2026-11-16), which Agamemnon declined as 'other'
HANDBACK_4609 = (
    "The change for #4609 is ready on branch claude-issue-4609 (4 files, rebased onto the current origin/dev), but it "
    "can't ship yet.\n"
    "Tests: ember pass 1616, fail 0. The full run reports FAIL because of 2 lint errors on "
    "app/components/app-ui/notifications/notification.hbs: no-duplicate-attributes (duplicate data-notification-id on "
    "line 1) and invalid-todo-violation-rule. ai ship refuses unless the last full run passed.\n"
    "Cause: this is not a CRLF problem, as the earlier handoff assumed. Every entry in prometheus/.lint-todo (377 of "
    "them) carries the expiry timestamp 1794614400000, which is 2026-11-14. The todos expired two days ago, so lint now "
    "reports every suppressed violation on dev itself. The extra failures I saw (e.g. no-curly-component-invocation "
    "on line 15 of the same file) come from the same expiry. Removing the duplicate attribute and running --clean-todo "
    "does not clear the lint: the curly-component error then surfaces.\n"
    "What would unblock it: either a separate Prometheus issue to renew or clean the expired .lint-todo entries on "
    "dev, after which I re-run the suite and ship, or your decision to let me ship with the lint failure documented as "
    "known. I can't override the gate myself. I left the diff limited to the issue scope.")

# `ai test prometheus --suite lint` output
LINT_CLEAN = "lint: OK\n\nRESULT: PASS (targeted run; ai ship needs a full run)"
LINT_FAILING = (
    "lint: 2 errors\n  app/a.hbs:1:4 Duplicate attribute (no-duplicate-attributes)\n"
    "  app/b.js:3:1 'x' is unused (no-unused-vars)\n"
    "  FAIL app/a.hbs:1:4 Duplicate attribute (no-duplicate-attributes)  <-- unexpected\n"
    "  FAIL app/b.js:3:1 'x' is unused (no-unused-vars)  <-- unexpected\n\nRESULT: FAIL (targeted run)")
BUG = {"title": "Duplicate attribute in a.hbs fails lint on dev", "signature": "app/a.hbs:1:4 Duplicate attribute",
       "body": "lint fails on a clean dev checkout.\n\napp/a.hbs:1:4 Duplicate attribute (no-duplicate-attributes)"}


class ClassifierTest(unittest.TestCase):
    def test_the_4609_handback_is_a_lint_incident(self):
        self.assertEqual(classifier.classify("agent_blocked", "", "", detail=HANDBACK_4609), INCIDENT)
        self.assertEqual(classifier.classify("agent_blocked", HANDBACK_4609, ""), INCIDENT)

    def test_wider_base_blame_phrases(self):
        for text in ("AvatarTest fails on dev itself", "the same failures show on dev as well",
                     "dev is broken: AvatarTest", "the base branch is red"):
            with self.subTest(text=text):
                self.assertEqual(classifier.classify("agent_blocked", text, ""), "base_test_failed")

    def test_a_base_test_reason_whose_failing_suite_is_lint(self):
        detail = json.dumps({"failingTests": ["lint: 3 errors"]})
        self.assertEqual(classifier.classify("base_test_failed", "", "", detail=detail), INCIDENT)
        self.assertEqual(classifier.classify("agent_blocked", "lint fails on dev too", ""), INCIDENT)

    def test_a_test_failure_with_lint_passing_stays_a_test_incident(self):
        summary = "lint: OK\nember: pass 10, fail 1\nAppUi::Avatar fails on dev too"
        self.assertEqual(classifier.classify("agent_blocked", summary, ""), "base_test_failed")

    def test_a_lint_mention_in_the_log_tail_does_not_make_a_test_failure_lint(self):
        log = "lint: 2 errors\n  FAIL app/a.hbs:1:4 (no-bare-strings)\nupdated .lint-todo"
        self.assertEqual(classifier.classify("base_test_failed", "AppUi::Avatar fails on dev too", log),
                         "base_test_failed")

    def test_lint_errors_without_blaming_the_base_are_not_an_incident(self):
        self.assertEqual(classifier.classify("agent_blocked", "lint: 2 errors in my template", ""), "other")


class BaseLintFailedTest(base.AgaTest):
    def setUp(self):
        super().setUp()
        self.tracker = FileBugTracker([base.an_issue(project="A Prometheus", milestone="aga-v1")])
        self.tracker.issues[8] = {"number": 8, "status": "in_progress", "assignee": WORKER_ID}
        self.ports.tracker = self.tracker
        self.settings.premise = premise.load()
        self.settings.worker.repo_for = lambda p: "prometheus"
        self.settings.worker.max_parallel = 2
        self.orch.aga_max_actions = 10
        self.merger.pr = None                       # a local failure: no PR yet
        self.ports.in_flight = lambda: []
        self.checkouts, self.dropped, self.linted = [], [], []
        self.lint_result = [(True, LINT_CLEAN)]
        self.ports.base_checkout = self.base_checkout
        self.ports.drop_checkout = self.dropped.append
        self.ports.lint = self.fake_lint
        self.run_log = self.tmp / "run.log"
        self.run_log.write_text(json.dumps({"session_id": "x", "result": HANDBACK_4609}), encoding="utf-8")
        self.ports.run_log = lambda n: self.run_log
        self.incident_id = journal.open_incident(ISSUE, INCIDENT, "2026-11-16T12:03:00+05:00")

    def base_checkout(self, issue, repo):
        folder = self.tmp / "base-checkout" / repo
        if not (folder / ".git").exists():
            folder.mkdir(parents=True)
            git(folder, "init")
            commit(folder, "a.txt", "a\n", "base")
        self.checkouts.append((issue, repo))
        return folder

    def fake_lint(self, folder, repo):
        self.linted.append((folder, repo))
        return self.lint_result[0]

    def run_verb(self, verb, args=None):
        return self.aga(INCIDENT, verb, args, incident=self.incident_id)

    # -- the 4609 case: clean on the base, a stale todo -> back to the worker --------------

    def test_lint_passes_on_the_base_and_the_notes_explain_the_stale_todo(self):
        out = self.run_verb("lint-base")
        self.assertTrue(out["passed"])
        self.assertTrue(out["staleTodo"])
        self.assertEqual((out["base"], out["errors"]), ("dev", []))
        for words in ("lint passes on dev", "invalid-todo-violation-rule", "stale", "p4m test lint --clean-todo"):
            self.assertIn(words, out["notes"])
        self.assertEqual(self.checkouts, [(ISSUE, "prometheus")])
        self.assertEqual(self.dropped, [ISSUE])
        self.assertNotIn(".runs", str(self.linted[0][0]))

        with self.assertRaisesRegex(Refused, "lint passed on dev.*branch's own failure"):
            self.run_verb("file-bug", dict(BUG))
        self.run_verb("send", {"notes": out["notes"]})
        self.assertEqual(self.tracker.issues[ISSUE]["assignee"], WORKER_ID)
        self.assertIn("recorded .lint-todo entry covers", self.tracker.comments[-2][2])
        self.assertEqual(self.tracker.created, [])

    def test_without_a_stale_todo_the_notes_blame_the_diff_only(self):
        self.run_log.write_text("lint: 1 error\n  FAIL app/a.hbs:1:4 Duplicate attribute (no-duplicate-attributes)",
                                encoding="utf-8")
        out = self.run_verb("lint-base")
        self.assertFalse(out["staleTodo"])
        self.assertIn("the branch's own diff", out["notes"])
        self.assertNotIn("clean-todo", out["notes"])

    # -- lint fails on the base -> Bug + hold ----------------------------------------------

    def test_lint_fails_on_the_base_files_the_bug_and_holds(self):
        self.lint_result[0] = (False, LINT_FAILING)
        out = self.run_verb("lint-base")
        self.assertFalse(out["passed"])
        self.assertFalse(out["staleTodo"])
        self.assertEqual(out["errors"], ["app/a.hbs:1:4 Duplicate attribute (no-duplicate-attributes)",
                                         "app/b.js:3:1 'x' is unused (no-unused-vars)"])
        self.assertIn("file-bug", out["notes"])
        bug = self.run_verb("file-bug", dict(BUG))["bug"]
        created = self.tracker.created[0]
        self.assertEqual((created["type"], created["assignee"], created["project"]),
                         ("Bug", WORKER_ID, "A Prometheus"))
        self.assertIn("no-duplicate-attributes", created["description"])
        held = self.run_verb("hold-until-done", {"blocker": str(bug)})
        self.assertIsNone(held["pr"])
        self.assertEqual(self.tracker.issues[ISSUE]["assignee"], MY_ID)

    def test_file_bug_needs_a_lint_base_that_reported_that_error(self):
        with self.assertRaisesRegex(Refused, "run lint-base first"):
            self.run_verb("file-bug", dict(BUG))
        self.lint_result[0] = (False, LINT_FAILING)
        self.run_verb("lint-base")
        with self.assertRaisesRegex(Refused, "did not report 'app/c.hbs"):
            self.run_verb("file-bug", {**BUG, "signature": "app/c.hbs:2:2 other"})
        self.assertEqual(self.tracker.created, [])

    # -- refusals --------------------------------------------------------------------------

    def test_lint_that_did_not_run_is_refused_and_not_recorded(self):
        def no_container(*_):
            raise TalosError("ai test did not run the tests (exit 2): prometheus-test did not start")
        self.ports.lint = no_container
        with self.assertRaisesRegex(Refused, "lint did not run on the base"):
            self.run_verb("lint-base")
        self.assertEqual(self.dropped, [ISSUE])
        self.assertEqual(journal.actions(), [])

    def test_a_repo_without_a_lint_suite_is_refused(self):
        self.settings.worker.repo_for = lambda p: "gaia"
        with self.assertRaisesRegex(Refused, "gaia has no lint suite"):
            self.run_verb("lint-base")
        self.assertEqual(self.checkouts, [])

    def test_lint_base_is_not_granted_outside_its_tier(self):
        other = journal.open_incident(ISSUE, "docker_down", "t")
        with self.assertRaisesRegex(Refused, "tier 'basetest'"):
            self.aga("docker_down", "lint-base", incident=other)

    def test_cli_has_the_verb(self):
        args = aga.build_parser().parse_args(["lint-base", "--why", "the worker blames dev"])
        self.assertEqual(args.verb, "lint-base")


class LintWiringTest(unittest.TestCase):
    def test_lint_runs_the_lint_suite_in_the_checkouts_workspace(self):
        folder = Path("ws") / "prometheus"
        calls = []

        def run_ai(env, *args, timeout=0):
            calls.append((env["P4M_WORKSPACE"], args))
            return CompletedProcess(args, 1, LINT_FAILING, "")
        with mock.patch.object(wiring, "run_ai", run_ai):
            passed, output = wiring.lint(folder, "prometheus")
        self.assertFalse(passed)
        self.assertEqual(calls, [(str(Path("ws")), ("test", "prometheus", "--suite", "lint"))])

    def test_a_lint_run_without_errors_named_did_not_run(self):
        with mock.patch.object(wiring, "run_ai", lambda *a, **k: CompletedProcess(a, 1, "no summary (see log)", "")):
            with self.assertRaisesRegex(TalosError, "did not run"):
                wiring.lint(Path("ws") / "prometheus", "prometheus")


if __name__ == "__main__":
    unittest.main()
