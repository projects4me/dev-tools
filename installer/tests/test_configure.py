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
                   "AGENT_LOCAL": self.tools / "talos" / "agents" / "odysseus" / "agent.local.json",
                   "GAIA_AGENTS_LOCAL": self.tools / "talos" / "agents" / "gaia-runtime" / "agent.local.json"}
        for name, value in patches.items():
            p = mock.patch.object(configure, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(configure, "gaia_client", return_value=(FakeServer, FakeGaia()))
        p.start()
        self.addCleanup(p.stop)
        self.a = {"agamemnon": True, "agamemnonEmail": "agent.agamemnon@projects4.me", "agamemnonClientId": "cid", "agamemnonClientSecret": "sec",
                  "gaiaUrl": "https://api.x", "appUrl": "https://app.x",
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
        self.assertEqual(env["GAIA_CLIENT_ID"], "cid")
        self.assertEqual(env["GAIA_CLIENT_SECRET"], "sec")
        self.assertNotIn("GAIA_PASSWORD", env)

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

    def test_engineer_settings_make_gaia_agents_hand_back_to_the_engineer(self):
        configure.write_json(configure.GAIA_AGENTS_LOCAL, {"enabled": False})
        with mock.patch.object(configure, "AI_LOCAL", self.tools / "ai.json"), \
                mock.patch.object(configure, "TALOS_LOCAL", self.tools / "talos.json"):
            configure.write_engineer_settings(self.a, "5")
        self.assertEqual({"enabled": False, "acceptAssignmentsFrom": ["5"], "handoffTo": "5", "reviewerName": "Eng"},
                         json.loads(configure.GAIA_AGENTS_LOCAL.read_text(encoding="utf-8")))

    def test_skip_leaves_agamemnon_disabled(self):
        configure.setup_agamemnon(dict(self.a, agamemnon=False), "5")
        self.assertFalse(configure.AGAMEMNON_LOCAL.exists())
        self.assertFalse(configure.AGAMEMNON_ENV.exists())


if __name__ == "__main__":
    unittest.main()


class BackupTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.env = self.dir / "gaia-mcp.env"

    def backups(self):
        return sorted(self.dir.glob("gaia-mcp.env.*.bak"))

    def test_rewrite_leaves_byte_identical_owner_only_backup(self):
        configure.write_private(self.env, "GAIA_PASSWORD=secret\n")
        configure.write_private(self.env, "GAIA_URL=x\n")
        baks = self.backups()
        self.assertEqual(len(baks), 1)
        self.assertEqual(baks[0].read_bytes(), b"GAIA_PASSWORD=secret\n")
        if sys.platform != "win32":
            self.assertEqual(baks[0].stat().st_mode & 0o777, 0o600)

    def test_unchanged_content_makes_no_backup(self):
        configure.write_private(self.env, "A=1\n")
        configure.write_private(self.env, "A=1\n")
        configure.write_json(self.dir / "c.json", {"a": 1})
        configure.write_json(self.dir / "c.json", {"a": 1})
        self.assertEqual(self.backups(), [])
        self.assertEqual(list(self.dir.glob("c.json.*.bak")), [])

    def test_prunes_to_five_newest(self):
        self.env.write_bytes(b"v0\n")
        for i in range(8):
            (self.dir / f"gaia-mcp.env.20200101-00000{i}.bak").write_text("old", encoding="utf-8")
        configure.write_private(self.env, "v1\n")
        baks = self.backups()
        self.assertEqual(len(baks), configure.BACKUPS_KEPT)
        self.assertEqual(baks[0].name, "gaia-mcp.env.20200101-000004.bak")
        self.assertEqual(baks[-1].read_bytes(), b"v0\n")
