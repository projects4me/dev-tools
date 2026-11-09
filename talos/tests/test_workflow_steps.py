"""Workflow 'Run agent' steps: the event and intent, the agent spec applied, and a step claimed, run and reported."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.config import TalosError  # noqa: E402
from lib import db, dispatcher as dispatcher_mod, hermes, policy, sessions, workflow_steps  # noqa: E402
from test_dispatcher import StubTracker, result  # noqa: E402
from test_policy import AGENT_ID, make_agent, make_issue  # noqa: E402

SPEC = {"specVersion": 1, "agent": {"id": "a-1", "userId": AGENT_ID, "name": "Agent Odysseus"},
        "config": {"rolePrompt": "spec role", "modelConfig": {}, "maxTurns": 40, "maxRunsPerIssue": 3,
                   "permissionMode": "acceptEdits"},
        "toolGrants": [
            {"id": "g1", "permissionRule": None, "server": {"name": "gaia", "kind": "mcp"}, "tool": None},
            {"id": "g2", "permissionRule": None, "server": {"name": "ares", "kind": "mcp"},
             "tool": {"id": "t1", "name": "ask"}},
            {"id": "g3", "permissionRule": "Bash(git status:*)", "server": {"name": "claude", "kind": "builtin"},
             "tool": {"id": "t2", "name": "Bash"}},
            {"id": "g4", "permissionRule": None, "server": {"name": "claude", "kind": "builtin"},
             "tool": {"id": "t3", "name": "Read"}},
            {"id": "g5", "permissionRule": None, "server": {"name": "billing", "kind": "openapi"}, "tool": None}],
        "skills": []}


def step(**overrides):
    s = {"id": "s-1", "status": "pending", "actorUserId": AGENT_ID, "agentId": "a-1", "entityType": "issue",
         "entityId": "i", "prompt": "Work {not a placeholder} #42", "session": "new", "model": None,
         "maxTurns": None}
    s.update(overrides)
    return s


class StepTracker(StubTracker):
    """StubTracker plus the step calls, recording claims and reports."""

    def __init__(self, issue, steps=None, spec=SPEC):
        super().__init__(issue)
        self.steps = {s["id"]: s for s in steps or [step()]}
        self.spec = spec
        self.claims, self.reports = [], []
        self.refuse_claim = False

    def issue_by_id(self, issue_id):
        return dict(self._issue)

    def actor_step(self, sid):
        return dict(self.steps[sid])

    def actor_steps(self, user_id, status):
        return [s for s in self.steps.values() if s["actorUserId"] == user_id and s["status"] == status]

    def claim_step(self, sid):
        if self.refuse_claim:
            raise TalosError("Actor step s-1 is not pending.")
        self.claims.append(sid)

    def report_step(self, sid, outcome, comment):
        self.reports.append((sid, outcome, comment))

    def milestone(self, milestone_id):
        if milestone_id == "gone":
            raise TalosError(f"no milestone {milestone_id}")
        return {"name": "v1", "id": milestone_id, "project": "A Gaia", "status": "completed"}

    def agent_spec(self, agent_id):
        if self.spec is None:
            raise TalosError("agentspec returned 404")
        return self.spec


class EventTest(unittest.TestCase):
    def test_a_run_request_names_its_step(self):
        env = {"eventName": "workflow.agent.run.requested", "meta": {"stepId": "s-1", "workflowId": "w"}}
        self.assertEqual(workflow_steps.step_id(env), "s-1")

    def test_other_events_name_no_step(self):
        self.assertIsNone(workflow_steps.step_id({"eventName": "issue.status.changed", "meta": {"stepId": "s"}}))
        self.assertIsNone(workflow_steps.step_id({"eventName": "workflow.agent.run.requested", "meta": {}}))
        self.assertIsNone(workflow_steps.step_id(None))

    def test_the_intent_is_scoped_to_the_agents_user(self):
        self.assertEqual(workflow_steps.intents("u-7"),
                         [{"projectId": "user:u-7", "eventName": "workflow.agent.run.requested"}])

    def test_the_listener_subscribes_project_events_and_the_user_scope(self):
        class Projects:
            def project_ids(self):
                return {"A Gaia": "p-1"}
        listener = hermes.Listener(make_agent(), Projects(), ["issue.status.changed"], None, None, "http://h",
                                   extra_intents=workflow_steps.intents("u-7"))
        self.assertEqual(listener.intents, [{"projectId": "p-1", "eventName": "issue.status.changed"},
                                            {"projectId": "user:u-7", "eventName": "workflow.agent.run.requested"}])


class SpecTest(unittest.TestCase):
    def test_grants_become_claude_code_tool_names(self):
        self.assertEqual(workflow_steps.granted_tools(SPEC),
                         ["mcp__gaia", "mcp__ares__ask", "Bash(git status:*)", "Read"])

    def test_the_spec_overrides_the_agent_where_it_says_something(self):
        agent = workflow_steps.with_spec(make_agent(max_turns=80), SPEC)
        self.assertEqual((agent.role_prompt, agent.max_turns, agent.max_iterations, agent.permission_mode),
                         ("spec role", 40, 3, "acceptEdits"))
        self.assertEqual(agent.allowed_tools, ["Read", "Bash(dev-tools/ai/ai:*)", "mcp__gaia", "mcp__ares__ask",
                                               "Bash(git status:*)"])

    def test_an_empty_spec_config_keeps_agent_json(self):
        base = make_agent(max_turns=80)
        agent = workflow_steps.with_spec(base, {"config": {"rolePrompt": None, "maxTurns": None,
                                                           "maxRunsPerIssue": None, "permissionMode": None}})
        self.assertEqual((agent.role_prompt, agent.max_turns, agent.max_iterations, agent.permission_mode,
                          agent.allowed_tools),
                         (base.role_prompt, 80, 10, "auto", base.allowed_tools))


class ReportTest(unittest.TestCase):
    def run_(self, **fields):
        return workflow_steps.StepRun("s-1", make_agent(), **fields)

    def test_a_recorded_hand_off_is_reported(self):
        self.assertEqual(workflow_steps.report("needs_input", self.run_(outcome="needs_input", comment="<p>q</p>")),
                         ("needs_input", "<p>q</p>"))

    def test_done_and_a_pr_waiting_for_coderabbit_are_done(self):
        self.assertEqual(workflow_steps.report("done", self.run_(summary="a & b")), ("done", "<p>a &amp; b</p>"))
        self.assertEqual(workflow_steps.report(policy.AWAITING_BOT_REVIEW, self.run_())[0], "done")

    def test_anything_else_is_failed(self):
        self.assertEqual(workflow_steps.report("dry_run", self.run_())[0], "failed")


class FakeDispatcher:
    def __init__(self, outcomes, busy=0, stopping=False):
        self.outcomes = list(outcomes)
        self.stopping = stopping
        self.busy = busy            # reserve() refusals before it succeeds
        self.runs, self.records, self.released, self.events, self.waits, self.resyncs = [], [], [], [], 0, 0

    def reserve(self, number):
        if self.busy:
            self.busy -= 1
            return False
        return True

    def release(self, number):
        self.released.append(number)

    def run_step(self, issue, repo, prompt, resume, model, run):
        self.runs.append((issue["number"], repo, prompt, resume, model, run))
        return self.outcomes.pop(0)

    def run_record(self, record, prompt, resume, model, run):
        self.records.append((record, prompt, resume, model, run))
        return self.outcomes.pop(0)

    def wait_for_usage(self):
        self.waits += 1
        return not self.stopping

    def on_event(self, envelope):
        self.events.append(envelope)

    def resync(self):
        self.resyncs += 1


class StepRunnerTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(workflow_steps, "log", lambda *a, **k: None)
        patch.start()
        self.addCleanup(patch.stop)

    def runner(self, outcomes=("done",), tracker=None, busy=0, dry_run=False):
        self.tracker = tracker or StepTracker(make_issue())
        self.dispatcher = FakeDispatcher(outcomes, busy)
        return workflow_steps.StepRunner(make_agent(), self.tracker, self.dispatcher, dry_run=dry_run)

    def test_a_step_is_claimed_run_with_its_settings_and_reported(self):
        tracker = StepTracker(make_issue(), [step(session="resume", model="opus", maxTurns="25")])
        self.assertEqual(self.runner(tracker=tracker).handle("s-1"), "done")
        self.assertEqual(tracker.claims, ["s-1"])
        number, repo, prompt, resume, model, run = self.dispatcher.runs[0]
        self.assertEqual((number, repo, prompt, resume, model), (42, "gaia", "Work {not a placeholder} #42",
                                                                 True, "opus"))
        self.assertEqual((run.step_id, run.max_turns, run.agent.role_prompt), ("s-1", 25, "spec role"))
        self.assertEqual(tracker.reports, [("s-1", "done", "")])
        self.assertEqual(self.dispatcher.released, [42])

    def test_a_stopped_runner_takes_no_new_steps_and_leaves_them_pending(self):
        steps = self.runner()
        steps.stop()
        self.assertFalse(steps.submit("s-1", "requested"))
        self.assertIsNone(steps.handle("s-1"))
        self.assertEqual((self.tracker.claims, self.tracker.reports, self.dispatcher.runs), ([], [], []))

    def test_a_hand_off_recorded_by_the_run_is_its_report(self):
        class Recording(FakeDispatcher):
            def run_step(self, issue, repo, prompt, resume, model, run):
                run.outcome, run.comment = "blocked", "<p>stuck</p>"
                return "blocked"
        r = self.runner()
        r.dispatcher = Recording([])
        r.handle("s-1")
        self.assertEqual(self.tracker.reports, [("s-1", "blocked", "<p>stuck</p>")])

    def test_a_step_that_is_not_pending_or_not_ours_is_skipped(self):
        for s in (step(status="claimed"), step(actorUserId="someone else")):
            r = self.runner(tracker=StepTracker(make_issue(), [s]))
            self.assertIsNone(r.handle("s-1"))
            self.assertEqual((self.tracker.claims, self.dispatcher.runs, self.tracker.reports), ([], [], []))

    def test_a_refused_claim_runs_nothing(self):
        tracker = StepTracker(make_issue())
        tracker.refuse_claim = True
        self.assertIsNone(self.runner(tracker=tracker).handle("s-1"))
        self.assertEqual((self.dispatcher.runs, tracker.reports), ([], []))

    def test_a_step_naming_no_record_is_claimed_and_failed(self):
        tracker = StepTracker(make_issue(), [step(entityType=None)])
        self.assertEqual(self.runner(tracker=tracker).handle("s-1"), "failed")
        self.assertEqual(tracker.claims, ["s-1"])
        self.assertIn("names no record", tracker.reports[0][2])
        self.assertEqual(self.dispatcher.runs, [])

    def test_a_step_on_another_record_is_a_record_run(self):
        tracker = StepTracker(make_issue(), [step(entityType="milestone", entityId="m-1", session="resume")])
        self.assertEqual(self.runner(tracker=tracker).handle("s-1"), "done")
        self.assertEqual(tracker.claims, ["s-1"])
        record, prompt, resume, model, run = self.dispatcher.records[0]
        self.assertEqual((record.key, record.project, record.details["name"]), ("milestone:m-1", "A Gaia", "v1"))
        self.assertEqual((prompt, resume, run.agent.role_prompt), ("Work {not a placeholder} #42", True, "spec role"))
        self.assertEqual((self.dispatcher.runs, self.dispatcher.released), ([], ["milestone:m-1"]))
        self.assertEqual(tracker.reports, [("s-1", "done", "")])

    def test_an_unreadable_record_is_failed(self):
        tracker = StepTracker(make_issue(), [step(entityType="milestone", entityId="gone")])
        self.assertEqual(self.runner(tracker=tracker).handle("s-1"), "failed")
        self.assertIn("could not read the step's milestone: no milestone gone", tracker.reports[0][2])
        self.assertEqual(self.dispatcher.records, [])

    def test_a_record_run_reports_what_it_recorded(self):
        class Recording(FakeDispatcher):
            def run_record(self, record, prompt, resume, model, run):
                run.outcome, run.comment = "needs_input", "<p>which issue?</p>"
                return "needs_input"
        r = self.runner(tracker=StepTracker(make_issue(), [step(entityType="wikipage", entityId="w-1")]))
        r.dispatcher = Recording([])
        self.assertEqual(r.handle("s-1"), "needs_input")
        self.assertEqual(self.tracker.reports, [("s-1", "needs_input", "<p>which issue?</p>")])

    def test_a_record_run_waits_out_a_usage_limit_like_an_issue_step(self):
        tracker = StepTracker(make_issue(), [step(entityType="wikipage", entityId="w-1")])
        r = self.runner(outcomes=[policy.LIMITED, "done"], tracker=tracker)
        self.assertEqual(r.handle("s-1"), "done")
        self.assertEqual((len(self.dispatcher.records), self.dispatcher.waits), (2, 1))

    def test_an_issue_outside_the_agents_projects_is_failed(self):
        tracker = StepTracker(make_issue(project="Someone else's"))
        self.assertEqual(self.runner(tracker=tracker).handle("s-1"), "failed")
        self.assertIn("not one of", tracker.reports[0][2])

    def test_an_unreadable_issue_is_failed(self):
        class Unreadable(StepTracker):
            def issue_by_id(self, issue_id):
                raise TalosError("Gaia is down")
        tracker = Unreadable(make_issue())
        self.assertEqual(self.runner(tracker=tracker).handle("s-1"), "failed")
        self.assertIn("Gaia is down", tracker.reports[0][2])

    def test_a_run_that_breaks_in_talos_is_failed_and_frees_the_issue(self):
        class Broken(FakeDispatcher):
            def run_step(self, *a):
                raise OSError("disk full")
        r = self.runner()
        r.dispatcher = Broken([])
        self.assertEqual(r.handle("s-1"), "failed")
        self.assertIn("disk full", self.tracker.reports[0][2])
        self.assertEqual(r.dispatcher.released, [42])

    def test_an_unreadable_spec_is_failed(self):
        tracker = StepTracker(make_issue(), spec=None)
        self.assertEqual(self.runner(tracker=tracker).handle("s-1"), "failed")
        self.assertIn("agent spec", tracker.reports[0][2])
        self.assertEqual(self.dispatcher.runs, [])

    def test_a_usage_limit_runs_the_same_step_again_after_the_pause(self):
        r = self.runner(outcomes=[policy.LIMITED, "done"])
        self.assertEqual(r.handle("s-1"), "done")
        self.assertEqual((len(self.dispatcher.runs), self.dispatcher.waits), (2, 1))
        self.assertEqual(len(self.tracker.reports), 1)

    def test_a_usage_limit_cut_short_by_a_stop_is_reported_limit(self):
        r = self.runner(outcomes=[policy.LIMITED, "done"])
        self.dispatcher.stopping = True
        self.assertEqual(r.handle("s-1"), "limit")
        self.assertEqual(len(self.dispatcher.runs), 1)
        self.assertEqual((self.dispatcher.released, self.tracker.reports[0][1]), ([42], "limit"))

    def test_a_busy_issue_is_waited_for(self):
        r = self.runner(busy=2)
        with mock.patch.object(workflow_steps.time, "sleep") as sleep:
            self.assertEqual(r.handle("s-1"), "done")
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(self.tracker.claims, ["s-1"])     # claimed first, so the claim deadline can't pass

    def test_dry_run_neither_claims_nor_runs(self):
        r = self.runner(dry_run=True)
        self.assertIsNone(r.handle("s-1"))
        self.assertEqual((self.tracker.claims, self.dispatcher.runs, self.tracker.reports), ([], [], []))

    def test_events_route_run_requests_here_and_the_rest_to_the_dispatcher(self):
        r = self.runner()
        r.on_event({"eventName": "workflow.agent.run.requested", "meta": {"stepId": "s-1"}})
        r.on_event({"eventName": "workflow.agent.run.requested", "meta": {"stepId": "s-1"}})   # a repeat
        status = {"eventName": "issue.status.changed", "meta": {"issueNumber": 42}}
        r.on_event(status)
        self.assertEqual(r._queue.qsize(), 1)
        self.assertEqual(self.dispatcher.events, [status])

    def test_reconcile_queues_pending_steps_and_resyncs_the_dispatcher(self):
        tracker = StepTracker(make_issue(), [step(), step(id="s-2", status="finished")])
        r = self.runner(tracker=tracker)
        r.reconcile({})
        self.assertEqual((r._queue.qsize(), self.dispatcher.resyncs), (1, 1))

    def test_steps_claimed_before_a_restart_are_reported_failed(self):
        tracker = StepTracker(make_issue(), [step(status="claimed"), step(id="s-2")])
        self.runner(tracker=tracker).recover_interrupted()
        self.assertEqual([(s, o) for s, o, _ in tracker.reports], [("s-1", "failed")])


class DispatcherStepTest(unittest.TestCase):
    """Dispatcher.run_step: the step's agent and turn cap reach Claude, and the issue is left to the workflow."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        for p in (mock.patch.object(db, "DB_FILE", tmp / "talos.db"),
                  mock.patch.object(dispatcher_mod, "log", lambda *a, **k: None),
                  mock.patch.object(dispatcher_mod, "lease_file", lambda name: tmp / f"{name}-leases.json"),
                  mock.patch.object(dispatcher_mod.runner.workspaces, "ensure",
                                    lambda root, tools, number, repo: tmp / f"issue-{number}")):
            p.start()
            self.addCleanup(p.stop)
        self.builds, self.prompts = [], []

    def run_step(self, outcomes, issue=None, resume=False, model=None, max_turns=0, **agent_overrides):
        tracker = StubTracker(issue or make_issue(status="new"))
        replies = iter(outcomes)

        def fake_run(cmd, env, agent, number, prompt, cwd=None):
            self.prompts.append(prompt)
            return next(replies), Path("log.json")

        def fake_build(claude, agent, model, sid=None, new_session_id=None, limits=None, **kw):
            self.builds.append({"agent": agent, "model": model, "resume": sid, "limits": limits})
            return ["claude", "--model", model]

        agent = make_agent(projects={"A Gaia": "dev-tools"}, **agent_overrides)
        run = workflow_steps.StepRun("s-1", workflow_steps.with_spec(agent, SPEC), max_turns=max_turns)
        with mock.patch.object(dispatcher_mod.runner, "run", fake_run), \
                mock.patch.object(dispatcher_mod.runner, "build_command", fake_build):
            d = dispatcher_mod.Dispatcher(agent, tracker, "claude")
            outcome = d.run_step(tracker.issue(42), "dev-tools", "Work {x} #42", resume, model, run)
        return outcome, run, tracker

    def test_the_spec_agent_and_the_steps_turn_cap_reach_claude(self):
        outcome, run, tracker = self.run_step([result("done")], max_turns=12)
        self.assertEqual(outcome, "done")
        self.assertEqual(self.builds[0]["agent"].role_prompt, "spec role")
        self.assertEqual(self.builds[0]["limits"].max_turns, 12)
        self.assertEqual(self.prompts, ["Work {x} #42"])
        self.assertEqual(run.summary, "done")

    def test_without_a_step_turn_cap_the_spec_cap_holds(self):
        self.run_step([result("done")])
        self.assertEqual(self.builds[0]["limits"].max_turns, 40)

    def test_the_step_model_wins_and_the_agents_choice_is_the_default(self):
        self.run_step([result("done")], model="opus")
        self.run_step([result("done")], issue=make_issue(complexity="complex"))
        self.run_step([result("done")])
        self.assertEqual([b["model"] for b in self.builds], ["opus", "opus", "sonnet"])

    def test_the_issue_is_left_to_the_workflow(self):
        _, run, tracker = self.run_step([result("needs_input")])
        self.assertEqual((run.outcome, tracker.handoffs, tracker.statuses, tracker.comments),
                         ("needs_input", [], [], []))
        self.assertIn("needs your input", run.comment)

    def test_done_still_assigned_is_not_flagged(self):
        _, run, tracker = self.run_step([result("done")])
        self.assertIsNone(run.outcome)
        self.assertEqual(tracker.handoffs, [])

    def test_the_specs_runs_per_issue_cap_is_a_limit(self):
        sessions.update("odysseus", 42, runs=3)
        outcome, run, tracker = self.run_step([])
        self.assertEqual((outcome, run.outcome, tracker.handoffs), ("limit", "limit", []))
        self.assertEqual(self.builds, [])

    def test_ready_to_ship_still_ships_in_a_fresh_session(self):
        outcome, run, _ = self.run_step([result("ready_to_ship", handoff="note"), result("done", session="s2")],
                                        ship_prompt="Ship #{issue}: {handoff}")
        self.assertEqual(outcome, "done")
        self.assertEqual(self.prompts[1], "Ship #42: note")
        self.assertIsNone(self.builds[1]["resume"])

    def test_resume_continues_a_warm_session(self):
        sessions.update("odysseus", 42, session_id="old", last_ended_at=dispatcher_mod._iso(dispatcher_mod._now()))
        self.run_step([result("done")], resume=True)
        self.assertEqual(self.builds[0]["resume"], "old")


if __name__ == "__main__":
    unittest.main()
