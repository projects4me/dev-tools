"""Gaia-declared agents: delegated tokens (delegated_tokens.py) and discovery (gaia_agents.py) against a fake Gaia."""

import copy
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import delegated_tokens, gaia_agents, service_identity, workflow_steps  # noqa: E402
from lib import tracker as tracker_mod  # noqa: E402
from lib.config import GAIA_MCP, TalosError  # noqa: E402
from test_policy import make_agent  # noqa: E402
import talos  # noqa: E402

sys.path.insert(0, str(GAIA_MCP / "tests"))
from fake_gaia import FakeGaia  # noqa: E402

import service_client  # noqa: E402  (on the path through lib.delegated_tokens)

SPEC = {
    "specVersion": 1,
    "agent": {"id": "a-1", "userId": "u-1", "name": "Agent Hera", "purpose": "Reports", "sponsorId": None},
    "config": {"rolePrompt": "You are Hera.", "modelConfig": {"model": "claude-sonnet-5-5"}, "maxTurns": 40,
               "maxRunsPerIssue": None, "permissionMode": None},
    "toolGrants": [{"id": "g-1", "permissionRule": None, "tool": None,
                    "server": {"id": "s-1", "name": "gaia", "kind": "mcp", "location": "local", "transport": "stdio"}}],
    "skills": [{"id": "k-1", "name": "release-notes", "description": "Write release notes.", "version": "1",
                "content": "Group the changes by project."}],
}


def spec(**config):
    s = copy.deepcopy(SPEC)
    s["config"].update(config)
    return s


def template():
    return make_agent(name="gaia-runtime", display_name="gaia-runtime", gaia_env="", dispatch="workflow",
                      role_prompt="", model_default="", model_escalate="", model_by_complexity={},
                      max_turns=80, max_iterations=10, permission_mode="auto")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class TokenCacheTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.grants = []
        self.reply = lambda agent_id: {"accessToken": f"t{len(self.grants)}", "expiresIn": 3600}
        self.cache = delegated_tokens.TokenCache(self.grant, clock=self.clock)

    def grant(self, agent_id):
        self.grants.append(agent_id)
        return self.reply(agent_id)

    def test_a_token_is_kept_until_five_minutes_before_it_expires(self):
        self.assertEqual("t1", self.cache.token("a-1"))
        self.clock.now += 3600 - 301
        self.assertEqual("t1", self.cache.token("a-1"))
        self.clock.now += 2
        self.assertEqual("t2", self.cache.token("a-1"))
        self.assertEqual(["a-1", "a-1"], self.grants)

    def test_a_fresh_token_is_a_new_grant_with_its_scopes_whatever_is_cached(self):
        self.reply = lambda agent_id: {"accessToken": f"t{len(self.grants)}", "expiresIn": 3600,
                                       "scopes": [f"scope{len(self.grants)}"]}
        self.assertEqual("t1", self.cache.token("a-1"))
        self.assertEqual(["scope2"], self.cache.fresh("a-1"))
        self.assertEqual("t2", self.cache.token("a-1"))
        self.assertEqual(["a-1", "a-1"], self.grants)

    def test_each_agent_has_its_own_token(self):
        self.assertNotEqual(self.cache.token("a-1"), self.cache.token("a-2"))

    def test_a_refusal_drops_the_cached_token(self):
        self.cache.token("a-1")
        self.clock.now += 3400

        def refuse(agent_id):
            raise service_client.DelegationRefused("Gaia refused a token for agent a-1 (401: not delegable)", 401)
        self.reply = refuse
        with self.assertRaisesRegex(delegated_tokens.Refused, "not delegable"):
            self.cache.token("a-1")
        self.reply = lambda agent_id: {"accessToken": "fresh", "expiresIn": 3600}
        self.clock.now -= 3400              # a cached token would still be good now: it was dropped
        self.assertEqual("fresh", self.cache.token("a-1"))

    def test_gaia_unreachable_keeps_a_token_that_has_not_expired(self):
        self.cache.token("a-1")

        def down(agent_id):
            raise service_client.GaiaError("Cannot reach Gaia")
        self.reply = down
        self.clock.now += 3400
        self.assertEqual("t1", self.cache.token("a-1"))
        self.clock.now += 300
        with self.assertRaisesRegex(TalosError, "Cannot reach Gaia"):
            self.cache.token("a-1")

    def test_the_runtime_gets_tokens_with_its_own_client_credentials(self):
        fake = FakeGaia()
        fake.secrets["svc-1"] = ["secret-1"]
        fake.agents = {"a-1": {"name": "Agent Hera", "userId": "u-1", "spec": SPEC}}
        identity = mock.Mock(spec=service_identity.ServiceIdentity)
        g = service_client.service_gaia("https://gaia.test", "svc-1", "secret-1")
        g._http = fake.http
        identity.gaia.return_value = g
        cache = delegated_tokens.TokenCache.for_identity(identity)
        self.assertEqual("agent-a-1-1", cache.token("a-1"))
        fake.agents["a-1"]["delegable"] = False
        cache.forget("a-1")
        with self.assertRaises(delegated_tokens.Refused):
            cache.token("a-1")


