"""Agent Nestor on Gaia ideas: the Hermes listener's intents and handlers, the idea event filter, the mapping of
Gaia's idea records, and a turn, an approval and a reconcile through the gaia MCP's idea tools (in memory)."""

import base64
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import talos  # noqa: E402
from lib import hermes  # noqa: E402
from lib import runs as run_log  # noqa: E402
from lib.ideas import policy, state  # noqa: E402
from lib.ideas.approval import Approval  # noqa: E402
from lib.ideas.client import GaiaIdeasClient, to_idea  # noqa: E402
from lib.ideas.conversation import Conversation  # noqa: E402
from lib.ideas.progress import NoProgress  # noqa: E402
from lib.ideas.worker import IdeaWorker  # noqa: E402
from test_ideas import (MILESTONES, OWNER, PROJECTS, FakeKb, FakeLauncher, FakeRenderer, FakeTracker,  # noqa: E402
                        TempDb, block, issue, make_agent)

NESTOR = "u-nestor"
IDEA = "3f2a9c1e-7d4b-4e8a-9b1c-2d3e4f5a6b7c"
PROJECT_IDS = {"A Gaia": "p-gaia", "A Prometheus": "p-prom"}


def gaia_env(name, actor=OWNER, changes=None, idea=IDEA, resource_id="pl-1", project="p-gaia"):
    return {"eventName": name, "projectId": project, "actorId": actor, "changes": changes or {},
            "meta": {"ideaId": idea}, "resource": {"type": "x", "id": resource_id}}


class GaiaTools:
    """The gaia MCP's idea tools over in-memory Gaia records (GUID ids, JSON text columns), plus issue creation."""

    def __init__(self):
        self.ideas, self.messages, self.plans, self.plan_issues, self.uploads = {}, [], [], [], {}
        self.calls, self.n = [], 0

    def guid(self, prefix):
        self.n += 1
        return f"{prefix}-{self.n:04x}-guid"

    def add_idea(self, idea_id=IDEA, owner=OWNER, project="A Gaia"):
        self.ideas[idea_id] = {"id": idea_id, "title": "Recurring issues", "status": "open", "project": project,
                               "createdUser": owner}

    def say(self, body, author=OWNER, kind="message"):
        self.messages.append({"id": self.guid("m"), "ideaId": IDEA, "kind": kind, "body": body,
                              "createdUser": author})

    def get_idea(self, a):
        i = self.ideas[a["idea"]]
        return {"id": i["id"], "title": i["title"], "status": i["status"], "projectId": "p-gaia",
                "createdUser": i["createdUser"], "messages": [m for m in self.messages if m["ideaId"] == i["id"]],
                "plans": [p for p in self.plans if p["ideaId"] == i["id"]],
                "planIssues": [x for x in self.plan_issues if x["ideaId"] == i["id"]]}

    def list_ideas(self, a):
        return {"ideas": [{"id": i["id"], "title": i["title"], "status": i["status"]}
                          for i in self.ideas.values() if i["project"] == a["project"]]}

    def add_idea_message(self, a):
        row = {"id": self.guid("m"), "ideaId": a["idea"], "kind": a.get("kind") or "message", "body": a["body"],
               "createdUser": NESTOR, "ideaplanId": a.get("ideaplanId"), "costUsd": a.get("costUsd")}
        for key in ("steps", "screens"):
            if a.get(key) is not None:
                row[key] = json.dumps(a[key])
        self.messages.append(row)
        return {"id": row["id"], "ideaId": a["idea"]}

    def add_idea_plan(self, a):
        version = 1 + sum(1 for p in self.plans if p["ideaId"] == a["idea"])
        self.plans.append({"id": self.guid("pl"), "ideaId": a["idea"], "version": version,
                           "body": json.dumps(a["body"]), "status": "proposed", "error": None})
        return {"id": self.plans[-1]["id"], "version": version, "status": "proposed"}

    def update_idea_plan(self, a):
        p = next(p for p in self.plans if p["id"] == a["plan"])
        p.update({k: a[k] for k in ("status", "error") if a.get(k) is not None})
        return {"updated": a["plan"]}

    def add_idea_plan_issue(self, a):
        p = next(p for p in self.plans if p["id"] == a["plan"])
        code = {"A Gaia": "agaia"}.get(a["project"], a["project"])
        self.plan_issues.append({"id": self.guid("pi"), "ideaId": p["ideaId"], "ideaplanId": p["id"], "key": a["key"],
                                 "issueId": f"i-{a['issue_number']}", "issueNumber": a["issue_number"],
                                 "projectShortcode": code})
        return {"id": self.plan_issues[-1]["id"]}

    def upload_idea_screen(self, a):
        up = self.guid("up")
        self.uploads[up] = base64.b64decode(a["png_base64"])
        return {"uploadId": up, "key": a["key"]}


