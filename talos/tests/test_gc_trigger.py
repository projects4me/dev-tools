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


class TestContainersTest(unittest.TestCase):
    """The p4m test containers of finished issues' checkouts go with the gc, even when the workspace stays or is gone."""

    def setUp(self):
        self.root = fixture_root(self, 1, 2)
        self.agent = SimpleNamespace(runner={})
        self.statuses = {1: "done", 2: "in_progress", 3: "done"}
        self.tracker = SimpleNamespace(issue=lambda n: {"status": self.statuses[n]})
        self.testenv = mock.Mock()
        self.testenv.TestEnvError = type("TestEnvError", (Exception,), {})
        self.testenv.containers.return_value = [("box-1", "issue-1/gaia"), ("box-2", "issue-2/gaia"),
                                                ("box-3", "issue-3/prometheus"), ("box-x", "gaia")]

    def run_all(self, entries, **kw):
        return workspace_gc.collect_all(self.root, entries, lambda name: self.agent, lambda agent: self.tracker,
                                        lambda i: False, testenv=self.testenv, **kw)

    def removed(self):
        return [c.args[0] for c in self.testenv.remove.call_args_list]

    def test_removes_finished_issues_containers_only(self):
        res = self.run_all({("a", 1): {}, ("a", 2): {}, ("a", 3): {}})
        self.assertIn((1, "removed", "test container box-1"), res)
        self.assertIn((3, "removed", "test container box-3"), res)      # its workspace is already gone
        self.assertEqual(self.removed(), [["box-1"], ["box-3"]])
        self.testenv.containers.assert_called_once_with(self.root / ".runs")

    def test_a_kept_dirty_workspace_still_loses_its_containers(self):
        (self.root / ".runs" / "issue-1" / "gaia" / "new.txt").write_text("x", encoding="utf-8")
        res = self.run_all({("a", 1): {}}, only=1)
        self.assertEqual([r[1] for r in res], ["kept", "removed"])
        self.assertEqual(self.removed(), [["box-1"]])

    def test_never_while_a_run_is_in_flight_and_not_on_a_dry_run(self):
        res = self.run_all({("a", 1): {"running": 1}, ("a", 3): {}}, dry_run=True)
        self.assertIn((1, "kept", "run in flight"), res)
        self.assertIn((3, "would-remove", "test container box-3"), res)
        self.testenv.remove.assert_not_called()

    def test_docker_unreachable_leaves_them_and_the_gc_carries_on(self):
        self.testenv.containers.side_effect = self.testenv.TestEnvError("no daemon")
        res = self.run_all({("a", 1): {}})
        self.assertIn((1, "removed", "done"), res)
        self.testenv.remove.assert_not_called()

    def test_a_failed_removal_is_reported_as_kept(self):
        self.testenv.remove.side_effect = self.testenv.TestEnvError("busy")
        res = self.run_all({("a", 3): {}}, only=3)
        self.assertEqual(res, [(3, "kept", "test container box-3: busy")])


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