class AgentTokenTest(unittest.TestCase):
    def test_the_file_is_written_owner_only_when_the_token_changes(self):
        tokens = iter(["t1", "t1", "t2"])
        cache = mock.Mock()
        cache.token.side_effect = lambda agent_id: next(tokens)
        path = Path(tempfile.mkdtemp()) / "gaia-hera" / "gaia-token" / "token"
        restricted = []
        token = delegated_tokens.AgentToken(cache, "a-1", path, restrict=restricted.append)
        self.assertEqual("t1", token())
        self.assertEqual("t1", path.read_text(encoding="utf-8"))
        token()
        self.assertEqual(1, len(restricted))
        self.assertEqual("t2", token())
        self.assertEqual("t2", path.read_text(encoding="utf-8"))
        self.assertEqual(2, len(restricted))
        self.assertFalse(path.with_name("token.tmp").exists())

    def test_a_deleted_file_is_written_again_and_a_closed_token_grants_nothing(self):
        cache = mock.Mock()
        cache.token.return_value = "t1"
        path = Path(tempfile.mkdtemp()) / "gaia-token" / "token"
        token = delegated_tokens.AgentToken(cache, "a-1", path, restrict=lambda p: None)
        token()
        path.unlink()
        token()
        self.assertEqual("t1", path.read_text(encoding="utf-8"))
        token.close()
        cache.forget.assert_called_once_with("a-1")
        with self.assertRaises(delegated_tokens.Refused):
            token()
        self.assertEqual("t1", path.read_text(encoding="utf-8"))     # a run in progress keeps the last token

    def test_a_failed_write_leaves_no_temporary_copy(self):
        path = Path(tempfile.mkdtemp()) / "gaia-token" / "token"
        with mock.patch.object(delegated_tokens.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(TalosError, "cannot write the delegated token file"):
                delegated_tokens.write_token(path, "t1", restrict=lambda p: None)
        self.assertFalse(path.with_name("token.tmp").exists())


class DelegatedTrackerTest(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "token"
        self.token = mock.Mock(return_value="agent-token", path=self.path)
        self.calls = []

    def http(self, method, path, params=None, body=None, form=None, auth=True):
        self.calls.append((method, path, params))
        return 200, {"data": [{"type": "user", "id": "u-1", "attributes": {"email": "hera@projects4.me"}}]}

    def tracker(self):
        agent = make_agent(name="gaia-agent-hera", gaia_env="", gaia_agent_id="a-1", gaia_user_id="u-1")
        with mock.patch.object(tracker_mod.gaia_mcp.Gaia, "_http", lambda g, *a, **k: self.http(*a, **k)):
            return tracker_mod.Tracker.delegated(agent, "https://gaia.test/", self.token)

    def test_calls_use_the_delegated_token_and_children_get_its_file_not_a_secret(self):
        tracker = self.tracker()
        self.assertEqual("agent-token", tracker.token())
        self.assertEqual("u-1", tracker.user_id())
        self.assertEqual({"GAIA_URL": "https://gaia.test", "GAIA_TOKEN_FILE": str(self.path),
                          "GAIA_EMAIL": "hera@projects4.me"}, tracker.env)
        self.assertEqual("(User.id : u-1)", self.calls[0][2]["query"])

    def test_a_user_the_token_cannot_read_stops_the_start(self):
        self.http = lambda *a, **k: (200, {"data": []})
        with self.assertRaisesRegex(TalosError, "not readable on its delegated token"):
            self.tracker()


class SettingsTest(unittest.TestCase):
    def test_the_spec_sets_identity_prompt_model_limits_grants_and_skills_on_the_template(self):
        agent = gaia_agents.build(template(), {"id": "a-1", "name": "Agent Hera", "userId": "u-1"}, SPEC,
                                  "gaia-agent-hera")
        self.assertEqual((agent.name, agent.display_name, agent.gaia_agent_id, agent.gaia_user_id),
                         ("gaia-agent-hera", "Agent Hera", "a-1", "u-1"))
        self.assertTrue(agent.role_prompt.startswith("You are Hera.\n\n# Skills granted in Gaia"))
        self.assertIn("## Skill: release-notes\n\nWrite release notes.\n\nGroup the changes by project.",
                      agent.role_prompt)
        self.assertEqual((agent.model_default, agent.max_turns, agent.max_iterations, agent.permission_mode),
                         ("claude-sonnet-5-5", 40, 10, "auto"))
        self.assertEqual(["Read", "Bash(dev-tools/ai/ai:*)", "mcp__gaia"], agent.allowed_tools)
        self.assertEqual(("workflow", "issues"), (agent.dispatch, agent.kind))

    def test_a_step_run_keeps_the_skills(self):
        agent = workflow_steps.with_spec(template(), SPEC)
        self.assertIn("## Skill: release-notes", agent.role_prompt)

    def test_names_are_slugs_with_the_id_when_two_collide(self):
        self.assertEqual("gaia-agent-hera", gaia_agents.agent_name("Agent Hera", "a-1", set()))
        self.assertEqual("gaia-agent-hera-a-2", gaia_agents.agent_name("Agent  HERA!", "a-2", {"gaia-agent-hera"}))


class KeepVisibleTest(unittest.TestCase):
    def agent(self):
        return make_agent(projects={"A Gaia": "gaia", "A Ares": "ares"})

    def test_invisible_projects_are_skipped_and_logged_once(self):
        with mock.patch.object(gaia_agents, "log") as log:
            agent = gaia_agents.keep_visible(self.agent(), {"A Gaia": "1"}, {"A Ares": "not visible"})
        self.assertEqual({"A Gaia": "gaia"}, agent.projects)
        log.assert_called_once()
        self.assertIn("A Ares", log.call_args[0][0])

    def test_no_visible_project_means_not_started(self):
        with self.assertRaisesRegex(TalosError, "none of its projects is visible"):
            gaia_agents.keep_visible(self.agent(), {}, {"A Gaia": "x", "A Ares": "y"})


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGaia()
        self.fake.secrets["svc-1"] = ["secret-1"]
        self.fake.agents = {"a-1": {"name": "Agent Hera", "userId": "u-1", "spec": spec()},
                            "a-2": {"name": "Agent Iris", "userId": "u-2", "spec": spec(rolePrompt="You are Iris.")}}
        self.g = service_client.service_gaia("https://gaia.test", "svc-1", "secret-1")
        self.g._http = self.fake.http
        self.started, self.stopped = [], []
        self.refuse_start = set()
        self.discovery = gaia_agents.Discovery(lambda: self.g, template(), self.start)
        patcher = mock.patch.object(gaia_agents, "log")
        self.log = patcher.start()
        self.addCleanup(patcher.stop)

    def start(self, agent):
        if agent.gaia_agent_id in self.refuse_start:
            raise delegated_tokens.Refused("Gaia refused a token")
        self.started.append(agent.name)
        token = mock.Mock()
        return gaia_agents.AgentRun(agent, token, [lambda: self.stopped.append(agent.name)])

    def test_discovery_starts_the_listed_agents(self):
        self.assertEqual((["gaia-agent-hera", "gaia-agent-iris"], []), self.discovery.sync())
        self.assertEqual({"a-1", "a-2"}, set(self.discovery.running))
        self.assertEqual("You are Iris.", self.discovery.running["a-2"].agent.role_prompt.split("\n")[0])

    def test_an_agent_gone_from_the_list_stops_and_comes_back_when_listed_again(self):
        self.discovery.sync()
        self.fake.agents["a-1"]["delegable"] = False        # disabled, or its delegation emptied
        self.assertEqual(([], ["gaia-agent-hera"]), self.discovery.sync())
        self.assertEqual(["gaia-agent-hera"], self.stopped)
        self.assertNotIn("a-1", self.discovery.running)
        self.discovery.running["a-2"].token.close.assert_not_called()
        self.fake.agents["a-1"]["delegable"] = True
        self.assertEqual((["gaia-agent-hera"], []), self.discovery.sync())

    def test_an_agent_already_running_is_updated_in_place_not_restarted(self):
        self.discovery.sync()
        agent = self.discovery.running["a-1"].agent
        self.fake.agents["a-1"]["spec"] = spec(maxTurns=12, modelConfig={"model": "claude-opus-5-5"})
        self.assertEqual(([], []), self.discovery.sync())
        self.assertIs(agent, self.discovery.running["a-1"].agent)
        self.assertEqual((12, "claude-opus-5-5"), (agent.max_turns, agent.model_default))
        self.assertEqual(2, len(self.started))

    def test_a_spec_talos_cant_run_keeps_the_last_good_one_or_does_not_start(self):
        self.discovery.sync()
        self.fake.agents["a-1"]["spec"] = spec(rolePrompt="")
        self.fake.agents["a-3"] = {"name": "Agent Nyx", "userId": "u-3", "spec": spec(permissionMode="yolo")}
        self.assertEqual(([], []), self.discovery.sync())
        self.assertTrue(self.discovery.running["a-1"].agent.role_prompt.startswith("You are Hera."))
        self.assertNotIn("a-3", self.discovery.running)

    def test_changed_delegation_scopes_drop_the_cached_token(self):
        self.fake.agents["a-1"]["delegationScopes"] = ["issue.read"]
        self.discovery.sync()
        run = self.discovery.running["a-1"]
        self.discovery.sync()
        run.token.drop.assert_not_called()          # same scopes: the cached token stays
        self.fake.agents["a-1"]["delegationScopes"] = ["issue.read", "issuestatus.read"]
        self.discovery.sync()
        run.token.drop.assert_called_once_with()
        self.discovery.running["a-2"].token.drop.assert_not_called()

    def test_a_list_gaia_cant_give_changes_nothing(self):
        self.discovery.sync()
        self.g._http = lambda *a, **k: (503, {"errors": [{"detail": "down"}]})
        with self.assertRaisesRegex(TalosError, "cannot list the Gaia agents"):
            self.discovery.sync()
        self.assertEqual({"a-1", "a-2"}, set(self.discovery.running))
        self.assertEqual([], self.stopped)

    def test_a_new_membership_restarts_the_agent_on_the_next_round(self):
        seen = {"now": ("A Gaia",)}
        started = []

        def start(agent):
            started.append(agent.name)
            return gaia_agents.AgentRun(agent, mock.Mock(), [lambda: self.stopped.append(agent.name)],
                                        seen["now"], lambda: seen["now"])
        d = gaia_agents.Discovery(lambda: self.g, template(), start)
        self.assertEqual((["gaia-agent-hera", "gaia-agent-iris"], []), d.sync())
        self.assertEqual(([], []), d.sync())
        seen["now"] = ("A Gaia", "A Ares")
        for run in d.running.values():
            run.visible = lambda: seen["now"]
        started_before = len(started)
        d.sync()
        self.assertEqual(started_before + 2, len(started))
        self.assertEqual(2, len(self.stopped))

    def test_an_agent_refused_at_start_is_tried_again_on_the_next_sync(self):
        self.refuse_start.add("a-1")
        self.assertEqual((["gaia-agent-iris"], []), self.discovery.sync())
        self.refuse_start.clear()
        self.assertEqual((["gaia-agent-hera"], []), self.discovery.sync())

    def test_a_refused_renewal_stops_the_agent_and_a_failed_one_keeps_it(self):
        self.discovery.sync()
        self.discovery.running["a-1"].token.side_effect = delegated_tokens.Refused("refused")
        self.discovery.running["a-2"].token.side_effect = TalosError("Cannot reach Gaia")
        self.discovery.renew()
        self.assertEqual(["gaia-agent-hera"], self.stopped)
        self.assertEqual({"a-2"}, set(self.discovery.running))

    def test_the_loop_syncs_renews_and_stops_every_agent_when_stopped(self):
        stop = threading.Event()
        self.discovery.renew_seconds = 0

        def renew():
            stop.set()                      # one round, then stop
        with mock.patch.object(self.discovery, "renew", renew):
            self.discovery.loop(stop)
        self.assertEqual(2, len(self.started))
        self.assertEqual(sorted(self.stopped), ["gaia-agent-hera", "gaia-agent-iris"])
        self.assertEqual({}, self.discovery.running)

    def test_shutdown_stops_every_agent_and_a_later_sync_starts_none(self):
        self.discovery.sync()
        self.discovery.shutdown()
        self.assertEqual({}, self.discovery.running)
        self.assertEqual(([], []), self.discovery.sync())
        self.assertEqual(2, len(self.started))

    def test_a_failing_stopper_does_not_keep_the_others_or_the_token_running(self):
        self.discovery.sync()
        run = self.discovery.running["a-1"]
        run.stoppers.insert(0, mock.Mock(side_effect=RuntimeError("listener stuck")))
        self.discovery.stop_all()
        self.assertIn("gaia-agent-hera", self.stopped)
        run.token.close.assert_called_once_with()


class RuntimeStartTest(unittest.TestCase):
    """talos._gaia_agents wires the runtime's run-request router and listener to the agents it discovers."""

    def test_a_discovered_agent_is_started_on_the_router_and_everything_stops_with_discovery(self):
        fake = FakeGaia()
        fake.secrets["svc-1"] = ["secret-1"]
        fake.agents = {"a-1": {"name": "Agent Hera", "userId": "u-1", "spec": spec()}}
        g = service_client.service_gaia("https://gaia.test", "svc-1", "secret-1")
        g._http = fake.http
        identity = mock.Mock(gaia=lambda: g, gaia_url="https://gaia.test")
        cfg = {"hermesUrl": "http://hermes.test"}
        patches = [mock.patch.object(talos, "load_gaia_template", return_value=template()),
                   mock.patch.object(talos.delegated_tokens, "TokenCache"),
                   mock.patch.object(talos.delegated_tokens, "AgentToken"),
                   mock.patch.object(talos.Tracker, "delegated"),
                   mock.patch.object(talos, "Dispatcher"),
                   mock.patch.object(talos, "Listener"),
                   mock.patch.object(talos, "mcp_config"),
                   mock.patch.object(talos.workflow_steps, "StepRunner"),
                   mock.patch.object(talos.runtime_requests, "Listener"),
                   mock.patch.object(talos.gaia_agents, "start_discovery"),
                   mock.patch.object(talos, "log"), mock.patch.object(gaia_agents, "log")]
        mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        agent_listener, runtime_listener = mocks[5], mocks[8]
        mocks[3].return_value.visible_projects.return_value = ({"A Gaia": "1"}, {})
        args = mock.Mock(dry_run=False)

        discovery = talos._gaia_agents(cfg, "claude", identity, args)

        runtime_listener.return_value.start.assert_called_once_with()
        self.assertEqual((["gaia-agent-hera"], []), discovery.sync())
        agent_listener.return_value.start.assert_called_once_with()
        mocks[7].return_value.start.assert_called_once_with()   # the step runner
        discovery.shutdown()
        runtime_listener.return_value.stop.assert_called_once_with()
        agent_listener.return_value.stop.assert_called_once_with()


class IsolationTest(unittest.TestCase):
    """The local agents must keep running whatever Gaia agent discovery does: a failure is only logged."""

    def setUp(self):
        self.fake = FakeGaia()
        self.fake.secrets["svc-1"] = ["secret-1"]
        self.fake.agents = {"a-1": {"name": "Agent Hera", "userId": "u-1", "spec": spec()}}
        self.g = service_client.service_gaia("https://gaia.test", "svc-1", "secret-1")
        self.g._http = self.fake.http
        self.identity = mock.Mock(gaia=lambda: self.g, gaia_url="https://gaia.test")
        self.logged = []
        for target in (talos, gaia_agents):
            p = mock.patch.object(target, "log", side_effect=lambda m, *a: self.logged.append(m))
            p.start()
            self.addCleanup(p.stop)

    def discover(self, identity="default", template_effect=None, args=None):
        identity = self.identity if identity == "default" else identity
        template_patch = mock.patch.object(talos, "load_gaia_template", **(
            {"side_effect": template_effect} if template_effect else {"return_value": template()}))
        with template_patch, mock.patch.object(talos.runtime_requests, "listener"),                 mock.patch.object(talos.delegated_tokens, "TokenCache"),                 mock.patch.object(talos.gaia_agents, "start_discovery"):
            return talos._discover_gaia_agents({"hermesUrl": "http://h"}, "claude", identity,
                                               args or mock.Mock(dry_run=False))

    def test_no_template_means_no_discovery(self):
        self.assertIsNone(self.discover(template_effect=lambda: None))

    def test_a_broken_template_is_only_logged(self):
        self.assertIsNone(self.discover(template_effect=TalosError("bad agent.local.json")))
        self.assertIsNone(self.discover(template_effect=ValueError("not json")))
        self.assertTrue(any("not discovered" in m or "bad agent.local.json" in m for m in self.logged))

    def test_no_service_identity_means_no_discovery(self):
        self.assertIsNone(self.discover(identity=None))

    def test_a_failure_wiring_discovery_is_only_logged(self):
        with mock.patch.object(talos.runtime_requests, "listener", side_effect=RuntimeError("boom")),                 mock.patch.object(talos, "load_gaia_template", return_value=template()),                 mock.patch.object(talos.delegated_tokens, "TokenCache"):
            self.assertIsNone(talos._discover_gaia_agents({"hermesUrl": "h"}, "claude", self.identity,
                                                          mock.Mock(dry_run=False)))
        self.assertTrue(any("boom" in m for m in self.logged))

    def discovery(self, start):
        return gaia_agents.Discovery(lambda: self.g, template(), start)

    def test_a_failure_listing_agents_is_raised_for_the_loop_to_log(self):
        d = self.discovery(lambda a: None)
        d.gaia = mock.Mock(side_effect=RuntimeError("down"))
        stop = threading.Event()
        clock = Clock()
        d.renew_seconds = 0
        calls = []
        d.sync = lambda: (calls.append(1), stop.set(), (_ for _ in ()).throw(RuntimeError("down")))[-1]
        d.loop(stop, clock)             # does not raise
        self.assertEqual([1], calls)
        self.assertTrue(any("down" in m for m in self.logged))

    def test_a_spec_that_cannot_be_built_is_only_logged_and_the_rest_start(self):
        self.fake.agents["a-2"] = {"name": "Agent Iris", "userId": "u-2", "spec": spec(rolePrompt="You are Iris.")}
        started = []
        d = self.discovery(lambda a: started.append(a.name) or gaia_agents.AgentRun(a, mock.Mock(), []))
        with mock.patch.object(gaia_agents, "build", side_effect=[TalosError("bad spec"), mock.DEFAULT]) as b:
            b.side_effect = [TalosError("bad spec"), gaia_agents.dataclasses.replace(template(), name="gaia-x",
                                                                                   gaia_agent_id="a-2")]
            self.assertEqual(["gaia-x"], d.sync()[0])
        self.assertTrue(any("bad spec" in m for m in self.logged))

    def test_a_start_that_raises_is_only_logged(self):
        def start(agent):
            raise TalosError("no hermes")
        d = self.discovery(start)
        self.assertEqual(([], []), d.sync())
        self.assertEqual({}, d.running)

    def test_a_lost_delegated_token_stops_only_that_agent(self):
        token = mock.Mock(side_effect=delegated_tokens.Refused("revoked"))
        stopped = []
        d = self.discovery(lambda a: gaia_agents.AgentRun(a, token, [lambda: stopped.append(a.name)]))
        d.sync()
        d.renew()
        self.assertEqual(["gaia-agent-hera"], stopped)
        self.assertEqual({}, d.running)


if __name__ == "__main__":
    unittest.main()
