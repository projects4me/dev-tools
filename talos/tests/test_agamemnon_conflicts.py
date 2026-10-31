"""Unit tests for Agamemnon resolving a merge conflict itself: the additive guard (conflicts.resolve) and the
`aga resolve-conflict` verb in a real git workspace (fake Gaia, GitHub and tests), and `send` for a conflict.
Run: python -m unittest discover -s tests"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import sessions  # noqa: E402
from lib.agamemnon import conflicts, journal, premise, wiring  # noqa: E402
from lib.agamemnon.verbs import Refused  # noqa: E402
from test_agamemnon_aga import ISSUE, OWNER, WORKER_ID, AgaTest, git  # noqa: E402

BRANCH = f"claude-issue-{ISSUE}"
FEATURE = "Feature: ideas\n\n  Scenario: list\n    Given ideas\n"


def hunk(ours, base, theirs):
    return f"<<<<<<< HEAD\n{ours}||||||| base\n{base}=======\n{theirs}>>>>>>> origin/dev\n"


class ResolveTest(unittest.TestCase):
    def test_both_sides_added_lines_keeps_all_of_them(self):
        text = "Feature: x\n" + hunk("  Scenario: a\n", "", "  Scenario: b\n") + "end\n"
        r = conflicts.resolve("x.feature", text)
        self.assertTrue(r.additive)
        self.assertEqual(r.text, "Feature: x\n  Scenario: a\n  Scenario: b\nend\n")
        self.assertEqual(r.hunks, 1)

    def test_a_changed_base_line_is_not_additive(self):
        r = conflicts.resolve("x.js", "a\n" + hunk("b2\n", "b\n", "b3\n"))
        self.assertFalse(r.additive)
        self.assertIsNone(r.text)
        self.assertIn("changed lines of the base", r.problems[0])

    def test_one_side_adding_nothing_is_not_additive(self):
        self.assertIn("added nothing", conflicts.resolve("x", hunk("", "", "b\n")).problems[0])

    def test_without_the_base_part_or_an_end_it_refuses(self):
        two_way = "<<<<<<< HEAD\na\n=======\nb\n>>>>>>> dev\n"
        self.assertIn("diff3", conflicts.resolve("x", two_way).problems[0])
        self.assertIn("does not end", conflicts.resolve("x", "<<<<<<< HEAD\na\n").problems[0])
        self.assertIn("no conflict hunk", conflicts.resolve("x", "plain\n").problems[0])

    def test_crlf_line_endings_are_kept(self):
        text = "a\r\n<<<<<<< HEAD\r\nb\r\n||||||| base\r\n=======\r\nc\r\n>>>>>>> dev\r\n"
        self.assertEqual(conflicts.resolve("x", text).text, "a\r\nb\r\nc\r\n")


class ResolveConflictVerbTest(AgaTest):
    def setUp(self):
        super().setUp()
        self.settings.premise = premise.parse({
            "tiers": {t: "" for t in ("read", "restore", "resolve", "handoff")},
            "incidents": {"merge_conflict": {"tiers": ["read", "restore", "resolve", "handoff"]}}})
        self.tested, self.reviews, self.test_result = [], [], (True, "RESULT: PASS")
        self.ports.ody_git = lambda cwd, *a, check=True: wiring.git(
            cwd, "-c", "user.name=Agent Odysseus", "-c", "user.email=o@example.com", "-c", "commit.gpgsign=false",
            *a, check=check)
        self.ports.test_changed = lambda folder, repo: (self.tested.append(repo), self.test_result)[1]
        self.ports.bot_review = lambda issue, repo, pr: self.reviews.append((issue, repo, pr))
        self.ports.update_worker_state = lambda issue, **f: sessions.update("odysseus", issue, **f)
        self.incident_id = journal.open_incident(ISSUE, "merge_conflict", "t")

    def gh(self, *args):
        self.gh_calls.append(args)
        return git(self.folder, "rev-parse", "HEAD") + "\n" if "headRefOid" in args else ""

    def conflicting(self, ours_line, theirs_line, base_line=""):
        """The workspace on claude-issue-7 and origin/dev, both changing ideas.feature after `first`."""
        self.folder = folder = self.workspace()
        origin = self.tmp / "origin.git"
        git(self.tmp, "init", "--bare", str(origin))
        git(folder, "remote", "add", "origin", str(origin))
        (folder / "ideas.feature").write_text(FEATURE + base_line, encoding="utf-8")
        git(folder, "add", ".")
        git(folder, "commit", "-m", "base")
        git(folder, "push", "origin", "HEAD:refs/heads/dev")
        git(folder, "checkout", "-b", "other")
        (folder / "ideas.feature").write_text(FEATURE + theirs_line, encoding="utf-8")
        git(folder, "commit", "-am", "theirs")
        git(folder, "push", "origin", "HEAD:refs/heads/dev")
        git(folder, "checkout", BRANCH)
        (folder / "ideas.feature").write_text(FEATURE + ours_line, encoding="utf-8")
        git(folder, "commit", "-am", "ours")
        git(folder, "push", "odysseus", BRANCH)
        return folder

    def resolve(self, backup=True):
        if backup:
            self.aga("merge_conflict", "backup", incident=self.incident_id)
        return self.aga("merge_conflict", "resolve-conflict", why="both sides added a scenario",
                        incident=self.incident_id)

    def remote_head(self):
        return git(self.folder, "ls-remote", "odysseus", f"refs/heads/{BRANCH}").split()[0]

    def test_an_additive_conflict_is_merged_tested_pushed_and_reviewed(self):
        folder = self.conflicting("\n  Scenario: create\n", "\n  Scenario: delete\n")
        out = self.resolve()
        text = (folder / "ideas.feature").read_text(encoding="utf-8")
        self.assertEqual(text, FEATURE + "\n  Scenario: create\n\n  Scenario: delete\n")
        self.assertEqual(out["resolved"], {"ideas.feature": 1})
        self.assertEqual(self.remote_head(), out["pushed"])
        self.assertEqual(len(git(folder, "rev-list", "--parents", "-n", "1", "HEAD").split()), 3)
        self.assertIn("Agent Odysseus", git(folder, "log", "-1", "--format=%an"))
        self.assertIn(f"incident {self.incident_id}", git(folder, "log", "-1", "--format=%B"))
        self.assertEqual((self.tested, self.reviews), (["gaia"], [(ISSUE, "gaia", 12)]))
        self.assertIsNone(journal.action(out["action"])["undo"])                 # pushed: no undo here
        self.assertIn("Agent Agamemnon", self.tracker.comments[-1][1])

    def test_needs_a_backup_first(self):
        self.conflicting("a\n", "b\n")
        with self.assertRaisesRegex(Refused, "backup first"):
            self.resolve(backup=False)

    def test_a_changed_line_is_aborted_and_left_to_the_worker(self):
        folder = self.conflicting("  Then mine\n", "  Then theirs\n", base_line="  Then base\n")
        head, remote = git(folder, "rev-parse", "HEAD"), self.remote_head()
        with self.assertRaisesRegex(Refused, "not every conflict is additive(.|\n)*ideas.feature line 5"):
            self.resolve()
        self.assertEqual((git(folder, "rev-parse", "HEAD"), git(folder, "status", "--porcelain")), (head, ""))
        self.assertEqual((self.remote_head(), self.tested, self.reviews), (remote, [], []))

    def test_failing_tests_drop_the_merge(self):
        folder = self.conflicting("  Scenario: create\n", "  Scenario: delete\n")
        head, remote = git(folder, "rev-parse", "HEAD"), self.remote_head()
        self.test_result = (False, "FAIL ideas.feature: duplicate step")
        with self.assertRaisesRegex(Refused, "tests failed(.|\n)*duplicate step"):
            self.resolve()
        self.assertEqual((git(folder, "rev-parse", "HEAD"), git(folder, "status", "--porcelain")), (head, ""))
        self.assertEqual((self.remote_head(), self.reviews), (remote, []))

    def test_a_test_run_that_errors_drops_the_merge(self):
        folder = self.conflicting("  Scenario: create\n", "  Scenario: delete\n")
        head, remote = git(folder, "rev-parse", "HEAD"), self.remote_head()

        def timed_out(folder, repo):
            raise TimeoutError("ai test timed out")
        self.ports.test_changed = timed_out
        with self.assertRaises(Exception):
            self.resolve()
        self.assertEqual((git(folder, "rev-parse", "HEAD"), git(folder, "status", "--porcelain")), (head, ""))
        self.assertEqual(self.remote_head(), remote)

    def test_a_failed_review_reentry_still_journals_the_push(self):
        self.conflicting("  Scenario: create\n", "  Scenario: delete\n")

        def no_review(issue, repo, pr):
            raise RuntimeError("tracker down")
        self.ports.bot_review = no_review
        out = self.resolve()
        self.assertEqual(self.remote_head(), out["pushed"])
        self.assertIn("tracker down", out["botReviewError"])
        self.assertIsNotNone(journal.action(out["action"]))

    def test_never_in_a_workspace_with_a_run_in_flight(self):
        self.conflicting("a\n", "b\n")
        self.aga("merge_conflict", "backup", incident=self.incident_id)
        sessions.update("odysseus", ISSUE, running=True)
        with self.assertRaisesRegex(Refused, "in flight"):
            self.aga("merge_conflict", "resolve-conflict", why="x", incident=self.incident_id)

    def test_send_for_a_conflict_counts_the_workers_attempt_and_undo_gives_it_back(self):
        out = self.aga("merge_conflict", "send", {"notes": "ideas.feature: both sides changed a step."},
                       incident=self.incident_id)
        state = sessions.get("odysseus", ISSUE)
        self.assertEqual((state["next_action"], state["conflict_attempts"]), ("merge_conflict", 1))
        self.assertEqual(self.tracker.issues[ISSUE]["assignee"], WORKER_ID)
        self.undoer().undo(out["action"])
        state = sessions.get("odysseus", ISSUE)
        self.assertEqual((state["next_action"], state["conflict_attempts"]), ("", 0))
        self.assertEqual(self.tracker.issues[ISSUE]["assignee"], OWNER)


if __name__ == "__main__":
    unittest.main()