class GaiaTracker(FakeTracker):
    """The Tracker as GaiaIdeasClient and Approval use it."""

    def __init__(self, tools, agent):
        super().__init__()
        self.tools, self.agent = tools, agent

    def tool(self, name, args):
        self.tools.calls.append(name)
        return getattr(self.tools, name)(args)

    def download(self, upload_id):
        return self.tools.uploads[upload_id]


# -- the listener and the event filter --------------------------------------------------------------------------

class ListenerTest(unittest.TestCase):
    def test_intents_are_every_event_in_every_project(self):
        self.assertEqual(hermes.intents(PROJECT_IDS, policy.GAIA_EVENTS), [
            {"projectId": "p-gaia", "eventName": policy.MESSAGE}, {"projectId": "p-gaia", "eventName": policy.PLAN_STATUS},
            {"projectId": "p-prom", "eventName": policy.MESSAGE}, {"projectId": "p-prom", "eventName": policy.PLAN_STATUS}])

    def test_each_agent_gets_its_own_handlers_and_a_bad_event_is_only_logged(self):
        class Tracker:
            def project_ids(self):
                return PROJECT_IDS
        seen, ready = [], []

        def on_event(envelope):
            if envelope.get("bad"):
                raise RuntimeError("boom")
            seen.append(envelope["eventName"])
        listener = hermes.Listener(make_agent(), Tracker(), ["issue.assignee.changed"], on_event, ready.append, "h")
        self.assertEqual([i["eventName"] for i in listener.intents], ["issue.assignee.changed"] * 2)
        listener._dispatch({"bad": True})
        listener._dispatch({"eventName": "x"})
        self.assertEqual(seen, ["x"])


class GaiaPolicyTest(unittest.TestCase):
    def setUp(self):
        self.agent = make_agent()

    def job(self, envelope):
        return policy.gaia_job_for(envelope, self.agent, NESTOR, PROJECT_IDS)

    def test_jobs(self):
        self.assertEqual(self.job(gaia_env(policy.MESSAGE, changes={"kind": "message"})), ("turn", IDEA))
        self.assertEqual(self.job(gaia_env(policy.PLAN_STATUS, changes={"status": "approved"}, project="p-prom")),
                         ("approve", IDEA, "pl-1"))

    def test_ignored(self):
        self.assertIsNone(self.job(gaia_env(policy.MESSAGE, actor=NESTOR, changes={"kind": "message"})))  # its own
        self.assertIsNone(self.job(gaia_env(policy.MESSAGE, changes={"kind": "system"})))
        self.assertIsNone(self.job(gaia_env(policy.MESSAGE, actor="99", changes={"kind": "message"})))  # not allowed
        self.assertIsNone(self.job(gaia_env(policy.MESSAGE, changes={"kind": "message"}, project="p-other")))
        self.assertIsNone(self.job(gaia_env(policy.MESSAGE, changes={"kind": "message"}, idea=None)))
        self.assertIsNone(self.job(gaia_env(policy.PLAN_STATUS, changes={"status": "created"})))
        self.assertIsNone(self.job(gaia_env("idea.updated", changes={"status": "archived"})))
        self.assertIsNone(self.job("not an envelope"))


# -- mapping --------------------------------------------------------------------------------------------------

