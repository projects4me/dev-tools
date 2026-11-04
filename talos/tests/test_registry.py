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

    def overlay(self, **fields):
        (self.local / "agent.local.json").write_text(json.dumps(
            {"acceptAssignmentsFrom": ["7"], "handoffTo": "7", **fields}), encoding="utf-8")

    def test_dispatch_is_by_status_unless_workflows_are_chosen(self):
        self.overlay()
        self.assertEqual(registry.load_agent(self.folder).dispatch, "status")
        self.overlay(dispatch="workflow")
        self.assertEqual(registry.load_agent(self.folder).dispatch, "workflow")

    def test_an_unknown_dispatch_fails_at_load(self):
        self.overlay(dispatch="events")
        with self.assertRaises(TalosError) as e:
            registry.load_agent(self.folder)
        self.assertIn("dispatch must be one of status, workflow", str(e.exception))

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

    def gaia_agent_json(self, **changes):
        raw = json.loads((self.folder / "agent.json").read_text(encoding="utf-8"))
        raw.pop("model", None), raw.pop("rolePrompt", None)
        raw.update(gaiaAgent="Agent Achilles", **changes)
        (self.folder / "agent.json").write_text(json.dumps(raw), encoding="utf-8")
        (self.local / "agent.local.json").write_text(json.dumps(
            {"acceptAssignmentsFrom": ["7"], "handoffTo": "7"}), encoding="utf-8")

    def test_agent_without_gaia_agent_takes_role_prompt_and_model_from_agent_json(self):
        (self.local / "agent.local.json").write_text(json.dumps(
            {"acceptAssignmentsFrom": ["7"], "handoffTo": "7"}), encoding="utf-8")
        agent = registry.load_agent(self.folder)
        self.assertEqual(agent.gaia_agent, "")
        self.assertEqual(agent.role_prompt, (ODYSSEUS / "role.md").read_text(encoding="utf-8"))
        self.assertTrue(agent.model_default)

    def test_gaia_agent_needs_no_role_prompt_or_model_and_keeps_local_settings(self):
        self.gaia_agent_json()
        agent = registry.load_agent(self.folder)
        self.assertEqual((agent.gaia_agent, agent.role_prompt, agent.model_default), ("Agent Achilles", "", ""))
        self.assertTrue(agent.allowed_tools)
        self.assertTrue(agent.projects)

    def test_gaia_agent_with_role_prompt_or_model_in_agent_json_is_refused(self):
        self.gaia_agent_json(model={"default": "sonnet"})
        with self.assertRaisesRegex(TalosError, "gaiaAgent takes model from Gaia"):
            registry.load_agent(self.folder)

    def test_gaia_agent_is_for_issues_agents_only(self):
        for kind in ("ideas", "events", "orchestrator"):
            self.gaia_agent_json(kind=kind)
            with self.assertRaisesRegex(TalosError, "issues agents only"):
                registry.load_agent(self.folder)

    def test_shipped_agents_are_configured_in_agent_json(self):
        for path in (ODYSSEUS.parent).glob("*/agent.json"):
            self.assertNotIn("gaiaAgent", json.loads(path.read_text(encoding="utf-8")), path.parent.name)

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
