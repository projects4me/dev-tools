"""Ideas agents (Agent Nestor): event policy, plans, the streamed run, a conversation turn, approval and the worker,
with an in-memory idea client (sim Gaia's contract), a fake launcher and a fake tracker."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import db  # noqa: E402
from lib.ideas import browser, page, plan, policy, screens, state, stream, styles  # noqa: E402
from lib.ideas.approval import Approval  # noqa: E402
from lib.ideas.conversation import Conversation  # noqa: E402
from lib.ideas.sanitize import SCREEN, clean_html  # noqa: E402
from lib.ideas.worker import IdeaWorker  # noqa: E402
from lib.registry import Agent  # noqa: E402
from lib.simbus import parse_sse  # noqa: E402

AGENT_ID, OWNER = "nestor-id", "1"
PROJECTS = {"A Gaia": "agaia", "A Prometheus": "aprom"}


def make_agent(**overrides):
    fields = dict(
        name="nestor", display_name="Agent Nestor", role="planning", gaia_env="x.env",
        projects={"A Gaia": "gaia", "A Prometheus": "prometheus"}, accept_from=[OWNER], handoff_to=OWNER,
        milestones=[], actions={}, statuses={}, model_default="opus", model_escalate="opus", model_by_complexity={},
        permission_mode="dontAsk", allowed_tools=["Read", "Edit(dev-tools/talos/state/nestor/map.md)"],
        max_iterations=10, role_prompt="role", reviewer_name="Hammad Hassan", max_turns=40, kind="ideas",
        events=list(policy.EVENTS), builtin_tools="Read,Grep,Glob,Bash,Edit", denied_tools=["Write", "Bash(git:*)"])
    fields.update(overrides)
    return Agent(**fields)


def issue(key, project="A Gaia", depends=(), **kw):
    return {"key": key, "project": project, "subject": f"Do {key}", "description": f"<p>{key}</p>",
            "complexity": "simple", "dependsOn": list(depends), **kw}


def block(*issues, summary="Build it"):
    return "```nestor-plan\n" + json.dumps({"summary": summary, "issues": list(issues)}) + "\n```"


def env(name, actor=OWNER, changes=None, idea=7, resource_id="1", project=f"user:{AGENT_ID}"):
    return {"eventName": name, "projectId": project, "actorId": actor, "changes": changes or {},
            "meta": {"ideaId": idea}, "resource": {"type": "x", "id": resource_id}}


class TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patch = mock.patch.object(db, "DB_FILE", self.tmp / "talos.db")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()


# -- pure parts ---------------------------------------------------------------------------------------------

class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.agent = make_agent()

    def job(self, envelope):
        return policy.job_for(envelope, self.agent, AGENT_ID)

    def test_jobs(self):
        self.assertEqual(self.job(env(policy.MESSAGE, changes={"role": "user"})), ("turn", 7))
        self.assertEqual(self.job(env(policy.PLAN_STATUS, changes={"status": "approved"}, resource_id="12")),
                         ("approve", 7, 12))
        self.assertEqual(self.job(env(policy.UPDATED, changes={"agent_state": "stopping"})), ("stop", 7))

    def test_ignored(self):
        self.assertIsNone(self.job(env(policy.MESSAGE, changes={"role": "agent"})))         # its own reply
        self.assertIsNone(self.job(env(policy.MESSAGE, actor=AGENT_ID, changes={"role": "user"})))
        self.assertIsNone(self.job(env(policy.MESSAGE, actor="99", changes={"role": "user"})))  # not allowed
        self.assertIsNone(self.job(env(policy.MESSAGE, changes={"role": "user"}, project="user:someone")))
        self.assertIsNone(self.job(env(policy.PLAN_STATUS, changes={"status": "created"})))

    def test_intents_are_user_scoped(self):
        self.assertEqual(policy.intents(self.agent, AGENT_ID)[0],
                         {"projectId": f"user:{AGENT_ID}", "eventName": policy.EVENTS[0]})

    def test_unanswered(self):
        msgs = [{"id": 1, "role": "user"}, {"id": 2, "role": "agent"}, {"id": 3, "role": "user"},
                {"id": 4, "role": "system"}, {"id": 5, "role": "user"}]
        self.assertEqual([m["id"] for m in policy.unanswered(msgs, 0)], [3, 5])     # no state: after the last reply
        self.assertEqual([m["id"] for m in policy.unanswered(msgs, 3)], [5])        # sent during a turn still counts

    def test_sse(self):
        lines = ["event: ready\n", 'data: {"accepted": []}\n', "\n", ": keepalive\n", "\n",
                 "event: domain:event\n", 'data: {"eventName": "x"}\n', "\n"]
        self.assertEqual(list(parse_sse(lines)), [("ready", '{"accepted": []}'), ("domain:event", '{"eventName": "x"}')])


class PlanTest(unittest.TestCase):
    def test_extract_takes_the_last_block_and_leaves_the_prose(self):
        text, raw = plan.extract("Here is v1\n" + block(issue("a")) + "\nand v2\n" + block(issue("b")))
        self.assertEqual(json.loads(raw)["issues"][0]["key"], "b")
        self.assertIn("Here is v1", text)
        self.assertEqual(plan.extract("no plan here"), ("no plan here", None))

    def test_parse_normalises_and_reports_every_problem(self):
        p = plan.parse(json.dumps({"summary": "s", "issues": [issue("api", project="agaia"),
                                                             issue("ui", project="A Prometheus", depends=["api", "adt-4061"])]}),
                       PROJECTS)
        self.assertEqual([(i["project"], i["projectCode"], i["type"], i["priority"]) for i in p["issues"]],
                         [("A Gaia", "agaia", "Task", "medium"), ("A Prometheus", "aprom", "Task", "medium")])
        bad = {"summary": "", "issues": [issue("Bad Key"), issue("x", project="A Nowhere", complexity="huge"),
                                         issue("y", depends=["missing"]), issue("y")]}
        with self.assertRaises(ValueError) as e:
            plan.parse(json.dumps(bad), PROJECTS)
        for part in ("summary", "key", "project", "complexity", "missing", "used twice"):
            self.assertIn(part, str(e.exception))

    def test_cycles_and_order(self):
        with self.assertRaisesRegex(ValueError, "circular"):
            plan.parse(json.dumps({"summary": "s", "issues": [issue("a", depends=["b"]), issue("b", depends=["a"])]}),
                       PROJECTS)
        issues = [issue("ui", depends=["api"]), issue("docs"), issue("api", depends=["db", "adt-1"]), issue("db")]
        self.assertEqual([i["key"] for i in plan.ordered(issues)], ["docs", "db", "api", "ui"])

    def test_descriptions_are_sanitised(self):
        evil = issue("a", description='<p onclick="x()">Hi <img src=x onerror=alert(1)><script>steal()</script>'
                                      '<a href="javascript:alert(1)">bad</a> <a href="https://ok.example">ok</a></p>')
        p = plan.parse(json.dumps({"summary": "s", "issues": [evil]}), PROJECTS)
        self.assertEqual(p["issues"][0]["description"], '<p>Hi bad <a href="https://ok.example">ok</a></p>')
        self.assertEqual(clean_html("<p>a<br/>b</p>"), "<p>a<br>b</p>")


class RegistryTest(unittest.TestCase):
    def test_an_agent_that_cant_load_doesnt_stop_the_others(self):
        from lib import registry
        tmp = Path(tempfile.mkdtemp())
        local_dir = Path(tempfile.mkdtemp())      # the dev-tools checkout: agent.local.json and the gaia env file
        (local_dir / "agent.env").write_text("GAIA_EMAIL=x\n")
        for name, local in (("aaa", None), ("odysseus", {"acceptAssignmentsFrom": ["1"], "handoffTo": "1"})):
            (tmp / name).mkdir()
            (tmp / name / "agent.json").write_text(json.dumps({
                "name": name, "gaiaEnv": "agent.env", "projects": {}, "acceptAssignmentsFrom": [], "handoffTo": "",
                "actions": {}, "model": {"default": "sonnet"}, "rolePrompt": "agent.json"}))
            if local:
                (local_dir / "talos" / "agents" / name).mkdir(parents=True)
                (local_dir / "talos" / "agents" / name / "agent.local.json").write_text(json.dumps(local))
        with mock.patch.object(registry, "AGENTS_DIR", tmp), mock.patch.object(registry, "LOCAL_DIR", local_dir):
            self.assertEqual([a.name for a in registry.load_agents()], ["odysseus"])   # aaa isn't configured
            self.assertEqual([a.name for a in registry.load_agents("odysseus")], ["odysseus"])
            with self.assertRaises(Exception):
                registry.load_agents("aaa")


class ScreensTest(unittest.TestCase):
    def test_blocks_are_parsed_and_validated(self):
        reply = ('Intro\n```nestor-screen key="a" title="A" size="1200x700" shell="none"\n<p>x</p>\n```\n'
                 '```nestor-screen key="Bad Key" title="B"\n<p>y</p>\n```\n'
                 '```nestor-screen key="c" size="9x9"\n<script>only()</script>\n```\nOutro')
        text, found, problems = screens.extract(reply)
        self.assertEqual(text, "Intro\n\n\n\nOutro")
        self.assertEqual([(f["key"], f["width"], f["height"], f["shell"], f["body"]) for f in found],
                         [("a", 1200, 700, "none", "")])                         # bad body classes dropped
        self.assertEqual(len(problems), 2)

    def test_screen_html_keeps_layout_and_drops_anything_active(self):
        html = clean_html('<div class="box box-primary" style="x" onclick="y()"><a href="https://evil/x">l</a>'
                          '<a href="#top" class="btn">ok</a><img src="https://evil/i.png"><img src="data:image/png;base64,iVBORw0K">'
                          '<input class="form-control" placeholder="Name" onfocus="z()"><script>s()</script>'
                          '<link rel="stylesheet" href="https://evil/c.css"><style>body{}</style></div>', SCREEN)
        self.assertEqual(html, '<div class="box box-primary"><a>l</a><a href="#top" class="btn">ok</a><img>'
                               '<img src="data:image/png;base64,iVBORw0K"><input class="form-control" '
                               'placeholder="Name"></div>')

    def test_page_and_command(self):
        screen = {"key": "a", "title": "A", "width": 1440, "height": 900, "shell": "app", "html": "<p>x</p>",
                  "body": "notifications-sidebar-open", "project": 'A "Gaia" <b>'}
        html_page = page.build(screen, ["http://prom/x.css?a=1&b=2"], "Hammad <Hassan>")
        self.assertIn('class="skin-black-light sidebar-mini notifications-sidebar-open"', html_page)
        self.assertIn("A &quot;Gaia&quot; &lt;b&gt;", html_page)                  # escaped once, in the page
        self.assertIn("Hammad &lt;Hassan&gt;", html_page)
        self.assertIn('href="http://prom/x.css?a=1&amp;b=2"', html_page)
        self.assertNotIn("main-header", page.build(dict(screen, shell="none"), []))
        cmd = browser.command("edge", Path("s.html"), Path("s.png"), 1440, 900, Path("prof"), as_root=False)
        self.assertIn("--window-size=1440,900", cmd)
        self.assertIn("--disable-dev-shm-usage", cmd)
        self.assertTrue(any(c.startswith("--user-data-dir=") for c in cmd))      # never the engineer's profile
        self.assertNotIn("--no-sandbox", cmd)
        self.assertIn("--no-sandbox", browser.command("chromium", Path("s.html"), Path("s.png"), 1, 1, Path("p"),
                                                      as_root=True))
        with self.assertRaises(browser.RenderError):
            browser.find("/no/such/browser")

    def test_kill_tree_ends_a_running_process_and_reaps_it(self):
        import subprocess
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                start_new_session=os.name != "nt")
        browser.kill_tree(proc)
        self.assertIsNotNone(proc.poll())

    def test_a_browser_that_writes_no_image_is_an_error(self):
        class Proc:
            pid, returncode = 1, 0

            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0
        with tempfile.TemporaryDirectory() as tmp, mock.patch("subprocess.Popen", return_value=Proc()), \
                mock.patch.object(browser, "kill_tree"):
            with self.assertRaisesRegex(browser.RenderError, "no image"):
                browser.screenshot("edge", Path(tmp) / "s.html", Path(tmp) / "s.png", 800, 600, tmp)

    def test_stylesheets_prefer_the_built_css_same_origin_only(self):
        built = styles.stylesheets(["http://prom/assets/prometheus.css"])
        self.assertIn("http://prom/assets/prometheus.css", built)
        self.assertFalse([c for c in built if c.endswith("projects4me.css")])   # the overlay is only the fallback
        self.assertTrue([c for c in styles.stylesheets([]) if c.endswith("projects4me.css")])
        index = ('<link integrity="" rel="stylesheet" href="/assets/vendor-1.css">'
                 '<link rel="stylesheet" href="/assets/prometheus-2.css"><link rel="icon" href="/favicon.css">'
                 '<link rel="stylesheet" href="https://elsewhere.example/x.css">')
        with mock.patch("urllib.request.urlopen", mock.mock_open(read_data=index.encode())):
            self.assertEqual(styles.built_css("http://prom"), ["http://prom/assets/vendor-1.css",
                                                              "http://prom/assets/prometheus-2.css"])

    def test_stylesheets_are_looked_up_again_after_a_while(self):
        calls = []
        sheets = styles.Stylesheets("http://prom", fetch=lambda url: calls.append(url) or [])
        sheets.current()
        sheets.current()
        self.assertEqual(len(calls), 1)
        sheets._at -= styles.REFRESH_SECONDS + 1
        sheets.current()
        self.assertEqual(len(calls), 2)

    def test_sanitiser_edge_cases(self):
        out = clean_html('<a href="JaVaScRiPt:alert(1)">a</a><a href="#ok">b</a><img src="DATA:image/png;base64,AAAA">'
                         '<frame src="x"><p data-x="1" aria-label="l">kept</p><title>t</title><textarea>t&lt;</textarea>'
                         '<p bad"name="1">p</p>', SCREEN)
        self.assertEqual(out, '<a>a</a><a href="#ok">b</a><img><p data-x="1" aria-label="l">kept</p>'
                              '<textarea>t&lt;</textarea><p>p</p>')


class StreamTest(unittest.TestCase):
    def test_events(self):
        delta = {"type": "stream_event", "event": {"type": "content_block_delta",
                                                     "delta": {"type": "text_delta", "text": "Hi"}}}
        self.assertEqual(stream.events(delta), [{"type": "text", "text": "Hi"}])
        self.assertEqual(stream.events(dict(delta, parent_tool_use_id="t")), [])
        tool = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "dev-tools/ai/ai kb search  recurring"}}]}}
        self.assertEqual(stream.events(tool), [{"type": "step", "text": "Searching issues: recurring"}])
        self.assertTrue(stream.events({"type": "result", "subtype": "error_max_turns"})[0]["is_error"])

    def test_command_comes_from_the_agent(self):
        agent = make_agent()
        cmd = stream.build_command("claude", agent, "opus", "sys", mcp=Path("m.json"), session_id="s1")
        self.assertEqual(cmd[2:4], ["--model", "opus"])                     # the prompt goes on stdin
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Read,Grep,Glob,Bash,Edit")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "dontAsk")
        self.assertIn("Bash(git:*)", cmd[cmd.index("--disallowedTools"):])
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(cmd[cmd.index("--resume") + 1], "s1")


# -- with fakes ---------------------------------------------------------------------------------------------

class FakeClient:
    """Sim Gaia's idea resources in memory, as the agent sees them."""

    def __init__(self):
        self.ideas_, self.messages, self.plans, self.issues, self.screens = {}, [], [], [], []
        self.states = []

    def add_idea(self, idea_id=7, title="Recurring issues"):
        self.ideas_[idea_id] = {"id": idea_id, "title": title, "status": "open", "owner": OWNER, "agent": AGENT_ID,
                                "agent_state": "idle"}

    def say(self, idea_id, body):
        self.messages.append({"id": len(self.messages) + 1, "idea_id": idea_id, "role": "user", "body": body})

    def ideas(self):
        return list(self.ideas_.values())

    def idea(self, idea_id):
        plans = [dict(p, issues=[i for i in self.issues if i["plan_id"] == p["id"]])
                 for p in self.plans if p["idea_id"] == idea_id]
        return {"idea": self.ideas_[idea_id], "messages": [m for m in self.messages if m["idea_id"] == idea_id],
                "plans": plans, "screens": [dict(x, png=None) for x in self.screens if x["idea_id"] == idea_id]}

    def set_state(self, idea_id, s):
        self.states.append(s)
        self.ideas_[idea_id]["agent_state"] = s

    def mark_created(self, idea_id):
        self.ideas_[idea_id]["status"] = "created"

    def post(self, idea_id, role, body, steps=None, plan_id=None, cost=None, screens=None):
        self.messages.append({"id": len(self.messages) + 1, "idea_id": idea_id, "role": role, "body": body,
                              "steps": steps, "plan_id": plan_id, "cost_usd": cost, "screens": screens or []})

    def add_screen(self, idea_id, screen, png):
        version = 1 + sum(1 for x in self.screens if x["idea_id"] == idea_id and x["key"] == screen["key"])
        self.screens.append({"id": 900 + len(self.screens), "idea_id": idea_id, "key": screen["key"],
                             "title": screen["title"], "version": version, "png_bytes": png})
        return self.screens[-1]["id"], version

    def screen_png(self, screen_id):
        return next(x["png_bytes"] for x in self.screens if x["id"] == screen_id)

    def add_plan(self, idea_id, body):
        for p in self.plans:
            if p["idea_id"] == idea_id and p["status"] in ("proposed", "failed"):
                p["status"] = "superseded"
        version = 1 + sum(1 for p in self.plans if p["idea_id"] == idea_id)
        self.plans.append({"id": 100 + len(self.plans), "idea_id": idea_id, "version": version, "body": body,
                           "status": "proposed"})
        return self.plans[-1]["id"], version

    def set_plan_status(self, plan_id, status, error=None):
        p = next(p for p in self.plans if p["id"] == plan_id)
        p["status"], p["error"] = status, error

    def add_issue(self, plan_id, key, made):
        p = next(p for p in self.plans if p["id"] == plan_id)
        self.issues.append({"plan_id": plan_id, "key": key, "number": made["issueNumber"],
                            "project_code": made["projectShortcode"]})