class MappingTest(unittest.TestCase):
    def test_gaia_records_map_onto_the_ideas_shape(self):
        data = {"id": IDEA, "title": "T", "status": "open", "projectId": "p-gaia", "createdUser": OWNER,
                "messages": [
                    {"id": "m-b", "kind": "message", "body": "hi", "createdUser": OWNER},
                    {"id": "m-a", "kind": "system", "body": "working", "createdUser": NESTOR},
                    {"id": "m-c", "kind": "message", "body": "reply", "createdUser": NESTOR, "ideaplanId": "pl-1",
                     "costUsd": 0.5, "steps": '["Reading x"]',
                     "screens": '[{"uploadId": "up-1", "key": "list", "title": "L", "version": 1}]'},
                    {"id": "m-d", "kind": "error", "body": "failed", "createdUser": NESTOR, "screens": "not json"}],
                "plans": [{"id": "pl-1", "version": "2", "status": "approved", "body": '{"issues": []}'}],
                "planIssues": [{"ideaplanId": "pl-1", "key": "api", "issueNumber": 5001, "projectShortcode": "agaia"},
                               {"ideaplanId": "pl-0", "key": "old", "issueNumber": 4000, "projectShortcode": "agaia"}]}
        out = to_idea(data, NESTOR)
        self.assertEqual(out["idea"], {"id": IDEA, "title": "T", "status": "open", "owner": OWNER, "agent": NESTOR,
                                       "agent_state": "idle", "projectId": "p-gaia"})
        self.assertEqual([(m["id"], m["role"]) for m in out["messages"]],
                         [("m-b", "user"), ("m-a", "system"), ("m-c", "agent"), ("m-d", "error")])
        reply = out["messages"][2]
        self.assertEqual((reply["plan_id"], reply["cost_usd"], reply["steps"]), ("pl-1", 0.5, ["Reading x"]))
        self.assertEqual(out["messages"][3]["screens"], [])
        self.assertEqual(out["screens"], [{"id": "up-1", "key": "list", "title": "L", "version": 1}])
        self.assertEqual(out["plans"], [{"id": "pl-1", "version": 2, "status": "approved", "error": None,
                                         "body": {"issues": []},
                                         "issues": [{"key": "api", "number": 5001, "project_code": "agaia"}]}])

    def test_writes_use_gaia_fields(self):
        tools = GaiaTools()
        tools.add_idea()
        client = GaiaIdeasClient(GaiaTracker(tools, make_agent()), NESTOR, "Agent Nestor")
        client.post(IDEA, "agent", "Hi", steps=["a"], plan_id="pl-9", cost=0.25, screens=[{"uploadId": "up"}])
        client.post(IDEA, "error", "No")
        client.set_state(IDEA, "queued")
        client.set_state(IDEA, "working")
        rows = [(m["kind"], m["body"]) for m in tools.messages]
        self.assertEqual(rows, [("message", "Hi"), ("error", "No"), ("system", "Agent Nestor is working on it.")])
        first = tools.messages[0]
        self.assertEqual((first["ideaplanId"], first["costUsd"], json.loads(first["screens"])),
                         ("pl-9", 0.25, [{"uploadId": "up"}]))


# -- end to end over the idea tools -------------------------------------------------------------------------------

