"""Live auto-merge milestones (orchestrator/milestones.py): a config change takes effect without a restart, an invalid
file keeps the last good list, and an orchestrator that started with none can get some."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import registry  # noqa: E402
from lib.config import TalosError  # noqa: E402
from lib.orchestrator import merging, milestones, routes  # noqa: E402

MERGER_ID = "60"


def issue(number, milestone="aga-v1", status="in_review", assignee=MERGER_ID):
    return {"number": number, "milestone": milestone, "status": status, "assignee": assignee, "project": "A Gaia"}


class Files:
    """agent.json and agent.local.json in a temp folder, each write moving the modification time forward."""

    def __init__(self, base=None, local=None):
        root = Path(tempfile.mkdtemp())
        self.agents, self.local_dir = root / "agents", root / "local"
        self.json = self.agents / "agamemnon" / "agent.json"
        self.local = self.local_dir / "talos" / "agents" / "agamemnon" / "agent.local.json"
        self.json.parent.mkdir(parents=True)
        self.tick = 1_700_000_000
        self.write(self.json, base or {"name": "agamemnon", "kind": "orchestrator", "worker": "odysseus"})
        if local is not None:
            self.write(self.local, local)

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        self.tick += 10
        os.utime(path, (self.tick, self.tick))

    def patches(self):
        return (mock.patch.object(registry, "AGENTS_DIR", self.agents),
                mock.patch.object(registry, "LOCAL_DIR", self.local_dir))


class ReadTest(unittest.TestCase):
    def test_reads_agent_json_overlaid_by_the_local_file(self):
        files = Files(local={"autoMergeMilestones": [" aga-CustomControls ", "aga-UI Cleanups"]})
        a, b = files.patches()
        with a, b:
            self.assertEqual(registry.read_auto_merge_milestones("agamemnon"), ("aga-CustomControls", "aga-UI Cleanups"))

    def test_invalid_values_and_files_are_errors(self):
        for local in ({"autoMergeMilestones": "aga-v1"}, {"autoMergeMilestones": [""]}, "{not json"):
            files = Files(local=local)
            a, b = files.patches()
            with a, b, self.assertRaises(TalosError):
                registry.read_auto_merge_milestones("agamemnon")


class LiveTest(unittest.TestCase):
    def setUp(self):
        self.files = Files(local={"autoMergeMilestones": ["aga-v1"]})
        self.logged = []
        a, b = self.files.patches()
        a.start(), b.start()
        self.addCleanup(a.stop), self.addCleanup(b.stop)
        self.reads = []

        def read():
            self.reads.append(1)
            return registry.read_auto_merge_milestones("agamemnon")
        self.live = milestones.LiveMilestones("agamemnon", ("aga-v1",), registry.config_files("agamemnon"), read,
                                              log=lambda text, name: self.logged.append(text))

    def test_unchanged_files_are_not_read_again(self):
        self.assertEqual((self.live(), self.live()), (("aga-v1",), ("aga-v1",)))
        self.assertEqual(self.reads, [])

    def test_an_added_milestone_takes_effect_without_a_restart(self):
        self.files.write(self.files.local, {"autoMergeMilestones": ["aga-v1", "aga-CustomControls"]})
        self.assertEqual(self.live(), ("aga-v1", "aga-CustomControls"))
        self.assertIn("now aga-v1, aga-CustomControls", self.logged[0])

    def test_an_invalid_file_keeps_the_last_good_list_and_says_so_once(self):
        self.files.write(self.files.local, "{half saved")
        self.assertEqual((self.live(), self.live()), (("aga-v1",), ("aga-v1",)))
        self.assertEqual(len(self.logged), 1)
        self.assertIn("unchanged (aga-v1)", self.logged[0])
        self.files.write(self.files.local, {"autoMergeMilestones": ["aga-v2"]})     # fixed: picked up again
        self.assertEqual(self.live(), ("aga-v2",))

    def test_removing_the_local_file_falls_back_to_agent_json(self):
        self.files.local.unlink()
        self.assertEqual(self.live(), ())

    def test_live_starts_from_the_loaded_value_and_reads_the_agents_files(self):
        agent = SimpleNamespace(name="agamemnon",
                                orchestration=registry.Orchestration(worker="odysseus", auto_merge_milestones=("aga-v1",)))
        live = milestones.live(agent)
        self.assertEqual(live(), ("aga-v1",))
        self.files.write(self.files.local, {"autoMergeMilestones": ["aga-v3"]})
        with mock.patch.object(milestones, "log"):
            self.assertEqual(live(), ("aga-v3",))

    def test_a_save_in_the_same_tick_is_still_seen(self):
        self.files.write(self.files.local, {"autoMergeMilestones": ["aga-v1"]})
        self.live()
        tick = self.files.local.stat().st_mtime
        self.files.local.write_text(json.dumps({"autoMergeMilestones": ["aga-v1", "aga-v22"]}), encoding="utf-8")
        os.utime(self.files.local, (tick, tick))
        self.assertEqual(self.live(), ("aga-v1", "aga-v22"))


class ConsumersTest(unittest.TestCase):
    def agent(self, merge=()):
        return SimpleNamespace(name="agamemnon", display_name="Agent Agamemnon", kind="orchestrator",
                               orchestration=registry.Orchestration(worker="odysseus", auto_merge_milestones=merge))

    def test_the_route_follows_the_live_list(self):
        current = [()]
        route = routes.MergeRoute(self.agent(), lambda: MERGER_ID, lambda: False, milestones=lambda: current[0])
        self.assertIsNone(route(issue(1)))
        current[0] = ("aga-v1",)
        self.assertEqual(route(issue(1)), (MERGER_ID, "Agent Agamemnon"))

    def test_every_orchestrator_gets_a_route_even_without_milestones(self):
        tracker = mock.Mock()
        with mock.patch.object(routes, "Tracker", return_value=tracker), mock.patch.object(routes, "Merger"):
            found = routes.merge_routes([self.agent(), SimpleNamespace(kind="issues")])
        self.assertEqual(list(found), ["odysseus"])

    def test_the_merge_sweep_follows_the_live_list(self):
        current = [()]
        worker = SimpleNamespace(name="odysseus", projects={"A Gaia": "gaia"})
        sweeper = merging.MergeSweeper(self.agent(), worker, mock.Mock(), mock.Mock(), MERGER_ID, ["1"],
                                       milestones=lambda: current[0])
        self.assertFalse(sweeper.enabled())
        self.assertEqual(sweeper.sweep([issue(1)], ()), "")
        current[0] = ("aga-v1",)
        self.assertTrue(sweeper.enabled())


if __name__ == "__main__":
    unittest.main()
