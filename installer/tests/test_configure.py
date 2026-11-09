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
    def token(self):
        return "t"

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
        self.a = {"agamemnon": True, "agamemnonSignIn": "client", "agamemnonEmail": "agent.agamemnon@projects4.me", "agamemnonClientId": "cid", "agamemnonClientSecret": "sec",
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

    def test_engineer_settings_write_gaia_url_and_keep_hermes_url(self):
        talos_local = self.tools / "talos.json"
        configure.write_json(talos_local, {"hermesUrl": "https://old", "claudeBin": "c"})
        with mock.patch.object(configure, "AI_LOCAL", self.tools / "ai.json"),                 mock.patch.object(configure, "TALOS_LOCAL", talos_local):
            configure.write_engineer_settings(self.a, "5")
        self.assertEqual({"hermesUrl": "https://h", "claudeBin": "c", "gaiaUrl": "https://api.x"},
                         json.loads(talos_local.read_text(encoding="utf-8")))

    def test_engineer_settings_make_gaia_agents_hand_back_to_the_engineer(self):
        configure.write_json(configure.GAIA_AGENTS_LOCAL, {"enabled": False})
        with mock.patch.object(configure, "AI_LOCAL", self.tools / "ai.json"), \
                mock.patch.object(configure, "TALOS_LOCAL", self.tools / "talos.json"):
            configure.write_engineer_settings(self.a, "5")
        self.assertEqual({"enabled": False, "acceptAssignmentsFrom": ["5"], "handoffTo": "5", "reviewerName": "Eng"},
                         json.loads(configure.GAIA_AGENTS_LOCAL.read_text(encoding="utf-8")))

    def test_gaia_agents_step_writes_only_its_file(self):
        configure.write_json(configure.GAIA_AGENTS_LOCAL, {"enabled": False})
        configure.write_json(configure.AGENT_LOCAL, {"acceptAssignmentsFrom": ["5"]})
        env = self.tools / "gaia-mcp" / "gaia.env"
        env.parent.mkdir(parents=True, exist_ok=True)
        env.write_bytes(b"GAIA_PASSWORD=x\n")
        others = [configure.AGENT_LOCAL, env]
        before = {p: p.read_bytes() for p in others}
        answers = self.tools / "answers.json"
        answers.write_text(json.dumps({"name": "Eng", "gaiaUserId": 5}), encoding="utf-8")
        with mock.patch.object(configure, "ANSWERS", self.tools / "none.json"):
            self.assertEqual(0, configure.main(["gaia-agents", "--answers", str(answers)]))
        self.assertEqual({"enabled": False, "acceptAssignmentsFrom": ["5"], "handoffTo": "5", "reviewerName": "Eng"},
                         json.loads(configure.GAIA_AGENTS_LOCAL.read_text(encoding="utf-8")))
        self.assertEqual(before, {p: p.read_bytes() for p in others})

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


class EnvMergeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "gaia-mcp.env"
        p = mock.patch.object(configure, "GAIA_ENV", self.path)
        p.start()
        self.addCleanup(p.stop)
        self.a = {"gaiaUrl": "https://api.x", "appUrl": "https://app.x", "agentEmail": "a@x.me", "ghToken": "gh",
                  "agentSignIn": "client", "agentClientId": "cid", "agentClientSecret": "sec"}
        patches = [mock.patch.object(configure, "ODYSSEUS_ENV", Path(tmp.name) / "ody.env"),
                   mock.patch.object(configure, "TOOLS_DIR", Path(tmp.name)),
                   mock.patch.object(configure, "backup_file", return_value=None)]
        for q in patches:
            q.start()
            self.addCleanup(q.stop)

    def write(self, a, gaia=None):
        gaia = gaia or FakeGaia()
        with mock.patch.object(configure, "gaia_client", return_value=(FakeServer, gaia)):
            configure.write_credentials(a)
        return configure.read_env(self.path)

    def test_rerun_keeps_password_and_unknown_keys(self):
        self.path.write_text("GAIA_EMAIL=old@x.me\nGAIA_PASSWORD=pw\nCUSTOM=1\n", encoding="utf-8")
        env = self.write(self.a)
        self.assertEqual("pw", env["GAIA_PASSWORD"])
        self.assertEqual("1", env["CUSTOM"])
        self.assertEqual("a@x.me", env["GAIA_EMAIL"])
        self.assertEqual("sec", env["GAIA_CLIENT_SECRET"])

    def test_password_mode_writes_the_password(self):
        env = self.write(dict(self.a, agentSignIn="password", agentPassword="new"))
        self.assertEqual("new", env["GAIA_PASSWORD"])

    def test_client_mode_keeps_client_keys_of_other_mode(self):
        self.path.write_text("GAIA_PASSWORD=pw\nGAIA_CLIENT_ID=x\n", encoding="utf-8")
        env = self.write(self.a)
        self.assertEqual("pw", env["GAIA_PASSWORD"])
        self.assertEqual("cid", env["GAIA_CLIENT_ID"])

    def test_failed_sign_in_leaves_file_unchanged(self):
        self.path.write_text("GAIA_PASSWORD=pw\n", encoding="utf-8")

        class Bad(FakeGaia):
            def token(self):
                raise FakeServer.GaiaError("Client credentials are only for active agents")
        with self.assertRaises(SystemExit) as cm:
            self.write(self.a, Bad())
        self.assertIn("only for active agents", str(cm.exception))
        self.assertEqual("GAIA_PASSWORD=pw\n", self.path.read_text(encoding="utf-8"))

    def test_sign_in_mode_defaults_to_the_existing_files(self):
        a = {}
        configure.ask_sign_in(a, "agent", "Agent", {"GAIA_PASSWORD": "pw", "GAIA_CLIENT_ID": "c",
                                                    "GAIA_CLIENT_SECRET": "s"}, {})
        self.assertEqual(("password", "pw"), (a["agentSignIn"], a["agentPassword"]))
        b = {}
        configure.ask_sign_in(b, "agent", "Agent", {"GAIA_CLIENT_ID": "c", "GAIA_CLIENT_SECRET": "s"}, {})
        self.assertEqual("client", b["agentSignIn"])
