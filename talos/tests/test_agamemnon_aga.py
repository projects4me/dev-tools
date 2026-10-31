"""Unit tests for Agamemnon's aga tool: the guard, the journal, the verbs (fake Gaia and GitHub, real git in a temp
dir), undo and hold release. Run: python -m unittest discover -s tests"""

import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import db, registry, sessions  # noqa: E402
from lib.agamemnon import holds, journal, premise, tool, undo, verbs, wiring  # noqa: E402
from lib.agamemnon.verbs import Refused  # noqa: E402

WORKER_ID, MY_ID, OWNER = "w1", "a1", "o1"
ISSUE = 7
TZ = timezone(timedelta(hours=5))


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                           "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
                          cwd=cwd, capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL).stdout.strip()


class FakeTracker:
    def __init__(self, issues):
        self.issues = {i["number"]: dict(i) for i in issues}
        self.comments, self.assigned = [], []

    def issue(self, number):
        return dict(self.issues[number])

    def status_of(self, number):
        return self.issues[number]["status"]

    def assign(self, number, who):
        self.assigned.append((number, who))
        self.issues[number]["assignee"] = who

    def comment(self, number, title, body):
        self.comments.append((number, title, body))


class FakeMerger:
    def __init__(self):
        self.pr = {"number": 12, "merged": False}
        self.failed, self.reran, self.merged = [], [], []
        self.merge_result = {"state": "ok", "sha": "abc", "merged": "def"}

    def find_pr(self, repo, issue):
        return self.pr

    def check(self, repo, pr):
        return {"state": "ok"}

    def merge(self, repo, pr):
        self.merged.append(pr)
        return self.merge_result

    def failed_jobs(self, repo, sha):
        return self.failed

    def rerun(self, repo, runs):
        self.reran.extend(runs)


def an_issue(**fields):
    return {"number": ISSUE, "status": "feedback", "assignee": OWNER, "project": "A Gaia",
            "milestone": "aga-v1", **fields}


class AgaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        patcher = mock.patch.object(db, "DB_FILE", self.tmp / "talos.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = [datetime(2026, 10, 31, 10, 0, tzinfo=TZ)]
        self.tracker = FakeTracker([an_issue(), {"number": 8, "status": "in_progress", "assignee": WORKER_ID}])
        self.merger = FakeMerger()
        self.gh_calls, self.pushes = [], []
        self.premise = premise.parse({
            "tiers": {t: "" for t in ("read", "wait", "retry", "restore", "handoff", "update", "merge")},
            "incidents": {"ci_failed": {"tiers": ["read", "retry", "handoff"]},
                          "dirty_generated": {"tiers": ["read", "restore"], "generatedFiles": ["gen.txt"]},
                          "everything": {"tiers": ["read", "wait", "retry", "restore", "handoff", "update",
                                                   "merge"]}}})
        worker = SimpleNamespace(name="odysseus", display_name="Agent Odysseus", repo_for=lambda p: "gaia",
                                 dependency_done_statuses=("done", "closed"))
        self.orch = registry.Orchestration(worker="odysseus", auto_merge_milestones=("aga-v1",),
                                           aga_max_actions=3, aga_poll_seconds=60, aga_wait_max_minutes=5)
        self.settings = tool.Settings(worker=worker, worker_id=WORKER_ID, my_id=MY_ID, agent_name="agamemnon",
                                      display_name="Agent Agamemnon", orchestration=self.orch,
                                      premise=self.premise, workspace_root=self.tmp)
        self.slot = [None]
        self.ports = tool.Ports(
            tracker=self.tracker, merger=self.merger, gh=self.gh, git=wiring.git, push=self.push,
            worker_state=lambda n: sessions.get("odysseus", n), run_log=lambda n: None, in_flight=lambda: [8],
            free_slot=lambda: self.slot[0], now=lambda: self.clock[0], sleep=self.sleep)

    def gh(self, *args):
        self.gh_calls.append(args)
        return "headsha\n" if "headRefOid" in args else ""

    def push(self, cwd, *refspecs):
        self.pushes.append(refspecs)
        git(cwd, "push", "odysseus", *refspecs)

    def sleep(self, seconds):
        self.clock[0] += timedelta(seconds=seconds)

    def aga(self, incident_type, verb, args=None, why="because", incident=None):
        incident = incident or journal.open_incident(ISSUE, incident_type, "2026-10-31T09:00:00+05:00")
        self.incident = incident
        return tool.Aga(self.settings, self.ports, str(incident)).run(verb, args, why)

    def workspace(self):
        """.runs/issue-7/gaia: a clone on claude-issue-7 with an odysseus remote."""
        remote = self.tmp / "remote.git"
        git(self.tmp, "init", "--bare", str(remote))
        folder = self.tmp / ".runs" / f"issue-{ISSUE}" / "gaia"
        folder.parent.mkdir(parents=True)
        git(self.tmp, "clone", str(remote), str(folder))
        git(folder, "remote", "rename", "origin", "odysseus")
        git(folder, "checkout", "-b", f"claude-issue-{ISSUE}")
        (folder / "gen.txt").write_text("clean\n", encoding="utf-8")
        git(folder, "add", ".")
        git(folder, "commit", "-m", "first")
        git(folder, "push", "odysseus", f"claude-issue-{ISSUE}")
        return folder

    def undoer(self):
        return undo.Undoer(self.settings, self.ports, now=lambda: "2026-10-31T11:00:00+05:00")


class GuardTest(AgaTest):
    def test_no_incident_is_refused(self):
        with self.assertRaisesRegex(Refused, "no incident"):
            tool.Aga(self.settings, self.ports, None).run("status")
        with self.assertRaisesRegex(Refused, "no incident"):
            tool.Aga(self.settings, self.ports, "nope").run("status")

    def test_tier_not_in_premise_is_refused(self):
        with self.assertRaisesRegex(Refused, "tier 'update'"):
            self.aga("ci_failed", "update-branch")
        self.assertEqual(self.gh_calls, [])
        self.assertEqual(journal.actions(), [])

    def test_issue_outside_aga_milestones_is_refused(self):
        self.tracker.issues[ISSUE]["milestone"] = "Claude-v1"
        with self.assertRaisesRegex(Refused, "not an aga- one"):
            self.aga("ci_failed", "status")

    def test_acting_needs_a_reason(self):
        with self.assertRaisesRegex(Refused, "--why"):
            self.aga("ci_failed", "retry", why=" ")

    def test_closed_incident_is_refused(self):
        incident = journal.open_incident(ISSUE, "ci_failed", "t")
        journal.close_incident(incident, "t2", "resolved")
        with self.assertRaisesRegex(Refused, "closed"):
            self.aga("ci_failed", "status", incident=incident)

    def test_action_cap_per_incident(self):
        self.slot[0] = 1
        incident = journal.open_incident(ISSUE, "everything", "t")
        for _ in range(3):
            self.aga("everything", "wait-for", {"what": "slot"}, incident=incident)
        with self.assertRaisesRegex(Refused, "the most allowed"):
            self.aga("everything", "wait-for", {"what": "slot"}, incident=incident)
        self.aga("everything", "status", incident=incident)        # reads are not capped

    def test_every_verb_names_a_known_tier(self):
        shipped = premise.load()
        for verb in verbs.VERBS.values():
            self.assertIn(verb.tier, shipped.tiers, verb.name)


class VerbTest(AgaTest):
    def test_status_reads_without_journaling(self):
        sessions.update("odysseus", ISSUE, handoff_reason="ci_failed")
        out = self.aga("ci_failed", "status")
        self.assertEqual(out["talos"]["handoff_reason"], "ci_failed")
        self.assertEqual(out["pr"]["number"], 12)
        self.assertEqual(journal.actions(), [])

    def test_retry_is_journaled_and_undone(self):
        out = self.aga("ci_failed", "retry", why="CI flaked; the code is fine")
        self.assertEqual(self.tracker.issues[ISSUE]["assignee"], WORKER_ID)
        row = journal.action(out["action"])
        self.assertEqual(row["verb"], "retry")
        self.assertEqual(row["why"], "CI flaked; the code is fine")
        self.assertEqual(row["state_before"]["assignee"], OWNER)
        self.assertEqual(row["state_after"]["assignee"], WORKER_ID)
        self.undoer().undo(out["action"])
        self.assertEqual(self.tracker.issues[ISSUE]["assignee"], OWNER)
        self.assertIsNotNone(journal.action(out["action"])["undone_at"])
        with self.assertRaisesRegex(undo.UndoRefused, "already undone"):
            self.undoer().undo(out["action"])

    def test_undo_refuses_when_the_state_moved(self):
        out = self.aga("ci_failed", "retry")
        sessions.update("odysseus", ISSUE, running=True)       # the worker picked it up
        with self.assertRaisesRegex(undo.UndoRefused, "moved"):
            self.undoer().undo(out["action"])
        self.assertEqual(self.tracker.issues[ISSUE]["assignee"], WORKER_ID)

    def test_retry_refused_when_running_or_already_with_the_worker(self):
        sessions.update("odysseus", ISSUE, running=True)
        with self.assertRaisesRegex(Refused, "in flight"):
            self.aga("ci_failed", "retry")
        self.tracker.issues[ISSUE]["assignee"] = WORKER_ID
        with self.assertRaisesRegex(Refused, "already assigned"):
            self.aga("ci_failed", "retry")

    def test_send_posts_the_notes_then_assigns(self):
        self.aga("ci_failed", "send", {"notes": "Fix the failing lint step."})
        self.assertIn("Fix the failing lint step.", self.tracker.comments[0][2])
        self.assertEqual(self.tracker.assigned, [(ISSUE, WORKER_ID)])

    def test_rerun_ci_needs_a_failed_job(self):
        with self.assertRaisesRegex(Refused, "no failed CI job"):
            self.aga("ci_failed", "rerun-ci")
        self.merger.failed = [{"name": "tests", "run": "99"}]
        out = self.aga("ci_failed", "rerun-ci")
        self.assertEqual(self.merger.reran, ["99"])
        self.undoer().undo(out["action"])         # nothing changed: marked undone

    def test_wait_for_issue_gives_up_at_the_cap(self):
        out = self.aga("everything", "wait-for", {"what": "8", "minutes": 60})
        self.assertFalse(out["ready"])
        self.assertEqual(out["seconds"], 300)       # capped at aga_wait_max_minutes
        self.tracker.issues[8]["status"] = "done"
        self.assertTrue(self.aga("everything", "wait-for", {"what": "8"})["ready"])

    def test_hold_until_tomorrow_and_release(self):
        out = self.aga("everything", "hold-until-tomorrow")
        self.assertEqual(out["heldUntil"], "2026-11-01T06:00:00+05:00")
        self.assertEqual(self.tracker.issues[ISSUE]["assignee"], MY_ID)
        self.assertEqual(holds.release_due(self.tracker, WORKER_ID, MY_ID, "2026-11-01T05:59:00+05:00", "a"), "")
        note = holds.release_due(self.tracker, WORKER_ID, MY_ID, "2026-11-01T06:00:00+05:00", "a")
        self.assertIn("given back", note)
        self.assertEqual(self.tracker.issues[ISSUE]["assignee"], WORKER_ID)
        self.assertIsNone(journal.active_hold(ISSUE))
        with self.assertRaisesRegex(undo.UndoRefused, "not held"):
            self.undoer().undo(out["action"])

    def test_hold_undo_gives_it_back(self):
        out = self.aga("everything", "hold-until-tomorrow")
        self.undoer().undo(out["action"])
        self.assertEqual(self.tracker.issues[ISSUE]["assignee"], OWNER)
        self.assertIsNone(journal.active_hold(ISSUE))

    def test_hold_refused_for_in_review(self):
        self.tracker.issues[ISSUE]["status"] = "in_review"
        with self.assertRaisesRegex(Refused, "in_review"):
            self.aga("everything", "hold-until-tomorrow")

    def test_update_branch_cannot_be_undone(self):
        out = self.aga("everything", "update-branch")
        self.assertTrue(self.gh_calls[-1][3].endswith("/pulls/12/update-branch"))
        self.assertIn("expected_head_sha=headsha", self.gh_calls[-1])
        with self.assertRaisesRegex(undo.UndoRefused, "cannot be undone"):
            self.undoer().undo(out["action"])

    def test_merge_only_in_auto_merge_milestones_and_through_the_gate(self):
        self.merger.merge_result = {"state": "fail", "checks": []}
        with self.assertRaisesRegex(Refused, "did not pass"):
            self.aga("everything", "merge")
        self.assertEqual(journal.actions(), [])
        self.merger.merge_result = {"state": "ok", "sha": "abc", "merged": "def"}
        self.aga("everything", "merge")
        self.tracker.issues[ISSUE]["milestone"] = "aga-other"
        with self.assertRaisesRegex(Refused, "autoMergeMilestones"):
            self.aga("everything", "merge")

    def test_merge_daily_cap(self):
        self.orch.max_auto_merges_per_day = 1
        self.aga("everything", "merge")
        with self.assertRaisesRegex(Refused, "merges today"):
            self.aga("everything", "merge")


class WorkspaceVerbTest(AgaTest):
    def test_backup_restore_and_undo(self):
        folder = self.workspace()
        (folder / "gen.txt").write_text("dirty\n", encoding="utf-8")
        (folder / "new.txt").write_text("untracked\n", encoding="utf-8")
        incident = journal.open_incident(ISSUE, "dirty_generated", "t")
        with self.assertRaisesRegex(Refused, "backup first"):
            self.aga("dirty_generated", "restore", {"paths": ["gen.txt"]}, incident=incident)
        with self.assertRaisesRegex(Refused, "not known generated"):
            self.aga("dirty_generated", "restore", {"paths": ["other.txt"]}, incident=incident)
        backup = self.aga("dirty_generated", "backup", incident=incident)
        self.assertTrue(backup["ref"].startswith(f"refs/agamemnon/backup/{ISSUE}/"))
        self.assertEqual(git(folder, "show", f"{backup['ref']}:gen.txt"), "dirty")
        self.assertEqual(git(folder, "show", f"{backup['ref']}:new.txt"), "untracked")
        self.assertIn(backup["ref"], git(folder, "ls-remote", "odysseus"))
        self.assertIn("?? new.txt", git(folder, "status", "--porcelain"))      # the workspace is untouched
        restored = self.aga("dirty_generated", "restore", {"paths": ["gen.txt"]}, incident=incident)
        self.assertEqual((folder / "gen.txt").read_text(encoding="utf-8"), "clean\n")
        with self.assertRaisesRegex(undo.UndoRefused, "undo it first"):     # the restore still needs the backup
            self.undoer().undo(backup["action"])
        self.undoer().undo(restored["action"])
        self.assertEqual((folder / "gen.txt").read_text(encoding="utf-8").strip(), "dirty")
        self.undoer().undo(backup["action"])
        self.assertNotIn(backup["ref"], git(folder, "ls-remote", "odysseus"))

    def test_undo_since_goes_newest_first_and_stops_at_a_moved_state(self):
        folder = self.workspace()
        (folder / "gen.txt").write_text("dirty\n", encoding="utf-8")
        incident = journal.open_incident(ISSUE, "dirty_generated", "t")
        self.aga("dirty_generated", "backup", incident=incident)
        self.aga("dirty_generated", "restore", {"paths": ["gen.txt"]}, incident=incident)
        (folder / "gen.txt").write_text("someone else\n", encoding="utf-8")
        with self.assertRaisesRegex(undo.UndoRefused, "moved"):
            self.undoer().undo_since("2026-10-31")
        self.assertTrue(all(a["undone_at"] is None for a in journal.actions()))      # the backup stays too

    def test_push_merge_pushes_only_a_committed_merge(self):
        folder = self.workspace()
        incident = journal.open_incident(ISSUE, "everything", "t")
        with self.assertRaisesRegex(Refused, "not a merge commit"):
            self.aga("everything", "push-merge", incident=incident)
        git(folder, "checkout", "-b", "base")
        (folder / "base.txt").write_text("b\n", encoding="utf-8")
        git(folder, "add", ".")
        git(folder, "commit", "-m", "base")
        git(folder, "checkout", f"claude-issue-{ISSUE}")
        (folder / "mine.txt").write_text("m\n", encoding="utf-8")
        git(folder, "add", ".")
        git(folder, "commit", "-m", "mine")
        git(folder, "merge", "--no-edit", "base")
        out = self.aga("everything", "push-merge", incident=incident)
        self.assertEqual(git(folder, "ls-remote", "odysseus", f"refs/heads/claude-issue-{ISSUE}").split()[0],
                         out["pushed"])

    def test_overlap_lists_shared_files(self):
        folder = self.workspace()
        other = self.tmp / ".runs" / "issue-8" / "gaia"
        other.parent.mkdir(parents=True)
        git(self.tmp, "clone", "-b", f"claude-issue-{ISSUE}", str(self.tmp / "remote.git"), str(other))
        for f in (folder, other):
            git(f, "update-ref", "refs/remotes/origin/dev", "HEAD")
            (f / "shared.txt").write_text(str(f), encoding="utf-8")
            git(f, "add", ".")
            git(f, "commit", "-m", "change")
        with mock.patch("lib.workspaces.base_branch", return_value="dev"):
            out = self.aga("ci_failed", "overlap")
        self.assertEqual(out["overlap"], {"8": ["shared.txt"]})


if __name__ == "__main__":
    unittest.main()