class GaiaFlowTest(TempDb):
    def setUp(self):
        super().setUp()
        self.agent = make_agent()
        self.tools = GaiaTools()
        self.tools.add_idea()
        self.tracker = GaiaTracker(self.tools, self.agent)
        self.client = GaiaIdeasClient(self.tracker, NESTOR, "Agent Nestor")
        self.launcher = FakeLauncher()
        self.conv = Conversation(self.agent, self.client, NoProgress(), self.launcher, FakeKb(), lambda: PROJECTS,
                                 Path("map.md"), milestones=MILESTONES.get, renderer=FakeRenderer())
        self.worker = IdeaWorker(self.agent, self.client, NoProgress(), self.conv,
                                 Approval(self.agent, self.client, self.tracker), NESTOR,
                                 job_for=lambda e: policy.gaia_job_for(e, self.agent, NESTOR, PROJECT_IDS))

    def thread(self):
        return [(m["kind"], m["body"]) for m in self.tools.messages]

    def test_send_gets_a_working_notice_then_the_reply_with_a_plan_and_screens(self):
        shot = '```nestor-screen key="list" title="Grouped list"\n<section class="content">Hi</section>\n```'
        self.tools.say("Issues that repeat weekly")
        self.launcher.replies.append("Here.\n" + shot + "\n" + block(issue("ui", "A Prometheus", screens=["list"]),
                                                                     issue("api")))
        self.worker.on_event(gaia_env(policy.MESSAGE, changes={"kind": "message"}))
        self.worker.handle(self.worker._queue.get_nowait())
        self.assertEqual(self.thread(), [("message", "Issues that repeat weekly"),
                                         ("system", "Agent Nestor is working on it."), ("message", "Here.")])
        reply = self.tools.messages[-1]
        plan = self.tools.plans[0]
        self.assertEqual((reply["ideaplanId"], reply["costUsd"]), (plan["id"], 0.25))
        self.assertEqual([s["key"] for s in json.loads(reply["screens"])], ["list"])
        self.assertEqual(json.loads(plan["body"])["issues"][0]["screens"], ["list"])  # this turn's screen counts
        self.assertTrue(self.launcher.calls[0][0].startswith(f"Idea #{IDEA}: Recurring issues"))
        self.worker.on_event(gaia_env(policy.MESSAGE, actor=NESTOR, changes={"kind": "message"}))  # its own reply
        self.assertTrue(self.worker._queue.empty())

        plan["status"] = "approved"
        self.worker.on_event(gaia_env(policy.PLAN_STATUS, changes={"status": "approved"}, resource_id=plan["id"]))
        self.worker.handle(self.worker._queue.get_nowait())
        self.assertEqual(plan["status"], "created")
        self.assertEqual([(x["key"], x["issueNumber"]) for x in self.tools.plan_issues], [("ui", 5001), ("api", 5002)])
        self.assertEqual(self.tracker.attached, [(5001, "list.png", b"PNG:list")])
        self.assertEqual(self.tracker.created[0][2], OWNER)                # assigned to who created the idea
        self.assertIn("Created 2 issue(s) from plan v1: aprom-5001 (ui), agaia-5002 (api).", self.thread()[-1][1])

    def test_a_plan_written_before_the_last_message_is_kept(self):
        # the agent answers with its plan and screen, edits its knowledge map, then sums up "plan and screen above"
        shot = '```nestor-screen key="list" title="Grouped list"\n<section class="content">Hi</section>\n```'
        self.tools.say("Issues that repeat weekly")
        self.launcher.replies.append(["Looking around.", "Here.\n" + shot + "\n" + block(issue("ui", screens=["list"])),
                                      "One issue (plan and screen above)."])
        self.worker.handle(("turn", IDEA))
        reply = self.tools.messages[-1]
        # the answer, then the summary; not the narration before it, and not the blocks
        self.assertEqual(reply["body"].split(), "Here. One issue (plan and screen above).".split())
        self.assertEqual(reply["ideaplanId"], self.tools.plans[0]["id"])
        self.assertEqual([s["key"] for s in json.loads(reply["screens"])], ["list"])

    def test_a_turn_is_recorded_as_a_run_and_shows_while_running(self):
        self.tools.say("hi")
        self.launcher.replies.append("Hello.")
        seen = []
        self.conv.turn(IDEA, lambda run: seen.append(run_log.running_ideas()))
        self.assertEqual([(r["agent"], r["issue"], r["summary"]) for r in seen[0]],
                         [("nestor", IDEA, "Recurring issues")])
        self.assertEqual(run_log.running_ideas(), [])
        row = run_log.recent()[0]
        self.assertEqual((row["status_at_start"], row["outcome"], row["cost_usd"], row["model"]),
                         (run_log.IDEA, "replied", 0.25, self.agent.model_default))
        out = io.StringIO()
        run_log.start("nestor", IDEA, None, "opus", "s-9", False, run_log.IDEA, "2026-10-22T16:41:49+05:00",
                      summary="Recurring issues")
        with contextlib.redirect_stdout(out):
            talos.cmd_status(SimpleNamespace(steps=5))
            talos.cmd_runs(SimpleNamespace(limit=5, issue=None))
        self.assertIn(f'RUNNING  nestor idea {IDEA} "Recurring issues"  on opus', out.getvalue())
        self.assertIn(f"idea {IDEA[:8]}", out.getvalue())
        run_log.close_unfinished_ideas("nestor", "2026-10-22T17:00:00+05:00")      # what a restarted worker does
        self.assertEqual(run_log.running_ideas(), [])

    def test_nothing_to_answer_starts_no_run(self):
        self.worker.handle(("turn", IDEA))
        self.assertEqual((self.launcher.calls, run_log.recent(), self.thread()), ([], [], []))

    def test_a_follow_up_resumes_the_session_and_only_takes_in_the_new_message(self):
        self.tools.say("first")
        self.launcher.replies += ["one", "two"]
        self.worker.handle(("turn", IDEA))
        self.tools.say("second")
        self.worker.handle(("turn", IDEA))
        self.assertEqual(self.launcher.calls[1], ("second", "sess-1"))
        self.worker.handle(("turn", IDEA))                                  # nothing new
        self.assertEqual(len(self.launcher.calls), 2)
        self.assertEqual(self.thread()[-1], ("message", "two"))             # and no "working" notice left behind

    def test_reconcile_answers_a_turn_cut_off_and_resumes_plans(self):
        self.tools.say("anyone there?")
        self.client.post(IDEA, "system", "Agent Nestor is working on it.")      # Talos stopped mid-turn
        self.client.add_plan(IDEA, {"summary": "s", "issues": [issue("api", type="Task", priority="normal")]})
        self.tools.plans[0]["status"] = "creating"
        self.tools.add_idea("other-idea", project="A Prometheus")              # nothing waiting there
        self.worker.reconcile()
        self.assertEqual(self.worker._queue.get_nowait(), ("approve", IDEA, self.tools.plans[0]["id"]))
        self.assertEqual(self.worker._queue.get_nowait(), ("turn", IDEA))
        self.assertTrue(self.worker._queue.empty())
        self.launcher.replies.append("Yes.")
        self.worker._pending.clear()
        self.worker.handle(("approve", IDEA, self.tools.plans[0]["id"]))
        self.worker.handle(("turn", IDEA))
        self.assertEqual(self.tools.plans[0]["status"], "created")
        self.assertEqual(self.thread()[-1], ("message", "Yes."))
        self.assertEqual(state.get("nestor", IDEA)["answered_upto"], self.tools.messages[0]["id"])

    def test_ideas_of_owners_who_cant_hand_work_are_skipped(self):
        self.tools.ideas[IDEA]["createdUser"] = "99"
        self.tools.say("hi", author="99")
        self.worker.handle(("turn", IDEA))
        self.assertEqual(self.launcher.calls, [])


if __name__ == "__main__":
    unittest.main()
