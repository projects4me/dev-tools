"""Agent registry: agent.local.json overlay and the reviewer placeholder."""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import policy, registry  # noqa: E402
from lib.config import TalosError  # noqa: E402

ODYSSEUS = Path(__file__).resolve().parent.parent / "agents" / "odysseus"


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp()) / "odysseus"
        self.folder.mkdir()
        for name in ("agent.json", "role.md"):
            shutil.copy(ODYSSEUS / name, self.folder / name)
        env = Path(tempfile.mkdtemp()) / "agent.env"
        env.write_text("GAIA_EMAIL=x\n", encoding="utf-8")
        raw = json.loads((self.folder / "agent.json").read_text(encoding="utf-8"))
        raw["gaiaEnv"] = str(env)   # absolute path wins over LOCAL_DIR / gaiaEnv
        (self.folder / "agent.json").write_text(json.dumps(raw), encoding="utf-8")
        local_dir = Path(tempfile.mkdtemp())     # the dev-tools checkout holding agent.local.json
        self.local = local_dir / "talos" / "agents" / "odysseus"
        self.local.mkdir(parents=True)
        patcher = mock.patch.object(registry, "LOCAL_DIR", local_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_tracked_agent_json_has_no_reviewer(self):
        with self.assertRaises(TalosError) as e:
            registry.load_agent(self.folder)
        self.assertIn("p4m configure", str(e.exception))

    def test_local_overlay_supplies_reviewer(self):
        (self.local / "agent.local.json").write_text(json.dumps(
            {"acceptAssignmentsFrom": ["7"], "handoffTo": "7", "reviewerName": "Ayesha"}), encoding="utf-8")
        agent = registry.load_agent(self.folder)
        self.assertEqual((agent.accept_from, agent.handoff_to, agent.reviewer_name), (["7"], "7", "Ayesha"))
        issue = {"number": 5, "project": "A Gaia", "status": "feedback"}
        prompt = policy.render_prompt(agent.actions["feedback"], issue, "gaia", reviewer=agent.reviewer_name)
        self.assertTrue(prompt.startswith("Ayesha answered on Projects4Me issue #5"))

    def test_odysseus_run_policy(self):
        (self.local / "agent.local.json").write_text(json.dumps(
            {"acceptAssignmentsFrom": ["7"], "handoffTo": "7"}), encoding="utf-8")
        agent = registry.load_agent(self.folder)
        self.assertEqual(policy.run_limits(agent, "opus"), (150, 15.0))
        self.assertEqual(agent.env, {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"})
        self.assertIn("ToolSearch", agent.builtin_tools.split(","))
        self.assertIn("{handoff}", agent.ship_prompt)

    def set_test_slots(self, value):
        raw = json.loads((self.folder / "agent.json").read_text(encoding="utf-8"))
        raw["testSlots"] = value
        (self.folder / "agent.json").write_text(json.dumps(raw), encoding="utf-8")
        (self.local / "agent.local.json").write_text(json.dumps(
            {"acceptAssignmentsFrom": ["7"], "handoffTo": "7"}), encoding="utf-8")

    def test_test_slots_above_p4m_env_fail_at_load(self):
        self.set_test_slots(3)
        with mock.patch.object(registry, "installed_test_slots", return_value=2):
            with self.assertRaises(TalosError) as e:
                registry.load_agent(self.folder)
        self.assertIn("TEST_SLOTS", str(e.exception))

    def test_test_slots_within_p4m_env_load(self):
        self.set_test_slots(2)
        with mock.patch.object(registry, "installed_test_slots", return_value=2):
            self.assertEqual(registry.load_agent(self.folder).test_slots, 2)

    def test_installed_test_slots_reads_p4m_env(self):
        (self.local.parents[2] / "p4m.env").write_text("TEST_SLOTS=4\n", encoding="utf-8")
        with mock.patch.dict(registry.os.environ, clear=False):
            registry.os.environ.pop("TEST_SLOTS", None)
            self.assertEqual(registry.installed_test_slots(), 4)

    def test_env_may_not_override_credentials(self):
        (self.local / "agent.local.json").write_text(json.dumps(
            {"acceptAssignmentsFrom": ["7"], "handoffTo": "7", "env": {"GAIA_EMAIL": "x"}}), encoding="utf-8")
        with self.assertRaises(TalosError) as e:
            registry.load_agent(self.folder)
        self.assertIn("GAIA_EMAIL", str(e.exception))

    def test_runner_is_local_unless_a_container_is_configured(self):
        local = {"acceptAssignmentsFrom": ["7"], "handoffTo": "7"}
        (self.local / "agent.local.json").write_text(json.dumps(local), encoding="utf-8")
        self.assertEqual(registry.load_agent(self.folder).runner, {})
        for runner, ok in (({"type": "container", "image": "p4m-talos-runner"}, True),
                           ({"type": "container"}, False), ({"type": "ssh"}, False)):
            (self.local / "agent.local.json").write_text(json.dumps({**local, "runner": runner}), encoding="utf-8")
            if ok:
                self.assertEqual(registry.load_agent(self.folder).runner, runner)
            else:
                with self.assertRaises(TalosError):
                    registry.load_agent(self.folder)

    def test_shipped_prompts_name_no_person(self):
        raw = (ODYSSEUS / "agent.json").read_text(encoding="utf-8") + (ODYSSEUS / "role.md").read_text(encoding="utf-8")
        self.assertNotIn("Hammad", raw)


if __name__ == "__main__":
    unittest.main()
