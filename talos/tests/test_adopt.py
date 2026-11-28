"""Owner-held PRs in auto-merge milestones (adt-5045): the orchestrator takes them to merge when the gate allows, the
merge sweep hands a still-red CI to the worker up to maxCiFixAttempts times, and none of it can loop."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import sessions  # noqa: E402
from lib.config import TalosError  # noqa: E402
from lib.orchestrator import policy  # noqa: E402
from lib.orchestrator import state as store  # noqa: E402
from lib.orchestrator.adopt import Adopter  # noqa: E402
from lib.orchestrator.merging import CI_FIX_ACTION, MergeSweeper  # noqa: E402
from test_orchestrator import (MERGER_ID, OWNER, PROJECTS, WORKER_ID, FakeMerger, FakeTracker, SweepTest,  # noqa: E402
                               gate, issue)

MILESTONES = ("aga-v1",)
CI_FAIL = ("ci", "failed: Build-and-Test (failure)")


def held(number=5, **fields):
    """In review with the owner, last changed by the worker's hand-off."""
    return issue(number, **{"status": "in_review", "assignee": OWNER, "modifiedUser": WORKER_ID, **fields})


class PolicyTest(unittest.TestCase):
    def skip(self, item, state=None):
        return policy.adopt_skip_reason(item, MILESTONES, PROJECTS, [OWNER], WORKER_ID, state or {})

    def test_only_an_owner_held_issue_untouched_since_the_hand_off(self):
        self.assertIsNone(self.skip(held()))
        self.assertIn("milestone", self.skip(held(milestone="aga-v2")))
        self.assertIn("status", self.skip(held(status="feedback")))         # blocked or a question: left alone
        self.assertIn("status", self.skip(held(status="pending")))
        self.assertIn("owner", self.skip(held(assignee=MERGER_ID)))
        self.assertIn("changed by someone", self.skip(held(modifiedUser=OWNER)))      # the owner touched it
        self.assertIn("changed by someone", self.skip(held(modifiedUser=MERGER_ID)))  # the merge sweep's hand-back
        self.assertIn("run is in flight", self.skip(held(), {"running": 1}))
        self.assertIn("project", self.skip(held(project="A Hermes")))

    def test_adoptable_gates(self):
        self.assertTrue(policy.adoptable(gate("ok"), None))
        self.assertTrue(policy.adoptable(gate("pending", ("ci", "running")), None))
        self.assertTrue(policy.adoptable(gate("fail", CI_FAIL), None))
        self.assertFalse(policy.adoptable(gate("fail", CI_FAIL, ("people", "hammad commented")), None))
        self.assertFalse(policy.adoptable(gate("fail", ("labels", "do-not-merge")), None))
        self.assertFalse(policy.adoptable(gate("ok"), gate("ok")["sha"]))          # this head was taken before

    def test_ci_fix_steps(self):
        self.assertEqual(policy.ci_fix_step(2, 3), policy.FIX)
        self.assertEqual(policy.ci_fix_step(3, 3), policy.TELL)
        self.assertEqual(policy.ci_fix_step(0, 0), policy.TELL)


