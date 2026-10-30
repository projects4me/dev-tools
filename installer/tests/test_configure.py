"""configure: the Agamemnon step. Run: python -m unittest discover -s tests"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import configure  # noqa: E402


class FakeServer:
    class GaiaError(Exception):
        pass


class FakeGaia:
    def find(self, module, query=None, limit=None):
        return [{"id": 77, "name": "Agent Agamemnon"}]


class AgamemnonStepTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tools = Path(tmp.name)
        patches = {"TOOLS_DIR": self.tools,
                   "AGAMEMNON_ENV": self.tools / "gaia-mcp" / "agamemnon.env",
                   "AGAMEMNON_LOCAL": self.tools / "talos" / "agents" / "agamemnon" / "agent.local.json",
                   "AGENT_LOCAL": self.tools / "talos" / "agents" / "odysseus" / "agent.local.json"}
        for name, value in patches.items():
            p = mock.patch.object(configure, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(configure, "gaia_client", return_value=(FakeServer, FakeGaia()))
        p.start()
        self.addCleanup(p.stop)
        self.a = {"agamemnon": True, "agamemnonEmail": "agent.agamemnon@projects4.me", "agamemnonPassword": "pw",
                  "gaiaUrl": "https://api.x", "appUrl": "https://app.x", "clientId": "c", "clientSecret": "s",
                  "name": "Eng", "timezoneLabel": "UTC", "hermesUrl": "https://h"}

    def accepted(self):
        return json.loads(configure.AGENT_LOCAL.read_text(encoding="utf-8"))["acceptAssignmentsFrom"]

    def test_enables_agamemnon_and_adds_its_id_keeping_existing(self):
        configure.write_json(configure.AGENT_LOCAL, {"acceptAssignmentsFrom": ["5", "9"], "handoffTo": "5"})
        configure.setup_agamemnon(self.a, "5")
        local = json.loads(configure.AGAMEMNON_LOCAL.read_text(encoding="utf-8"))
        self.assertEqual(local, {"enabled": True, "owners": ["5"]})
        self.assertEqual(self.accepted(), ["5", "9", "77"])
        env = configure.read_env(configure.AGAMEMNON_ENV)
        self.assertEqual(env["GAIA_EMAIL"], "agent.agamemnon@projects4.me")
        self.assertEqual(env["GAIA_PASSWORD"], "pw")

    def test_twice_is_idempotent(self):
        configure.write_json(configure.AGENT_LOCAL, {"acceptAssignmentsFrom": ["5"]})
        configure.setup_agamemnon(self.a, "5")
        first = configure.AGENT_LOCAL.read_text(encoding="utf-8")
        configure.setup_agamemnon(self.a, "5")
        self.assertEqual(configure.AGENT_LOCAL.read_text(encoding="utf-8"), first)
        self.assertEqual(self.accepted(), ["5", "77"])

    def test_engineer_settings_keep_agamemnons_id(self):
        configure.write_json(configure.AGENT_LOCAL, {"acceptAssignmentsFrom": ["5", "77"]})
        with mock.patch.object(configure, "AI_LOCAL", self.tools / "ai.json"), \
                mock.patch.object(configure, "TALOS_LOCAL", self.tools / "talos.json"):
            configure.write_engineer_settings(self.a, "5")
        self.assertEqual(self.accepted(), ["5", "77"])

    def test_skip_leaves_agamemnon_disabled(self):
        configure.setup_agamemnon(dict(self.a, agamemnon=False), "5")
        self.assertFalse(configure.AGAMEMNON_LOCAL.exists())
        self.assertFalse(configure.AGAMEMNON_ENV.exists())


if __name__ == "__main__":
    unittest.main()
