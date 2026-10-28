"""Gc beyond the workspace folders: status lookup, PR lookup, the all-issues orchestration, container leftovers and
the done trigger in the dispatcher."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import container, dispatcher as dispatcher_mod, workspace_gc  # noqa: E402
from test_dispatcher import StubTracker, make_issue  # noqa: E402
from test_policy import make_agent  # noqa: E402
from test_workspaces import WorkspacesTest  # noqa: E402


def fixture_root(case, *issues):
    fixture = WorkspacesTest("setUp")
    fixture.setUp()
    case.addCleanup(fixture.tearDown)
    for issue in issues:
        fixture.ensure(issue)
    return fixture.root


class StatusAndPrTest(unittest.TestCase):
    def test_status_of_reads_the_tracker_and_none_on_error(self):
        class Tracker:
            def issue(self, number):
                if number == 9:
                    raise RuntimeError("gaia down")
                return {"status": "done"}
        self.assertEqual(workspace_gc.status_of(1, Tracker()), "done")
        self.assertIsNone(workspace_gc.status_of(9, Tracker()))

    def test_unknown_status_keeps_the_workspace(self):
        root = fixture_root(self, 1)
        self.assertEqual(workspace_gc.collect(root, {1: None}, set())[0][:2], (1, "kept"))

    def test_pr_merged_with_a_fake_gh(self):
        def gh(out, code=0):
            return mock.patch.object(workspace_gc.subprocess, "run",
                                     return_value=subprocess.CompletedProcess([], code, out, ""))
        with mock.patch.object(workspace_gc.workspaces, "worktrees", return_value=["/w"]):
            with gh("1\n"):
                self.assertTrue(workspace_gc.pr_merged("/r", 5))
            with gh("0\n"):
                self.assertFalse(workspace_gc.pr_merged("/r", 5))
            with gh("", 1):
                self.assertFalse(workspace_gc.pr_merged("/r", 5))

    def test_failing_pr_lookup_counts_as_not_merged(self):
        root = fixture_root(self, 1)

        def broken(issue):
            raise RuntimeError("gh missing")
        res = workspace_gc.collect(root, {1: "in_review"}, set(), merged=broken)
        self.assertEqual(res[0][:2], (1, "kept"))

    def test_in_flight(self):
        self.assertEqual(workspace_gc.in_flight({("a", 1): {"running": 1}, ("a", 2): {"running": 0}}), {1})


class CollectAllTest(unittest.TestCase):
    def setUp(self):
        self.root = fixture_root(self, 1, 2)
        self.agent = SimpleNamespace(runner={"type": "container"})
        self.statuses = {1: "done", 2: "in_progress", 3: "done"}
        self.tracker = SimpleNamespace(issue=lambda n: {"status": self.statuses[n]})
        self.container = mock.Mock()
        self.container.leftovers.return_value = [("volume", "talos-a-3-work")]

    def run_all(self, entries, **kw):
        return workspace_gc.collect_all(self.root, entries, lambda name: self.agent, lambda agent: self.tracker,
                                        lambda i: False, container=self.container, **kw)

    def test_collects_a_workspace_and_container_leftovers_of_finished_issues(self):
        res = self.run_all({("a", 1): {}, ("a", 2): {}, ("a", 3): {}})
        self.assertIn((1, "removed", "done"), res)
        self.assertIn((3, "removed", "volume talos-a-3-work"), res)
        self.assertEqual([r[1] for r in res if r[0] == 2], ["kept"])
        self.container.remove_leftovers.assert_called()

    def test_dry_run_removes_nothing(self):
        res = self.run_all({("a", 1): {}, ("a", 3): {}}, dry_run=True)
        self.assertIn((3, "would-remove", "volume talos-a-3-work"), res)
        self.container.remove_leftovers.assert_not_called()
        self.assertTrue(workspace_gc.issues_on_disk(self.root))

    def test_only_one_issue_and_never_a_run_in_flight(self):
        self.assertEqual(self.run_all({("a", 1): {"running": 1}, ("a", 2): {}}, only=1),
                         [(1, "kept", "run in flight")])

    def test_unreadable_status_keeps_everything(self):
        def broken(n):
            raise RuntimeError("down")
        self.tracker.issue = broken
        res = self.run_all({("a", 1): {}, ("a", 3): {}})
        self.assertEqual({r[1] for r in res}, {"kept"})
        self.container.remove_leftovers.assert_not_called()

    def test_docker_unreachable_keeps_the_leftovers(self):
        self.container.leftovers.side_effect = OSError("no daemon")
        res = self.run_all({("a", 3): {}}, only=3)
        self.assertEqual(res[0][:2], (3, "kept"))


class LeftoversTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        patch = mock.patch.object(container, "CONTAINER_DIR", self.tmp)
        patch.start()
        self.addCleanup(patch.stop)

    def test_lists_and_removes_container_volumes_and_folder(self):
        (self.tmp / "a-7" / "claude").mkdir(parents=True)
        outputs = {"ps": "talos-a-7\n", "volume": "talos-a-7-work\ntalos-a-7-claude\n"}
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, outputs.get(cmd[1], ""), "")
        with mock.patch.object(container.subprocess, "run", side_effect=run):
            found = container.leftovers("a", 7, {})
            self.assertEqual([k for k, _ in found], ["container", "volume", "volume", "folder"])
            container.remove_leftovers("a", 7, {}, found)
        self.assertIn(["docker", "rm", "-f", "talos-a-7"], calls)
        self.assertIn(["docker", "volume", "rm", "-f", "talos-a-7-claude"], calls)
        self.assertFalse((self.tmp / "a-7").exists())

    def test_docker_failure_raises_so_nothing_is_assumed(self):
        failing = mock.patch.object(container.subprocess, "run",
                                    return_value=subprocess.CompletedProcess([], 1, "", "no daemon"))
        with failing, self.assertRaises(OSError):
            container.leftovers("a", 7, {})


class DoneTriggerTest(unittest.TestCase):
    def dispatcher(self, **kw):
        with mock.patch.object(dispatcher_mod.runner, "gaia_pack", lambda n, folder=None: ""):
            return dispatcher_mod.Dispatcher(make_agent(), StubTracker(make_issue()), "claude", **kw)

    def test_status_event_queues_a_collection_run_by_the_release_worker(self):
        calls = []
        d = self.dispatcher(collector=lambda issue: calls.append(issue) or [(issue, "removed", "done")])
        d.on_event({"eventName": "issue.status.changed", "meta": {"issueNumber": 7}})
        self.assertEqual(d._releases.get_nowait(), 7)        # not collected on the listener thread
        self.assertEqual(calls, [])
        d.collect_finished(7)
        self.assertEqual(calls, [7])

    def test_dry_run_collects_nothing_and_a_failing_gc_does_not_raise(self):
        calls = []
        self.dispatcher(dry_run=True, collector=calls.append).collect_finished(7)
        self.assertEqual(calls, [])

        def boom(issue):
            raise RuntimeError("x")
        self.dispatcher(collector=boom).collect_finished(7)


if __name__ == "__main__":
    unittest.main()
