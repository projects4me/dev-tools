"""Routing a shipped PR by its merge review (merge_review.Router) with a faked `ai merge-review` and `ai handoff`, and
the dispatcher's pr_opened outcome."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import ai_cli, db, dispatcher as dispatcher_mod, merge_review, policy  # noqa: E402
from test_dispatcher import StubTracker, result  # noqa: E402
from test_policy import make_agent, make_issue  # noqa: E402
from lib.registry import Action  # noqa: E402

MERGER = ("60", "Agent Agamemnon")


def opened(pr=77, session="s1"):
    r = result(policy.PR_OPENED, session=session)
    r["structured_output"]["pr"] = pr
    return r


class RouterTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(merge_review, "log", lambda *a, **k: None)
        patch.start()
        self.addCleanup(patch.stop)
        self.tracker = StubTracker(make_issue(status="in_progress"))
        self.bodies = []
        self.tracker.comment = lambda number, title, body: self.bodies.append((number, title, body))
        self.sent, self.reviewed = [], []

    def router(self, state, route=None, find_pr=None):
        def review(repo, pr):
            self.reviewed.append((repo, pr))
            if isinstance(state, Exception):
                raise state
            return state
        return merge_review.Router("odysseus", "Agent Odysseus", self.tracker, review=review,
                                   handoff=lambda number, to=None: self.sent.append((number, to)),
                                   route=route, find_pr=find_pr)

    def test_clean_goes_to_the_reviewer_without_a_route(self):
        self.router("clean").route_pr({"number": 42}, "gaia", 77)
        self.assertEqual((self.reviewed, self.sent, self.bodies), ([("gaia", 77)], [(42, None)], []))

    def test_clean_goes_to_the_merging_orchestrator_when_routed(self):
        self.router("clean", route=lambda issue: MERGER).route_pr({"number": 42}, "gaia", 77)
        self.assertEqual(self.sent, [(42, "60")])

    def test_unreviewed_goes_to_the_reviewer_with_a_comment_even_when_routed(self):
        self.router("unreviewed", route=lambda issue: MERGER).route_pr({"number": 42}, "gaia", 77)
        self.assertEqual(self.sent, [(42, None)])
        self.assertEqual(len(self.bodies), 1)
        self.assertIn("records no clean design review", self.bodies[0][2])

    def test_an_unreadable_review_goes_to_the_reviewer_with_a_comment(self):
        self.router(merge_review.TalosError("gh down"), route=lambda issue: MERGER).route_pr({"number": 42}, "gaia", 77)
        self.assertEqual(self.sent, [(42, None)])
        self.assertIn("could not be read", self.bodies[0][2])
        self.assertIn("merge review unreadable", self.bodies[0][1])

    def test_a_failed_comment_does_not_block_the_hand_off(self):
        router = self.router("unreviewed")

        def broken(number, title, body):
            raise merge_review.TalosError("Gaia is down")
        self.tracker.comment = broken
        router.route_pr({"number": 42}, "gaia", 77)
        self.assertEqual(self.sent, [(42, None)])

    def test_a_failed_hand_off_raises(self):
        def broken(number, to=None):
            raise merge_review.TalosError("Gaia is down")
        router = self.router("clean")
        router.handoff = broken
        with self.assertRaises(merge_review.TalosError):
            router.route_pr({"number": 42}, "gaia", 77)

    def test_an_addressed_re_review_goes_to_the_merger_when_clean(self):
        open_pr = lambda repo, number: {"number": 77, "merged": False}  # noqa: E731
        self.assertFalse(self.router("clean", route=lambda issue: None, find_pr=open_pr).route_after_review({"number": 42}, "gaia"))
        self.assertTrue(self.router("clean", route=lambda issue: MERGER, find_pr=open_pr).route_after_review({"number": 42}, "gaia"))
        self.assertEqual(self.sent, [(42, "60")])

    def test_route_after_review_leaves_it_with_the_reviewer_otherwise(self):
        route = lambda issue: MERGER  # noqa: E731
        merged = lambda repo, number: {"number": 77, "merged": True}  # noqa: E731
        open_pr = lambda repo, number: {"number": 77, "merged": False}  # noqa: E731
        self.assertFalse(self.router("clean", route=route, find_pr=merged).route_after_review({"number": 42}, "gaia"))
        self.assertFalse(self.router("unreviewed", route=route, find_pr=open_pr).route_after_review({"number": 42}, "gaia"))
        self.assertFalse(self.router("clean", route=route, find_pr=lambda repo, number: None).route_after_review({"number": 42}, "gaia"))
        self.assertFalse(self.router("clean", route=route).route_after_review({"number": 42}, "gaia"))      # no find_pr
        self.assertEqual((self.sent, self.bodies), ([], []))


class PrOpenedTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.patches = [mock.patch.object(db, "DB_FILE", tmp / "talos.db"),
                        mock.patch.object(dispatcher_mod, "log", lambda *a, **k: None),
                        mock.patch.object(merge_review, "log", lambda *a, **k: None),
                        mock.patch.object(dispatcher_mod, "lease_file", lambda name: tmp / f"{name}-leases.json"),
                        mock.patch.object(dispatcher_mod.runner.workspaces, "ensure",
                                          lambda root, tools, number, repo: tmp / f"issue-{number}")]
        for p in self.patches:
            p.start()
        self.routed = []

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def dispatcher(self):
        self.tracker = StubTracker(make_issue(status="in_progress"))
        agent = make_agent(actions={"in_progress": Action("Continue #{issue}", resume=True)},
                           pickup_status="in_progress")
        d = dispatcher_mod.Dispatcher(agent, self.tracker, "claude")
        d.merge_review.route_pr = lambda issue, repo, pr: self.routed.append((issue["number"], repo, pr))
        return d

    def run_with(self, d, outcome):
        with mock.patch.object(dispatcher_mod.runner, "run", lambda *a, **k: (outcome, Path("log.json"))), \
                mock.patch.object(dispatcher_mod.runner, "build_command", lambda *a, **k: ["claude"]):
            return d.handle(42)

    def test_pr_opened_routes_the_issue_at_once(self):
        d = self.dispatcher()
        self.assertEqual(self.run_with(d, opened()), policy.PR_OPENED)
        self.assertEqual(self.routed, [(42, "gaia", 77)])
        self.assertEqual(self.tracker.handoffs, [])

    def test_pr_opened_without_a_pr_fails(self):
        d = self.dispatcher()
        self.assertEqual(self.run_with(d, result(policy.PR_OPENED)), "failed")
        self.assertEqual(self.routed, [])

    def test_a_failed_route_hands_the_issue_to_a_person(self):
        d = self.dispatcher()

        def broken(issue, repo, pr):
            raise merge_review.TalosError("Gaia is down")

        d.merge_review.route_pr = broken
        self.assertEqual(self.run_with(d, opened()), "failed")
        self.assertEqual(len(self.tracker.handoffs), 1)
        self.assertIn("hand-off failed", self.tracker.comments[0])

    def test_read_pr(self):
        self.assertEqual(policy.read_pr(opened(pr=12)), 12)
        self.assertEqual(policy.read_pr(opened(pr="#12")), 12)
        self.assertIsNone(policy.read_pr(result("done")))
        self.assertIsNone(policy.read_pr(None))


class AiCommandsTest(unittest.TestCase):
    def run_ai(self, stdout, returncode=0):
        done = mock.Mock(returncode=returncode, stdout=stdout, stderr="gh: not found")
        return mock.patch.object(ai_cli.subprocess, "run", return_value=done)

    def test_merge_review_reads_the_state(self):
        with self.run_ai('{"state": "unreviewed", "detail": "x"}\n', 1) as run:
            self.assertEqual(ai_cli.ai_merge_review({}, "gaia", 77), "unreviewed")
        self.assertEqual(run.call_args[0][0][1:], [str(ai_cli.AI), "merge-review", "gaia", "77", "--json"])
        with self.run_ai('{"state": "clean"}\n'):
            self.assertEqual(ai_cli.ai_merge_review({}, "gaia", 77), "clean")

    def test_merge_review_errors(self):
        for out in ("", '{"state": "pending"}\n'):
            with self.run_ai(out, 1), self.assertRaises(merge_review.TalosError):
                ai_cli.ai_merge_review({}, "gaia", 77)

    def test_handoff(self):
        with self.run_ai("", 0) as run:
            ai_cli.ai_handoff({}, 42, to="60")
        self.assertEqual(run.call_args[0][0][2:], ["handoff", "42", "--to", "60"])
        with self.run_ai("", 1), self.assertRaises(merge_review.TalosError):
            ai_cli.ai_handoff({}, 42)


if __name__ == "__main__":
    unittest.main()