class FakeProgress:
    def __init__(self):
        self.events = []

    def publish(self, idea, payload):
        self.events.append(payload)


class FakeRun:
    def __init__(self, events):
        self._events, self.stopped = events, False

    def __iter__(self):
        return iter(self._events)

    def stop(self):
        self.stopped = True


class FakeLauncher:
    """Each run costs 0.25; like Claude, a resumed session reports its cumulative cost."""

    def __init__(self):
        self.replies, self.calls, self.session_cost = [], [], {}

    def start(self, prompt, model, system_prompt, session_id=None, new_session_id=None, label=""):
        self.calls.append((prompt, session_id))
        text = self.replies.pop(0)
        session = session_id or "sess-1"
        self.session_cost[session] = self.session_cost.get(session, 0) + 0.25
        return FakeRun([{"type": "step", "text": "Reading x"}, {"type": "text", "text": text},
                        {"type": "result", "text": text, "session_id": session, "cost": self.session_cost[session],
                         "is_error": False, "subtype": "success"}])


class FakeKb:
    def ensure_fresh(self, step):
        pass


class FakeTracker:
    def __init__(self, fail_on=None):
        self.created, self.fail_on, self.next, self.milestones, self.attached = [], fail_on, 5000, [], []

    def create_issue(self, project, subject, description, type_, priority, complexity, assignee, milestone):
        if self.fail_on and self.fail_on in subject:
            raise RuntimeError("Gaia said no")
        self.next += 1
        self.created.append((subject, description, assignee))
        self.milestones.append(milestone)
        return {"issueNumber": self.next, "projectShortcode": {"A Gaia": "agaia"}.get(project, "aprom"),
                "subject": subject, "url": f"http://prom/{self.next}"}

    def attach_png(self, number, name, png):
        if getattr(self, "attach_fails", False):
            raise RuntimeError("upload refused")
        self.attached.append((number, name, png))


