"""Unit tests for Agamemnon's base_test_failed incident: a worker's local full suite failing on tests its diff does not
touch. The verbs update-workspace and test-base, file-bug and hold-until-done without a PR, the hold's release into the
workspace, and the throwaway base checkout (real git in a temp dir). Run: python -m unittest discover -s tests"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import aga  # noqa: E402
from lib import sessions, workspaces  # noqa: E402
from lib.config import TalosError  # noqa: E402
from lib.agamemnon import branch_update, holds, journal, premise, wiring  # noqa: E402
from lib.agamemnon.verbs import Refused  # noqa: E402
import test_agamemnon_aga as base  # noqa: E402
from test_agamemnon_aga import ISSUE, MY_ID, OWNER, WORKER_ID, git  # noqa: E402
from test_agamemnon_file_bug import FileBugTracker  # noqa: E402

INCIDENT = "base_test_failed"
TESTS = ["AppUi::Avatar shows initials", "AppUi::Tabs switches"]
BUG = {"title": "AppUi::Avatar fails on dev", "signature": "AppUi::Avatar shows initials",
       "body": "Fails on a clean dev checkout.\n\nexpected 'AB', got ''"}


def ody_git(cwd, *args, check=True):
    """git with an identity, as the odysseus wrapper gives it."""
    return wiring.git(cwd, "-c", "user.name=Agent Odysseus", "-c", "user.email=o@example.com",
                      "-c", "commit.gpgsign=false", *args, check=check)


def commit(folder, name, text, message):
    (folder / name).write_text(text, encoding="utf-8")
    git(folder, "add", name)
    git(folder, "commit", "-m", message)


def origin_with_dev(tmp):
    """A bare origin with a dev branch, and a clone of it to push more base commits from."""
    origin = tmp / "origin.git"
    git(tmp, "init", "--bare", str(origin))
    seed = tmp / "seed"
    git(tmp, "clone", str(origin), str(seed))
    git(seed, "checkout", "-b", "dev")
    commit(seed, "app.txt", "one\n", "base")
    git(seed, "push", "origin", "dev")
    return origin, seed


class BaseTestFailedTest(base.AgaTest):
    def setUp(self):
        super().setUp()
        self.tracker = FileBugTracker([base.an_issue(project="A Gaia", milestone="aga-v1")])
        self.tracker.issues[8] = {"number": 8, "status": "in_progress", "assignee": WORKER_ID}
        self.ports.tracker = self.tracker
        self.settings.premise = premise.load()
        self.orch.aga_max_actions = 10
        self.merger.pr = None                       # a local failure: no PR yet
        self.ports.ody_git = ody_git
        self.ports.in_flight = lambda: []
        self.settings.worker.max_parallel = 2
        self.checkouts, self.dropped, self.filters = [], [], []
        self.test_result = [(False, "not ok 1 AppUi::Avatar shows initials\nexpected 'AB', got ''\n# fail 1")]
        self.ports.base_checkout = self.base_checkout
        self.ports.drop_checkout = self.dropped.append
        self.ports.test_filter = self.fake_test_filter
        self.incident_id = journal.open_incident(ISSUE, INCIDENT, "2026-10-31T09:00:00+05:00")

    def base_checkout(self, issue, repo):
        folder = self.tmp / "base-checkout" / repo
        if not (folder / ".git").exists():
            folder.mkdir(parents=True)
            git(folder, "init")
            commit(folder, "a.txt", "a\n", "base")
        self.checkouts.append((issue, repo))
        return folder

    def no_pr_update(self, issue):
        raise AssertionError(f"#{issue} has no PR to update")

    def fake_test_filter(self, folder, repo, pattern):
        self.filters.append((folder, repo, pattern))
        return self.test_result[0]

    def run_verb(self, verb, args=None):
        return self.aga(INCIDENT, verb, args, incident=self.incident_id)

    def issue_workspace(self):
        """.runs/issue-7/gaia on claude-issue-7 (one commit of its own, uncommitted work), with dev moved on since."""
        origin, seed = origin_with_dev(self.tmp)
        folder = self.tmp / ".runs" / f"issue-{ISSUE}" / "gaia"
        folder.parent.mkdir(parents=True)
        git(self.tmp, "clone", str(origin), str(folder))
        git(folder, "checkout", "-b", f"claude-issue-{ISSUE}", "origin/dev")
        commit(folder, "mine.txt", "mine\n", "my change")
        (folder / "wip.txt").write_text("uncommitted\n", encoding="utf-8")
        commit(seed, "fix.txt", "the fix\n", "fix on dev")
        git(seed, "push", "origin", "dev")
        return folder

    # -- outcome 1: passes after the update ------------------------------------------------

    def test_update_workspace_merges_the_base_and_keeps_the_uncommitted_work(self):
        folder = self.issue_workspace()
        before = git(folder, "rev-parse", "HEAD")
        out = self.run_verb("update-workspace")
        self.assertEqual((out["base"], out["from"]), ("dev", before))
        self.assertTrue((folder / "fix.txt").exists())
        self.assertEqual((folder / "wip.txt").read_text(encoding="utf-8"), "uncommitted\n")
        self.assertEqual(len(git(folder, "rev-list", "--parents", "-n", "1", "HEAD").split()), 3)   # a merge
        self.assertEqual(self.pushes, [])
        self.assertEqual([a["verb"] for a in journal.actions()], ["update-workspace"])
        self.assertIn("git reset --keep", out["undo"])

    def test_then_retry_gives_it_back_and_a_second_update_is_refused(self):
        self.issue_workspace()
        self.run_verb("update-workspace")
        self.run_verb("retry")
        self.assertEqual(self.tracker.assigned, [(ISSUE, WORKER_ID)])
        with self.assertRaisesRegex(Refused, "already has origin/dev.*test-base"):
            self.run_verb("update-workspace")

    def test_update_workspace_refusals(self):
        with self.assertRaisesRegex(Refused, "no workspace"):
            self.run_verb("update-workspace")
        self.merger.pr = {"number": 12, "merged": False}
        with self.assertRaisesRegex(Refused, "has PR #12: update it on GitHub"):
            self.run_verb("update-workspace")
        self.merger.pr = None
        sessions.update("odysseus", ISSUE, running=True)
        with self.assertRaisesRegex(Refused, "run in flight"):
            self.run_verb("update-workspace")

    def test_a_base_that_does_not_merge_cleanly_changes_nothing(self):
        folder = self.issue_workspace()
        (folder / "fix.txt").write_text("my own, uncommitted\n", encoding="utf-8")     # dev adds this file too
        before = git(folder, "rev-parse", "HEAD")
        with self.assertRaisesRegex(Refused, "does not merge cleanly"):
            self.run_verb("update-workspace")
        self.assertEqual(git(folder, "rev-parse", "HEAD"), before)
        self.assertEqual((folder / "fix.txt").read_text(encoding="utf-8"), "my own, uncommitted\n")
        self.assertEqual(journal.actions(), [])

    # -- outcome 2: fails on the base -> Bug + hold ----------------------------------------

    def test_test_base_runs_the_tests_on_a_throwaway_checkout_and_drops_it(self):
        out = self.run_verb("test-base", {"tests": ", ".join(TESTS)})
        self.assertFalse(out["passed"])
        self.assertEqual((out["base"], out["tests"]), ("dev", TESTS))
        self.assertIn("expected 'AB'", out["output"])
        self.assertEqual(self.checkouts, [(ISSUE, "gaia")])
        self.assertEqual(self.dropped, [ISSUE])
        self.assertEqual(self.filters[0][1:], ("gaia", r"/AppUi::Avatar\ shows\ initials|AppUi::Tabs\ switches/"))
        self.assertNotIn(".runs", str(self.filters[0][0]))
        self.assertEqual(journal.actions()[0]["state_after"]["passed"], False)

    def test_one_test_is_filtered_by_its_name_and_a_failed_run_still_drops_the_checkout(self):
        self.run_verb("test-base", {"tests": TESTS[0]})
        self.assertEqual(self.filters[0][2], TESTS[0])

        def boom(*_):
            raise OSError("ai test crashed")
        self.ports.test_filter = boom
        with self.assertRaises(OSError):
            self.run_verb("test-base", {"tests": TESTS[0]})
        self.assertEqual(self.dropped, [ISSUE, ISSUE])

    def test_an_ai_test_that_did_not_run_the_tests_is_refused_and_not_recorded(self):
        def no_slot(*_):
            raise TalosError("ai test did not run the tests (exit 2): no free test slot")
        self.ports.test_filter = no_slot
        with self.assertRaisesRegex(Refused, "did not run on the base"):
            self.run_verb("test-base", {"tests": TESTS[0]})
        self.assertEqual(self.dropped, [ISSUE])
        with self.assertRaisesRegex(Refused, "run test-base first"):
            self.run_verb("file-bug", dict(BUG))

    def test_test_base_needs_test_names(self):
        with self.assertRaisesRegex(Refused, "--tests"):
            self.run_verb("test-base", {"tests": " , "})
        self.assertEqual(self.checkouts, [])

    def test_test_base_waits_for_room_under_the_workers_runs(self):
        self.ports.in_flight = lambda: [8]
        self.settings.worker.max_parallel = 1
        with self.assertRaisesRegex(Refused, "maxParallel"):
            self.run_verb("test-base", {"tests": TESTS[0]})
        self.assertEqual(self.checkouts, [])

    def test_fails_on_the_base_files_the_bug_and_holds_until_it_is_done(self):
        self.run_verb("test-base", {"tests": ", ".join(TESTS)})
        bug = self.run_verb("file-bug", dict(BUG))["bug"]
        created = self.tracker.created[0]
        self.assertEqual((created["type"], created["assignee"], created["project"]), ("Bug", WORKER_ID, "A Gaia"))
        self.assertIn("AppUi::Avatar shows initials", created["description"])
        self.assertIn("<code>dev</code>", created["description"])

        held = self.run_verb("hold-until-done", {"blocker": str(bug)})
        self.assertIsNone(held["pr"])
        self.assertEqual(self.tracker.issues[ISSUE]["assignee"], MY_ID)
        self.assertEqual(journal.active_hold(ISSUE)["condition"], f"done:{bug}")

        updated = []
        release = lambda: holds.release_due(self.tracker, WORKER_ID, MY_ID, "2026-10-31T12:00:00+05:00", "a",  # noqa: E731
                                            owner=OWNER, done_statuses=("done",), update_pr=self.no_pr_update,
                                            has_pr=lambda n: False,
                                            update_workspace=lambda n: (updated.append(n), branch_update.UPDATED)[1])
        self.assertEqual(release(), "")                 # the bug is still open
        self.tracker.issues[bug]["status"] = "done"
        self.assertIn("given back to the worker", release())
        self.assertEqual(updated, [ISSUE])
        self.assertEqual(self.tracker.issues[ISSUE]["assignee"], WORKER_ID)
        self.assertIn("merged the base branch into the workspace", self.tracker.comments[-1][2])
        self.assertIsNone(journal.active_hold(ISSUE))

    def test_a_base_conflict_on_release_still_goes_back_to_the_worker_with_a_note(self):
        self.run_verb("test-base", {"tests": TESTS[0]})
        bug = self.run_verb("file-bug", dict(BUG))["bug"]
        self.run_verb("hold-until-done", {"blocker": str(bug)})
        self.tracker.issues[bug]["status"] = "done"
        note = holds.release_due(self.tracker, WORKER_ID, MY_ID, "2026-10-31T12:00:00+05:00", "a", owner=OWNER,
                                 done_statuses=("done",), update_pr=self.no_pr_update, has_pr=lambda n: False,
                                 update_workspace=lambda n: branch_update.CONFLICT)
        self.assertIn("base conflicts", note)
        self.assertEqual(self.tracker.issues[ISSUE]["assignee"], WORKER_ID)
        self.assertIn("never rebase", self.tracker.comments[-1][2])

    def test_file_bug_needs_a_failing_test_base_for_that_test(self):
        with self.assertRaisesRegex(Refused, "run test-base first"):
            self.run_verb("file-bug", dict(BUG))
        self.run_verb("test-base", {"tests": TESTS[1]})
        with self.assertRaisesRegex(Refused, "did not run 'AppUi::Avatar shows initials'"):
            self.run_verb("file-bug", dict(BUG))
        self.assertEqual(self.tracker.created, [])

    def test_hold_without_a_pr_is_only_for_this_incident(self):
        other = journal.open_incident(ISSUE, "everything", "t")
        self.settings.premise = premise.parse({"tiers": {"wait": ""},
                                               "incidents": {"everything": {"tiers": ["wait"]}}})
        with self.assertRaisesRegex(Refused, "no open PR"):
            self.aga("everything", "hold-until-done", {"blocker": "8"}, incident=other)

    # -- outcome 3: passes on the base -> Odysseus -----------------------------------------

    def test_passes_on_the_base_refuses_a_bug_and_sends_it_to_the_worker(self):
        self.test_result[0] = (True, "ok 1 AppUi::Avatar shows initials\n# pass 1")
        out = self.run_verb("test-base", {"tests": TESTS[0]})
        self.assertTrue(out["passed"])
        with self.assertRaisesRegex(Refused, "passed on dev.*branch's own failure"):
            self.run_verb("file-bug", dict(BUG))
        self.run_verb("send", {"notes": "AppUi::Avatar passes on dev: your diff likely causes it.\n\nexpected 'AB'"})
        self.assertEqual(self.tracker.issues[ISSUE]["assignee"], WORKER_ID)
        self.assertIn("passes on dev", self.tracker.comments[-2][2])
        self.assertEqual(self.tracker.created, [])

    def test_cli_takes_the_test_names(self):
        args = aga.build_parser().parse_args(["test-base", "--tests", "a, b", "--why", "check the base"])
        self.assertEqual((args.verb, args.tests), ("test-base", "a, b"))


class TestVerdictTest(unittest.TestCase):
    def test_a_failing_test_is_told_from_a_run_that_did_not_test(self):
        self.assertTrue(wiring.test_verdict(0, "RESULT: PASS"))
        self.assertIs(wiring.test_verdict(1, "ember: 3 tests, 1 failed\n  FAIL AppUi::Avatar  <-- unexpected\n"
                                             "RESULT: FAIL"), False)
        self.assertIsNone(wiring.test_verdict(1, "no summary (exit 1; see log)\nRESULT: FAIL"))
        self.assertIsNone(wiring.test_verdict(2, "no test recipe for x"))
        self.assertIsNone(wiring.test_verdict(1, "FAIL fixture drift\nRESULT: FAIL"))


class MergeLocalTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())

    def test_fast_forward_current_and_conflict(self):
        origin, seed = origin_with_dev(self.tmp)
        folder = self.tmp / "work"
        git(self.tmp, "clone", str(origin), str(folder))
        git(folder, "checkout", "-b", "claude-issue-7", "origin/dev")
        result, before, after = branch_update.merge_local(wiring.git, ody_git, folder, "dev")
        self.assertEqual((result, before), (branch_update.CURRENT, after))
        commit(seed, "b.txt", "b\n", "more")
        git(seed, "push", "origin", "dev")
        result, before, after = branch_update.merge_local(wiring.git, ody_git, folder, "dev")
        self.assertEqual(result, branch_update.UPDATED)
        self.assertEqual(after, git(folder, "rev-parse", "origin/dev"))        # no commits of its own: fast-forward
        commit(folder, "app.txt", "mine\n", "mine")
        commit(seed, "app.txt", "theirs\n", "theirs")
        git(seed, "push", "origin", "dev")
        head = git(folder, "rev-parse", "HEAD")
        self.assertEqual(branch_update.merge_local(wiring.git, ody_git, folder, "dev"),
                         (branch_update.CONFLICT, head, head))
        self.assertEqual(git(folder, "status", "--porcelain"), "")


class BaseCheckoutTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())

    def test_a_detached_checkout_of_the_base_with_the_tooling_linked_then_removed(self):
        origin, seed = origin_with_dev(self.tmp)
        root, tools = self.tmp / "ws", self.tmp / "tools"
        root.mkdir()
        tools.mkdir()
        git(root, "clone", str(origin), str(root / "gaia"))
        (root / "CLAUDE.md").write_text("rules\n", encoding="utf-8")
        folder = workspaces.base_checkout(root, tools, ISSUE, "gaia")
        self.assertEqual(folder, root / ".runs" / f"base-issue-{ISSUE}" / "gaia")
        self.assertEqual(git(folder, "rev-parse", "HEAD"), git(seed, "rev-parse", "HEAD"))
        self.assertEqual(git(folder, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")      # detached, no branch
        self.assertTrue(workspaces.is_link(folder.parent / "dev-tools"))
        self.assertTrue((folder.parent / "CLAUDE.md").is_file())
        (folder / "junk.txt").write_text("left over\n", encoding="utf-8")
        again = workspaces.base_checkout(root, tools, ISSUE, "gaia")         # an earlier leftover is replaced
        self.assertFalse((again / "junk.txt").exists())
        workspaces.remove_checkout(root, again.parent)
        self.assertFalse(again.parent.exists())
        self.assertTrue(tools.is_dir())
        self.assertNotIn("base-issue", git(root / "gaia", "worktree", "list"))
        workspaces.remove_checkout(root, again.parent)                       # gone: nothing to do


if __name__ == "__main__":
    unittest.main()