class AdoptTest(SweepTest):
    def setUp(self):
        super().setUp()
        for target in ("lib.orchestrator.adopt.log", "lib.orchestrator.merging.log"):
            patch = mock.patch(target)
            patch.start()
            self.addCleanup(patch.stop)

    def wire(self, tracker, merger, max_ci_fix_attempts=3, **settings):
        o = self.orchestrator(tracker, auto_merge_milestones=MILESTONES, max_ci_fix_attempts=max_ci_fix_attempts,
                              **settings)
        o.merges = MergeSweeper(o.agent, o.worker, tracker, merger, MERGER_ID, [OWNER], now=lambda: self.clock[0],
                                worker_id=WORKER_ID)
        o.adopter = Adopter(o.agent, o.worker, tracker, merger, WORKER_ID, MERGER_ID, [OWNER],
                            lambda: o.agent.orchestration.auto_merge_milestones)
        return o

    def test_a_clean_owner_held_pr_is_taken_and_merged(self):
        tracker = FakeTracker([held()])
        merger = FakeMerger(gate("ok"))
        o = self.wire(tracker, merger)
        o.sweep()
        self.assertEqual(tracker.issues[5]["assignee"], MERGER_ID)
        self.assertEqual(sessions.get("odysseus", 5)["adopted_head"], gate("ok")["sha"])
        self.assertIn("taken to merge automatically", tracker.comments[-1][1])
        o.sweep()                                   # the merge sweep merges it on the next sweep
        self.assertEqual(merger.merged, [("gaia", 77)])
        self.assertEqual(tracker.issues[5]["status"], "done")

    def test_anything_else_stays_with_the_owner(self):
        for failing in ((("tests", "no passing full test run"),), (CI_FAIL, ("people", "hammad commented"))):
            tracker = FakeTracker([held()])
            self.wire(tracker, FakeMerger(gate("fail", *failing))).sweep()
            self.assertEqual((tracker.issues[5]["assignee"], tracker.comments), (OWNER, []))

    def test_an_unfinished_blocking_issue_or_a_run_leaves_it_alone(self):
        tracker = FakeTracker([held(dependsOn=[9]), held(6)], dependencies={9: "in_progress"})
        sessions.update("odysseus", 6, running=1)
        self.wire(tracker, FakeMerger(gate("ok"))).sweep()
        self.assertEqual([tracker.issues[n]["assignee"] for n in (5, 6)], [OWNER, OWNER])

    def test_nothing_is_taken_while_paused(self):
        tracker = FakeTracker([held()])
        o = self.wire(tracker, FakeMerger(gate("ok")))
        store.update("agamemnon", paused=1, pause_reason="talos pause")
        o.sweep()
        self.assertEqual(tracker.issues[5]["assignee"], OWNER)

    def test_red_ci_goes_to_the_worker_three_times_then_to_the_owner(self):
        """Loop guard 1: the attempt counter. Each fix pushes a new head that the PR hand-off gives the merger."""
        tracker = FakeTracker([held()])
        merger = FakeMerger(gate("fail", CI_FAIL))
        o = self.wire(tracker, merger, ci_rerun_attempts=0)
        for attempt in (1, 2, 3):
            head = f"head{attempt}"
            merger.gates = [{**gate("fail", CI_FAIL), "sha": head}]
            o.sweep()                               # taken from the owner (first time) or merged-route
            o.sweep()                               # the merge sweep: CI stays red, so the worker fixes it
            self.assertEqual(tracker.issues[5]["assignee"], WORKER_ID, attempt)
            st = sessions.get("odysseus", 5)
            self.assertEqual((st["next_action"], st["ci_fix_attempts"]), (CI_FIX_ACTION, attempt))
            self.assertIn(f"attempt {attempt} of 3", tracker.comments[-1][2])
            # the fix run ends pr_opened and the PR hand-off gives the issue to the merger (auto-merge milestone)
            sessions.update("odysseus", 5, next_action="")
            tracker.issues[5].update(assignee=MERGER_ID, modifiedUser=WORKER_ID)
        merger.gates = [{**gate("fail", CI_FAIL), "sha": "head4"}]
        o.sweep()
        o.sweep()
        self.assertEqual(tracker.issues[5]["assignee"], OWNER)      # out of attempts: the owner keeps it
        self.assertIn("3 CI fix attempts used", tracker.comments[-1][2])
        o.sweep()
        self.assertEqual(tracker.issues[5]["assignee"], OWNER)      # and nothing re-takes it

    def test_the_merge_sweeps_hand_back_is_never_taken_again(self):
        """Loop guard 2: the hand-back is the orchestrator's own change, and the head was already taken."""
        tracker = FakeTracker([held()])
        merger = FakeMerger(gate("ok"), gate("fail", ("people", "hammad commented")))
        o = self.wire(tracker, merger)
        o.sweep()                                   # taken
        o.sweep()                                   # someone commented meanwhile: handed back to the owner
        self.assertEqual(tracker.issues[5]["assignee"], OWNER)
        tracker.issues[5]["modifiedUser"] = WORKER_ID   # even if Gaia credited the worker, the head blocks it
        merger.gates = [gate("ok")]
        before = len(tracker.comments)
        o.sweep()
        o.sweep()
        self.assertEqual((tracker.issues[5]["assignee"], len(tracker.comments)), (OWNER, before))

    def test_the_owner_taking_it_back_ends_it(self):
        """Loop guard 3: once the owner changed the issue, it stays theirs."""
        tracker = FakeTracker([held(modifiedUser=OWNER)])
        self.wire(tracker, FakeMerger(gate("ok"))).sweep()
        self.assertEqual((tracker.issues[5]["assignee"], tracker.comments), (OWNER, []))

    def test_without_the_worker_or_attempts_red_ci_goes_to_the_owner_as_before(self):
        tracker = FakeTracker([issue(5, status="in_review", assignee=MERGER_ID)])
        merger = FakeMerger(gate("fail", CI_FAIL))
        self.wire(tracker, merger, max_ci_fix_attempts=0, ci_rerun_attempts=0).sweep()
        self.assertEqual(tracker.issues[5]["assignee"], OWNER)
        self.assertEqual(sessions.get("odysseus", 5).get("ci_fix_attempts") or 0, 0)

    def test_a_failed_hand_off_does_not_mark_the_head_taken(self):
        tracker = FakeTracker([held()])

        def broken(number, status, assignee):
            raise TalosError("Gaia down")
        tracker.hand_off = broken
        self.wire(tracker, FakeMerger(gate("ok"))).sweep()
        self.assertEqual(sessions.get("odysseus", 5).get("adopted_head") or "", "")

    def test_a_failed_assignment_does_not_use_up_an_attempt(self):
        tracker = FakeTracker([issue(5, status="in_review", assignee=MERGER_ID)])
        o = self.wire(tracker, FakeMerger(gate("fail", CI_FAIL)))

        def broken(number, assignee):
            raise TalosError("Gaia down")
        tracker.assign = broken
        o.sweep()
        st = sessions.get("odysseus", 5)
        self.assertEqual((st.get("next_action") or "", st.get("ci_fix_attempts") or 0), ("", 0))


if __name__ == "__main__":
    unittest.main()