class FakeRenderer:
    def __init__(self, fail=False):
        self.rendered, self.fail = [], fail

    def render(self, screen):
        if self.fail:
            raise RuntimeError("no browser")
        self.rendered.append(screen["key"])
        return b"PNG:" + screen["key"].encode()


MILESTONES = {"A Gaia": ["Claude-v1", "Agents"], "A Prometheus": ["Claude-v1", "v0.6.2"]}


class IdeasFlowTest(TempDb):
    def setUp(self):
        super().setUp()
        self.agent = make_agent()
        self.client, self.launcher, self.tracker = FakeClient(), FakeLauncher(), FakeTracker()
        self.progress = FakeProgress()
        self.client.add_idea()
        self.renderer = FakeRenderer()
        self.conv = Conversation(self.agent, self.client, self.progress, self.launcher, FakeKb(), lambda: PROJECTS,
                                 Path("map.md"), milestones=MILESTONES.get, renderer=self.renderer)
        self.worker = IdeaWorker(self.agent, self.client, self.progress, self.conv,
                                 Approval(self.agent, self.client, self.tracker), AGENT_ID)

    def turn(self, text, *replies):
        self.client.say(7, text)
        self.launcher.replies += list(replies)
        self.worker.handle(("turn", 7))

    def replies(self):
        return [m for m in self.client.messages if m["role"] != "user"]

    def test_a_conversation_resumes_and_posts_plans(self):
        self.turn("Issues that repeat weekly", "Questions first.")
        self.turn("Weekly only", "Plan:\n" + block(issue("api"), issue("ui", "A Prometheus", ["api"])))
        (first, s1), (second, s2) = self.launcher.calls
        self.assertTrue(first.startswith("Idea #7: Recurring issues"))
        self.assertEqual((s1, second, s2), (None, "Weekly only", "sess-1"))
        self.assertEqual([(m["role"], m["body"], m["cost_usd"]) for m in self.replies()],
                         [("agent", "Questions first.", 0.25), ("agent", "Plan:", 0.25)])   # per run, not cumulative
        self.assertEqual(self.replies()[1]["plan_id"], self.client.plans[0]["id"])
        self.assertEqual(self.client.states, ["working", "idle", "working", "idle"])
        self.assertIn({"phase": "started"}, self.progress.events)
        self.assertIn({"phase": "done"}, self.progress.events)

    def test_nothing_new_means_no_run(self):
        self.turn("hi", "hello")
        self.worker.handle(("turn", 7))                       # the same event twice, or a reconcile
        self.assertEqual(len(self.launcher.calls), 1)

    def test_an_invalid_plan_gets_one_correction_round(self):
        self.turn("go", "My answer\n" + block(issue("a", depends=["nope"])), block(issue("a")))
        self.assertIn("Reply with only the corrected", self.launcher.calls[1][0])
        self.assertEqual(len(self.client.plans), 1)
        reply = self.replies()[-1]
        self.assertEqual((reply["body"], reply["plan_id"], reply["cost_usd"]), ("My answer", self.client.plans[0]["id"], 0.5))

    def test_approval_creates_in_order_and_tells_the_agent(self):
        self.turn("go", block(issue("ui", "A Prometheus", ["api", "adt-4061"]), issue("api")))
        plan_id = self.client.plans[0]["id"]
        self.client.set_plan_status(plan_id, "approved")
        self.worker.handle(("approve", 7, plan_id))
        self.assertEqual([s for s, _, _ in self.tracker.created], ["Do api", "Do ui"])
        self.assertEqual(self.tracker.created[0][2], OWNER)                       # assigned to the idea's owner
        self.assertIn("Depends on:</strong> agaia-5001, adt-4061</p>", self.tracker.created[1][1])
        self.assertEqual((self.client.plans[0]["status"], self.client.ideas_[7]["status"]), ("created", "created"))
        self.worker.handle(("approve", 7, plan_id))                               # never twice
        self.assertEqual(len(self.tracker.created), 2)
        self.turn("thanks", "ok")
        self.assertIn("agaia-5001 (api), aprom-5002 (ui)", self.launcher.calls[-1][0])
        self.assertFalse(state.get("nestor", 7)["note"])                          # delivered once

    def test_a_failed_approval_resumes_where_it_stopped(self):
        self.tracker.fail_on = "ui"
        self.turn("go", block(issue("api"), issue("ui", depends=["api"]), issue("docs")))
        plan_id = self.client.plans[0]["id"]
        self.client.set_plan_status(plan_id, "approved")
        self.worker.handle(("approve", 7, plan_id))
        self.assertEqual(self.client.plans[0]["status"], "failed")
        self.assertIn("Created 1 of 3", self.client.messages[-1]["body"])
        self.tracker.fail_on = None
        self.client.set_plan_status(plan_id, "approved")
        self.worker.handle(("approve", 7, plan_id))
        self.assertEqual([s for s, _, _ in self.tracker.created], ["Do api", "Do ui", "Do docs"])

    def test_stop_before_the_run_starts(self):
        self.client.say(7, "hi")
        self.worker._pending.add(("turn", 7))
        self.worker.stop(7)
        self.worker.handle(("turn", 7))
        self.assertEqual(self.launcher.calls, [])
        self.assertEqual(self.client.messages[-1]["body"], "Stopped.")

    def test_reconcile_queues_waiting_work_and_cleans_up(self):
        self.client.say(7, "anyone there?")
        self.client.add_plan(7, {"summary": "s", "issues": []})
        self.client.plans[0]["status"] = "creating"                               # Talos died mid-approval
        self.worker.reconcile()
        self.assertEqual(self.client.plans[0]["status"], "failed")
        self.assertEqual(self.worker._queue.get_nowait(), ("turn", 7))

    def test_a_message_sent_during_a_turn_gets_its_own_turn(self):
        self.client.say(7, "first")
        conv = self.conv

        class WhileRunning:
            def ensure_fresh(inner, step):
                self.client.say(7, "and also this")              # arrives while the turn runs
                self.worker.submit(("turn", 7), "event")
        conv.kb = WhileRunning()
        self.launcher.replies += ["one", "two"]
        self.worker._pending.add(("turn", 7))
        self.worker.handle(("turn", 7))
        self.assertIn(7, self.worker._again)
        conv.kb = FakeKb()
        self.worker._pending.discard(("turn", 7))
        self.worker._current = None
        self.worker._again.discard(7)
        self.worker.submit(("turn", 7), "message sent during the last turn")
        self.worker.handle(self.worker._queue.get_nowait())
        self.assertEqual(self.launcher.calls[-1][0], "and also this")

    def test_a_turn_cut_off_is_taken_up_again(self):
        self.client.say(7, "hi")
        self.client.post = mock.Mock(side_effect=RuntimeError("sim Gaia down"))
        self.launcher.replies.append("hello")
        with self.assertRaises(RuntimeError):
            self.worker.handle(("turn", 7))
        self.assertFalse(state.get("nestor", 7).get("answered_upto"))       # not counted as answered
        del self.client.post
        self.assertTrue(self.conv.has_unanswered(7))

    def test_stop_with_nothing_running_resets_the_state(self):
        self.client.ideas_[7]["agent_state"] = "stopping"
        self.worker.stop(7)
        self.assertEqual(self.client.ideas_[7]["agent_state"], "idle")

    def test_ideas_of_other_owners_are_skipped(self):
        self.client.ideas_[7]["owner"] = "stranger"
        self.client.say(7, "hi")
        self.worker.handle(("turn", 7))
        self.assertEqual(self.launcher.calls, [])

    def test_milestone_defaults_to_claude_v1_and_new_ones_are_marked(self):
        self.turn("go", block(issue("a"), issue("b", milestone="agents"), issue("c", milestone="Sprint 9")))
        body = self.client.plans[0]["body"]
        self.assertEqual([(i["milestone"], i["newMilestone"]) for i in body["issues"]],
                         [("Claude-v1", False), ("Agents", False), ("Sprint 9", True)])   # exact spelling kept
        self.assertEqual(len(self.launcher.calls), 1)                          # not an error: no correction run
        plan_id = self.client.plans[0]["id"]
        self.client.set_plan_status(plan_id, "approved")
        self.worker.handle(("approve", 7, plan_id))
        self.assertEqual(self.tracker.milestones, ["Claude-v1", "Agents", "Sprint 9"])   # created by gaia-mcp

    def test_screens_are_rendered_stored_and_attached(self):
        shot = ('```nestor-screen key="list" title="Grouped list"\n<section class="content">'
                '<div class="box box-primary" onclick="x()">Hi</div></section>\n```')
        self.turn("show me", "Here it is.\n" + shot + "\n" + block(issue("ui", "A Prometheus", screens=["list"]),
                                                                    issue("api")))
        self.assertEqual(self.renderer.rendered, ["list"])
        reply = self.replies()[-1]
        self.assertEqual((reply["body"], reply["screens"]), ("Here it is.", [900]))
        self.assertEqual(self.client.plans[0]["body"]["issues"][0]["screens"], ["list"])
        self.turn("tweak it", shot)                                              # same key: the next version
        self.assertEqual([x["version"] for x in self.client.screens], [1, 2])
        plan_id = self.client.plans[0]["id"]
        self.client.set_plan_status(plan_id, "approved")
        self.worker.handle(("approve", 7, plan_id))
        number = next(i["number"] for i in self.client.issues if i["key"] == "ui")
        self.assertEqual(self.tracker.attached, [(number, "list.png", b"PNG:list")])     # the latest version
        self.assertIn("<strong>Screens:</strong> list.png</p>", self.tracker.created[0][1])

    def test_an_attachment_that_fails_is_reported(self):
        shot = '```nestor-screen key="list" title="List"\n<p>x</p>\n```'
        self.turn("show me", "Here.\n" + shot + "\n" + block(issue("ui", screens=["list"])))
        self.tracker.attach_fails = True
        plan_id = self.client.plans[0]["id"]
        self.client.set_plan_status(plan_id, "approved")
        self.worker.handle(("approve", 7, plan_id))
        self.assertEqual(self.client.plans[0]["status"], "created")              # the issue itself is fine
        self.assertIn("could not be attached", self.client.messages[-1]["body"])
        self.assertNotIn("(attached)", self.tracker.created[0][1])

    def test_a_failed_render_doesnt_cost_a_correction_round(self):
        self.conv.renderer = FakeRenderer(fail=True)
        shot = '```nestor-screen key="list" title="List"\n<p>x</p>\n```'
        self.turn("show me", "Here.\n" + shot + "\n" + block(issue("ui", screens=["list"])))
        self.assertEqual(len(self.launcher.calls), 1)                            # no correction run
        self.assertEqual(self.client.plans[0]["body"]["issues"][0]["screens"], [])
        self.assertIn("won't get list attached", self.replies()[-1]["body"])

    def test_unreadable_milestones_are_not_checked(self):
        def broken(project):
            raise RuntimeError("Gaia down")
        self.conv.milestones = broken
        self.turn("go", block(issue("a", milestone="Whatever")))
        self.assertEqual(self.client.plans[0]["body"]["issues"][0]["milestone"], "Whatever")

    def test_a_screen_that_cant_render_is_a_note_not_a_failure(self):
        self.conv.renderer = FakeRenderer(fail=True)
        self.turn("show me", 'Look.\n```nestor-screen key="x" title="X"\n<p>hi</p>\n```')
        reply = self.replies()[-1]
        self.assertIn("could not be rendered: no browser", reply["body"])
        self.assertEqual(reply["screens"], [])

    def test_a_plan_may_only_list_screens_the_idea_has(self):
        self.turn("go", block(issue("ui", screens=["nope"])), block(issue("ui", screens=["nope"])))
        self.assertIn("no screen with key 'nope'", self.launcher.calls[-1][0])

    def test_a_lost_session_replays_the_conversation(self):
        self.turn("first", "one")
        state.update("nestor", 7, session_id="")                                  # e.g. imported idea
        self.turn("second", "two")
        prompt = self.launcher.calls[-1][0]
        self.assertIn("Engineer: first", prompt)
        self.assertIn("You: one", prompt)
        self.assertTrue(prompt.rstrip().endswith("second"))


if __name__ == "__main__":
    unittest.main()
