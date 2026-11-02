"""Gaia agentspec -> Talos settings: the mapping, the last-good fallback and follow()."""

import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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


def agent():
    return SimpleNamespace(name="achilles", gaia_agent="Agent Achilles", role_prompt="", model_default="",
                           model_escalate="", model_by_complexity={}, max_turns=150, max_iterations=10,
                           permission_mode="dontAsk")


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


class SpecSourceTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(agent_spec, "log")
        self.log = patcher.start()
        self.addCleanup(patcher.stop)

    def test_follow_applies_the_spec_and_each_refresh_reads_it_again(self):
        a, replies = agent(), [SPEC, spec(rolePrompt="You are Achilles, v2.")]
        fetched = []

        def fetch(who):
            fetched.append(who)
            return replies.pop(0)

        refresh = agent_spec.follow(a, fetch)
        self.assertEqual((a.role_prompt, a.model_default, a.max_turns), ("You are Achilles.", "claude-sonnet-5-5", 80))
        refresh()
        self.assertEqual(a.role_prompt, "You are Achilles, v2.")
        self.assertEqual(fetched, ["Agent Achilles", "Agent Achilles"])

    def test_agent_without_gaia_agent_is_not_followed(self):
        a = agent()
        a.gaia_agent = ""
        self.assertIsNone(agent_spec.follow(a, lambda who: self.fail("fetched")))

    def test_gaia_unreachable_keeps_the_last_good_spec_and_logs_it(self):
        a, replies = agent(), [SPEC, TalosError("Gaia GET agentspec/a-1 failed")]

        def fetch(who):
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        refresh = agent_spec.follow(a, fetch)
        refresh()
        self.assertEqual((a.role_prompt, a.model_default), ("You are Achilles.", "claude-sonnet-5-5"))
        self.assertIn("keeping the last good one", self.log.call_args[0][0])

    def test_an_invalid_spec_later_keeps_the_last_good_one(self):
        a, replies = agent(), [SPEC, spec(permissionMode="yolo")]
        refresh = agent_spec.follow(a, lambda who: replies.pop(0))
        refresh()
        self.assertEqual(a.permission_mode, "acceptEdits")

    def test_no_spec_at_all_means_the_agent_does_not_start(self):
        def fetch(who):
            raise TalosError("Gaia is down")

        with self.assertRaisesRegex(TalosError, "no usable agentspec for Gaia agent 'Agent Achilles': Gaia is down"):
            agent_spec.follow(agent(), fetch)

    def test_ignored_tool_grants_and_skills_are_logged_once(self):
        granted = dict(SPEC, skills=[{"id": "s-1", "name": "x", "description": None, "version": None,
                                      "content": None}])
        refresh = agent_spec.follow(agent(), lambda who: granted)
        refresh()
        notes = [c for c in self.log.call_args_list if "not applied yet" in c[0][0]]
        self.assertEqual(len(notes), 1)


if __name__ == "__main__":
    unittest.main()
