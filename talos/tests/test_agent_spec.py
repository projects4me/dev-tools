"""Gaia agentspec -> Talos settings."""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import agent_spec  # noqa: E402
from lib.config import TalosError  # noqa: E402

SPEC = {
    "specVersion": 1,
    "agent": {"id": "a-1", "userId": "u-1", "name": "Agent Achilles", "purpose": "Codes", "sponsorId": None},
    "config": {"rolePrompt": "You are Achilles.", "modelConfig": {"model": "claude-sonnet-5-5", "effort": "medium"},
               "maxTurns": 80, "maxRunsPerIssue": 6, "permissionMode": "acceptEdits"},
    "toolGrants": [],
    "skills": [],
}
FALLBACK = {"max_turns": 150, "max_iterations": 10, "permission_mode": "dontAsk"}


def spec(**config):
    s = copy.deepcopy(SPEC)
    s["config"].update(config)
    return s


class SettingsFromTest(unittest.TestCase):
    def test_maps_role_prompt_model_limits_and_permission_mode(self):
        self.assertEqual(agent_spec.settings_from(SPEC, FALLBACK), {
            "role_prompt": "You are Achilles.", "model_default": "claude-sonnet-5-5",
            "model_escalate": "claude-sonnet-5-5", "model_by_complexity": {}, "max_turns": 80,
            "max_iterations": 6, "permission_mode": "acceptEdits"})

    def test_escalate_and_by_complexity_come_from_model_config(self):
        out = agent_spec.settings_from(spec(modelConfig={"model": "sonnet", "escalate": "opus",
                                                         "byComplexity": {"complex": "opus"}}), FALLBACK)
        self.assertEqual((out["model_escalate"], out["model_by_complexity"]), ("opus", {"complex": "opus"}))

    def test_unset_limits_and_mode_fall_back_to_agent_json(self):
        out = agent_spec.settings_from(spec(maxTurns=None, maxRunsPerIssue=None, permissionMode=None), FALLBACK)
        self.assertEqual((out["max_turns"], out["max_iterations"], out["permission_mode"]), (150, 10, "dontAsk"))

    def test_refuses_a_permission_mode_claude_code_does_not_have(self):
        with self.assertRaisesRegex(TalosError, "permissionMode 'yolo'"):
            agent_spec.settings_from(spec(permissionMode="yolo"), FALLBACK)

    def test_refuses_a_limit_that_is_not_a_positive_whole_number(self):
        for bad in ("eighty", 0, -3, 1.5, True):
            with self.assertRaisesRegex(TalosError, "maxTurns must be a positive whole number"):
                agent_spec.settings_from(spec(maxTurns=bad), FALLBACK)
        with self.assertRaisesRegex(TalosError, "maxRunsPerIssue"):
            agent_spec.settings_from(spec(maxRunsPerIssue="6"), FALLBACK)

    def test_refuses_a_spec_without_role_prompt_or_model(self):
        with self.assertRaisesRegex(TalosError, "rolePrompt"):
            agent_spec.settings_from(spec(rolePrompt="  "), FALLBACK)
        with self.assertRaisesRegex(TalosError, "model"):
            agent_spec.settings_from(spec(modelConfig={}), FALLBACK)

    def test_refuses_another_spec_version(self):
        with self.assertRaisesRegex(TalosError, "version 2"):
            agent_spec.settings_from(dict(SPEC, specVersion=2), FALLBACK)


if __name__ == "__main__":
    unittest.main()
