"""Sim Gaia (idea resources) and sim Hermes (envelopes, intents, SSE). Run: python -m unittest discover -s tests"""

import base64
import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.bus import Bus, InvalidEnvelope, envelope, validate  # noqa: E402
from lib.ideas import Forbidden, Ideas, NotFound  # noqa: E402
from lib.server import serve  # noqa: E402
from lib.store import Store  # noqa: E402

OWNER, AGENT = "1", "nestor"


class BusTest(unittest.TestCase):
    def test_envelope_contract(self):
        env = envelope("idea.message.created", "user:1", "ideamessage", 5, "1", {"role": "user"})
        self.assertIs(validate(env), env)
        for bad in (dict(env, extra=1), dict(env, schemaVersion=2), dict(env, eventName="issue.created"),
                    dict(env, resource={"type": "x"}), dict(env, actorId=5), dict(env, changes=[]),
                    dict(env, occurredAt="yesterday"), dict(env, eventId="x" * 256),
                    dict(env, resource={"type": "t" * 101, "id": "1"})):
            with self.assertRaises(InvalidEnvelope):
                validate(bad)

    def test_intents_are_own_user_only_and_delivery_matches_rooms(self):
        bus = Bus()
        sub, accepted, rejected = bus.subscribe("1", [
            {"projectId": "user:1", "eventName": "idea.message.created"},
            {"projectId": "user:2", "eventName": "idea.message.created"},
            {"projectId": "user:1", "eventName": "issue.created"},
            {"projectId": "user:1", "eventName": "idea.message.created"}])
        self.assertEqual(len(accepted), 1)
        self.assertEqual([r["reason"] for r in rejected], ["not_member", "unknown_event", "duplicate"])
        self.assertEqual(bus.publish(envelope("idea.message.created", "user:1", "x", 1, "2")), 1)
        self.assertEqual(bus.publish(envelope("idea.message.created", "user:2", "x", 1, "2")), 0)
        self.assertEqual(bus.publish(envelope("idea.updated", "user:1", "x", 1, "2")), 0)
        self.assertEqual(sub.queue.get_nowait()["projectId"], "user:1")
        self.assertTrue(bus.online("1"))
        bus.unsubscribe(sub)
        self.assertFalse(bus.online("1"))


class IdeasTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "sim.db")
        self.bus = Bus()
        self.ideas = Ideas(self.store, self.bus)
        self.owner_sub = self.bus.subscribe(OWNER, [{"projectId": f"user:{OWNER}", "eventName": n}
                                                    for n in ("idea.message.created", "idea.plan.status.changed")])[0]
        self.agent_sub = self.bus.subscribe(AGENT, [{"projectId": f"user:{AGENT}", "eventName": "idea.message.created"}])[0]
        self.idea = self.ideas.create(OWNER, "Recurring issues", AGENT)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def events(self, sub):
        out = []
        while not sub.queue.empty():
            out.append(sub.queue.get_nowait())
        return out

    def test_messages_by_role_and_events_to_both_participants(self):
        self.ideas.add_message(OWNER, self.idea["id"], "user", "Weekly repeats")
        self.ideas.add_message(AGENT, self.idea["id"], "agent", "Questions first", steps=["Reading x"], cost=0.4)
        with self.assertRaises(Forbidden):
            self.ideas.add_message(OWNER, self.idea["id"], "agent", "pretending")
        with self.assertRaises(Forbidden):
            self.ideas.add_message(AGENT, self.idea["id"], "user", "pretending")
        with self.assertRaises(NotFound):
            self.ideas.get(self.idea["id"], "stranger")
        agent_events = self.events(self.agent_sub)
        self.assertEqual([(e["actorId"], e["changes"]["role"]) for e in agent_events], [(OWNER, "user"), (AGENT, "agent")])
        self.assertEqual(agent_events[0]["meta"]["ideaId"], self.idea["id"])
        data = self.ideas.get(self.idea["id"], OWNER)
        self.assertEqual(data["messages"][1]["steps"], ["Reading x"])
        self.assertEqual(data["idea"]["cost_usd"], 0.4)

    def test_plan_versions_and_who_moves_them(self):
        p1, v1 = self.ideas.add_plan(AGENT, self.idea["id"], {"summary": "s", "issues": []})
        p2, v2 = self.ideas.add_plan(AGENT, self.idea["id"], {"summary": "s2", "issues": []})
        self.assertEqual((v1, v2, self.store.plan(p1)["status"]), (1, 2, "superseded"))
        with self.assertRaises(Forbidden):
            self.ideas.add_plan(OWNER, self.idea["id"], {"summary": "s", "issues": []})
        with self.assertRaises(ValueError):
            self.ideas.set_plan_status(OWNER, p1, "approved")               # superseded
        with self.assertRaises(Forbidden):
            self.ideas.set_plan_status(OWNER, p2, "created")                # the agent's move
        with self.assertRaises(Forbidden):
            self.ideas.set_plan_status(AGENT, p2, "approved")               # the owner's move
        self.ideas.set_plan_status(OWNER, p2, "approved")
        with self.assertRaises(ValueError):
            self.ideas.set_plan_status(OWNER, p2, "approved")               # already approved
        self.ideas.set_plan_status(AGENT, p2, "creating")
        self.ideas.add_issue(AGENT, p2, "api", 5001, "agaia", "Do api", "http://x/5001")
        self.ideas.set_plan_status(AGENT, p2, "created")
        approved = [e for e in self.events(self.owner_sub) if e["eventName"] == "idea.plan.status.changed"]
        self.assertEqual([e["changes"]["status"] for e in approved], ["approved", "creating", "created"])
        self.assertEqual(self.ideas.get(self.idea["id"], OWNER)["plans"][1]["issues"][0]["number"], 5001)

    def test_screens_versions_access_and_messages(self):
        png = b"\x89PNG\r\n\x1a\n" + b"data"
        s1, v1 = self.ideas.add_screen(AGENT, self.idea["id"], "list", "List", 1440, 900, png)
        s2, v2 = self.ideas.add_screen(AGENT, self.idea["id"], "list", "List", 1440, 900, png + b"2")
        self.assertEqual((v1, v2), (1, 2))
        with self.assertRaises(Forbidden):
            self.ideas.add_screen(OWNER, self.idea["id"], "x", "X", 1, 1, png)          # the agent's move
        with self.assertRaises(ValueError):
            self.ideas.add_screen(AGENT, self.idea["id"], "x", "X", 1, 1, b"GIF89a")    # PNG only
        self.assertEqual(self.ideas.screen_png(s2, OWNER), png + b"2")
        with self.assertRaises(NotFound):
            self.ideas.screen_png(s2, "stranger")
        self.ideas.add_message(AGENT, self.idea["id"], "agent", "Here", screens=[s1, s2])
        with self.assertRaises(ValueError):
            self.ideas.add_message(AGENT, self.idea["id"], "agent", "Bad", screens=[12345])
        data = self.ideas.get(self.idea["id"], OWNER)
        self.assertEqual([(x["key"], x["version"]) for x in data["screens"]], [("list", 1), ("list", 2)])
        self.assertNotIn("png", data["screens"][0])
        self.assertEqual(data["messages"][-1]["screens"], [s1, s2])

    def test_idea_fields_by_role(self):
        self.ideas.update(self.idea["id"], OWNER, {"agent_state": "stopping"})
        self.ideas.update(self.idea["id"], AGENT, {"agent_state": "working", "status": "created"})
        with self.assertRaises(Forbidden):
            self.ideas.update(self.idea["id"], AGENT, {"title": "x"})
        with self.assertRaises(Forbidden):
            self.ideas.update(self.idea["id"], OWNER, {"agent_state": "working"})
        self.ideas.update(self.idea["id"], OWNER, {"status": "archived"})
        self.assertEqual(self.ideas.list(OWNER), [])


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "sim.db")
        self.bus = Bus()
        self.httpd = serve(Ideas(self.store, self.bus), self.bus, "127.0.0.1", 0, "secret")
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, actor=OWNER, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Sim-Actor": actor, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_resources_publish_and_sse(self):
        intents = json.dumps([{"projectId": f"user:{AGENT}", "eventName": "idea.message.created"}])
        stream = urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{self.port}/hermes/events?intents={urllib.parse.quote(intents)}",
            headers={"X-Sim-Actor": AGENT}), timeout=5)
        lines = iter(stream)
        self.assertEqual(next(lines), b"event: ready\n")
        self.assertEqual(json.loads(next(lines)[6:])["accepted"][0]["eventName"], "idea.message.created")
        status, idea = self.call("POST", "/api/v1/idea", {"title": "T", "agent": AGENT})
        self.assertEqual(status, 201)
        self.assertEqual(self.call("POST", "/api/v1/ideamessage",
                                   {"idea_id": idea["data"]["id"], "role": "user", "body": "hi"})[0], 201)
        next(lines)                                                          # blank line after `ready`
        self.assertEqual(next(lines), b"event: domain:event\n")
        self.assertEqual(json.loads(next(lines)[6:])["changes"], {"role": "user"})
        stream.close()
        self.assertTrue(self.call("GET", f"/api/v1/idea/{idea['data']['id']}")[1]["data"]["messages"])
        self.assertEqual(self.call("GET", "/api/v1/idea", actor="")[0], 403)
        png = b"\x89PNG\r\n\x1a\nxyz"
        status, made = self.call("POST", "/api/v1/ideascreen", {"idea_id": idea["data"]["id"], "key": "k", "title": "K",
                                                                "width": 1440, "height": 900,
                                                                "png_base64": base64.b64encode(png).decode()}, actor=AGENT)
        self.assertEqual(status, 201)
        self.assertEqual(self.call("POST", "/api/v1/ideascreen", {"idea_id": idea["data"]["id"], "key": "k",
                                                                  "png_base64": "not base64!"}, actor=AGENT)[0], 400)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/v1/ideascreen/{made['data']['id']}",
                                     headers={"X-Sim-Actor": OWNER})
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual((r.headers["Content-Type"], r.read()), ("image/png", png))
        env = envelope("idea.agent.progress", f"user:{OWNER}", "idea", 1, AGENT, {"phase": "started"})
        self.assertEqual(self.call("POST", "/publish", env, headers={"X-Hermes-Secret": "wrong"})[0], 401)
        self.assertEqual(self.call("POST", "/publish", env, headers={"X-Hermes-Secret": "secret"})[1]["ok"], True)

    def test_console_shows_writes_and_failures_not_routine_traffic(self):
        env = envelope("idea.agent.progress", f"user:{OWNER}", "idea", 1, AGENT, {"phase": "started"})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.call("GET", "/api/v1/idea")
            self.call("POST", "/publish", env, headers={"X-Hermes-Secret": "secret"})   # live progress, every second
            self.call("POST", "/publish", env, headers={"X-Hermes-Secret": "wrong"})
            self.call("POST", "/api/v1/idea", {"title": "T", "agent": AGENT})
            self.call("GET", "/api/v1/idea/999")
        lines = err.getvalue().splitlines()
        self.assertEqual(len(lines), 3, lines)
        self.assertIn("/publish", lines[0])
        self.assertIn("401", lines[0])
        self.assertIn("POST /api/v1/idea ", lines[1])
        self.assertIn("404", lines[2])


if __name__ == "__main__":
    unittest.main()
