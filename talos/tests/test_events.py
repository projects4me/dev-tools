"""Events agents: loading agent.json, trigger matching, prompt rendering, and one run per (event, resource)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import db, policy as run_policy, registry, runs as run_log  # noqa: E402
from lib.config import TalosError  # noqa: E402
from lib.events import policy, store  # noqa: E402
from lib.events.worker import EventWorker  # noqa: E402

PROJECTS = {"A Gaia": "11", "A Prometheus": "22"}
CODES = {"A Gaia": "agaia", "A Prometheus": "aprom"}
PROMPT = "Milestone {{name}} ({{resourceId}}) in {{projectName}} ({{projectShortcode}}) is complete. {{missing}}"


def envelope(resource="m-1", project="11", event="milestone.completed", **changes):
    return {"eventName": event, "projectId": project, "actorId": "5", "resource": {"type": "milestone", "id": resource},
            "changes": changes or {"name": "Sprint 4", "status": "completed"}}


def raw_agent(**extra):
    return {"name": "retro", "kind": "events", "gaiaEnv": "agent.env", "projects": {"A Gaia": "gaia"},
            "triggers": {"milestone.completed": {"prompt": PROMPT}}, "model": {"default": "sonnet"},
            "rolePrompt": "role.md", **extra}


class LoadTest(unittest.TestCase):
    def setUp(self):
        self.local = Path(tempfile.mkdtemp())
        (self.local / "agent.env").write_text("GAIA_EMAIL=x\n", encoding="utf-8")
        self.folder = Path(tempfile.mkdtemp()) / "retro"
        self.folder.mkdir()
        (self.folder / "role.md").write_text("You write retrospectives.", encoding="utf-8")
        patcher = mock.patch.object(registry, "LOCAL_DIR", self.local)
        patcher.start()
        self.addCleanup(patcher.stop)

    def load(self, raw):
        (self.folder / "agent.json").write_text(json.dumps(raw), encoding="utf-8")
        return registry.load_agent(self.folder)

    def test_loads_without_actions_or_reviewer(self):
        agent = self.load(raw_agent(limitsByModel={"sonnet": {"maxTurns": 30}}))
        self.assertEqual(agent.kind, "events")
        self.assertEqual(agent.triggers, {"milestone.completed": PROMPT})
        self.assertEqual((agent.actions, agent.accept_from, agent.handoff_to), ({}, [], ""))
        self.assertEqual(agent.role_prompt, "You write retrospectives.")
        self.assertEqual(run_policy.run_limits(agent, "sonnet").max_turns, 30)

    def test_needs_triggers_with_prompts(self):
        raw = raw_agent()
        del raw["triggers"]
        with self.assertRaisesRegex(TalosError, "missing triggers"):
            self.load(raw)
        with self.assertRaisesRegex(TalosError, "needs a prompt"):
            self.load(raw_agent(triggers={"milestone.completed": {}}))
        with self.assertRaisesRegex(TalosError, "at least one event"):
            self.load(raw_agent(triggers={}))

    def test_needs_projects(self):
        with self.assertRaisesRegex(TalosError, "projects"):
            self.load(raw_agent(projects={}))


class PolicyTest(unittest.TestCase):
    agent = mock.Mock(triggers={"milestone.completed": PROMPT})

    def test_key_only_for_triggers_in_the_agents_projects(self):
        self.assertEqual(policy.key_for(envelope(), self.agent, PROJECTS), ("milestone.completed", "m-1", "11"))
        self.assertIsNone(policy.key_for(envelope(project="99"), self.agent, PROJECTS))
        self.assertIsNone(policy.key_for(envelope(event="milestone.updated"), self.agent, PROJECTS))
        self.assertIsNone(policy.key_for({**envelope(), "resource": {}}, self.agent, PROJECTS))
        self.assertIsNone(policy.key_for(None, self.agent, PROJECTS))

    def test_renders_project_resource_and_changes(self):
        values = policy.variables(envelope(project="22"), PROJECTS, CODES)
        self.assertEqual(policy.render(PROMPT, values),
                         "Milestone Sprint 4 (m-1) in A Prometheus (aprom) is complete. {{missing}}")

    def test_event_fields_win_over_changes(self):
        values = policy.variables(envelope(resourceId="spoofed", name=None), PROJECTS, CODES)
        self.assertEqual(policy.render("{{resourceId}}|{{name}}|{{ projectId }}", values), "m-1||11")


class FakeRunner:
    """A runner that records jobs and answers with a canned `claude -p` result."""

    def __init__(self, *results):
        self.results, self.jobs = list(results), []

    def claude_bin(self, host):
        return host

    def mcp_config(self, host):
        return host

    def runtime_guard(self, host):
        return None

    def execute(self, job):
        self.jobs.append(job)
        return self.results.pop(0), None


def done(summary="wrote it"):
    return {"subtype": "success", "session_id": "s", "total_cost_usd": 0.5,
            "structured_output": {"outcome": "done", "summary": summary}}


class WorkerTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        patcher = mock.patch.object(db, "DB_FILE", tmp / "talos.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.agent = mock.Mock(triggers={"milestone.completed": PROMPT}, model_default="sonnet", max_turns=0,
                               limits_by_model={}, reviewer_name="you", role_prompt="role", permission_mode="dontAsk",
                               builtin_tools="", allowed_tools=[], denied_tools=[])
        self.agent.name = "retro"

    def worker(self, *results):
        self.runner = FakeRunner(*results)
        return EventWorker(self.agent, "claude", {}, PROJECTS, lambda: CODES, run_on=self.runner)

    def drain(self, worker):
        while not worker._queue.empty():
            worker.handle(*worker._queue.get())

    def row(self, resource="m-1"):
        return next(r for r in store.recent() if r["resource_id"] == resource)

    def test_runs_once_per_milestone(self):
        worker = self.worker(done(), done())
        worker.on_event(envelope())
        worker.on_event(envelope())                  # re-sent on a reconnect
        worker.on_event(envelope(project="22"))      # the same milestone heard from another project
        self.drain(worker)
        self.assertEqual(len(self.runner.jobs), 1)
        self.assertIn("Milestone Sprint 4 (m-1) in A Gaia (agaia)", self.runner.jobs[0].prompt)
        self.assertEqual((self.row()["status"], self.row()["summary"]), (store.DONE, "wrote it"))
        worker.on_event(envelope(resource="m-2"))    # another milestone runs
        self.drain(worker)
        self.assertEqual(len(self.runner.jobs), 2)

    def test_runs_in_the_workspace_root_and_logs_the_run(self):
        worker = self.worker(done())
        worker.on_event(envelope())
        self.drain(worker)
        job = self.runner.jobs[0]
        self.assertEqual((job.repo, job.issue), ("", "m-1"))
        self.assertIn("--json-schema", job.cmd)
        run = run_log.recent(1)[0]
        self.assertEqual((run["status_at_start"], run["outcome"], run["cost_usd"]), (run_log.EVENT, "done", 0.5))

    def test_failure_is_kept_and_not_retried(self):
        failed = {"subtype": "success", "session_id": "s",
                  "structured_output": {"outcome": "blocked", "summary": "no access", "comment": "<p>denied</p>"}}
        worker = self.worker(failed)
        worker.on_event(envelope())
        self.drain(worker)
        worker.on_event(envelope())
        self.drain(worker)
        self.assertEqual(len(self.runner.jobs), 1)
        self.assertEqual((self.row()["status"], self.row()["outcome"], self.row()["detail"]),
                         (store.FAILED, "blocked", "<p>denied</p>"))

    def test_usage_limit_runs_it_again(self):
        limited = {"is_error": True, "subtype": "error", "result": "Claude AI usage limit reached|1792581600"}
        worker = self.worker(limited, done())
        worker.on_event(envelope())
        key, env = worker._queue.get()
        self.assertEqual(worker.handle(key, env), run_policy.LIMITED)
        self.assertEqual(self.row()["status"], store.QUEUED)
        self.assertIsNotNone(worker._pause.until)
        self.drain(worker)                           # the limited event was queued again
        self.assertEqual((len(self.runner.jobs), self.row()["status"]), (2, store.DONE))

    def test_restart_requeues_queued_and_closes_running(self):
        worker = self.worker()
        worker.on_event(envelope())
        worker.on_event(envelope(resource="m-2"))
        store.update("retro", ("milestone.completed", "m-2"), status=store.RUNNING)
        recovered = store.recover("retro", "2026-11-03T10:00:00+00:00")
        self.assertEqual([k[1] for k, _ in recovered], ["m-1"])
        self.assertEqual(recovered[0][1]["changes"]["name"], "Sprint 4")
        self.assertEqual(self.row("m-2")["status"], store.INTERRUPTED)

    def test_dry_run_claims_nothing(self):
        self.runner = FakeRunner()
        worker = EventWorker(self.agent, "claude", {}, PROJECTS, lambda: CODES, run_on=self.runner, dry_run=True)
        worker.on_event(envelope())
        self.assertTrue(worker._queue.empty())
        self.assertEqual(store.recent(), [])

    def test_dry_run_start_requeues_nothing(self):
        self.worker().on_event(envelope())           # left queued by an earlier, real Talos
        dry = EventWorker(self.agent, "claude", {}, PROJECTS, lambda: CODES, run_on=FakeRunner(), dry_run=True)
        dry.start()
        self.assertTrue(dry._queue.empty())
        self.assertEqual(self.row()["status"], store.QUEUED)

    def test_malformed_changes_and_resource_are_tolerated(self):
        env = {**envelope(), "changes": ["not", "a", "map"]}
        self.assertEqual(policy.variables(env, PROJECTS, CODES)["eventName"], "milestone.completed")
        self.assertIsNone(policy.key_for({**envelope(), "resource": "m-1"}, self.agent, PROJECTS))


if __name__ == "__main__":
    unittest.main()
