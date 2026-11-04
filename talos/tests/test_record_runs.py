"""Workflow steps on a record that isn't an issue: the record and its context, the outcome reported, the per-record
session, and the dispatcher's run in a scratch folder."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import db, dispatcher as dispatcher_mod, policy, record_runs, workflow_steps  # noqa: E402
from test_dispatcher import StubTracker, result  # noqa: E402
from test_policy import make_agent, make_issue  # noqa: E402
from test_workflow_steps import SPEC  # noqa: E402

MILESTONE = {"name": "Claude-v1", "id": "m-1", "status": "completed", "startDate": "2026-10-01",
             "endDate": "2026-10-31", "project": "A Gaia", "issueCount": 3,
             "issuesByStatus": {"done": 2, "in_review": 1}}


class MilestoneTracker:
    def __init__(self):
        self.read = []

    def milestone(self, milestone_id):
        self.read.append(milestone_id)
        return dict(MILESTONE)


class RecordTest(unittest.TestCase):
    def test_the_key_is_type_and_id_and_the_slug_is_a_safe_file_name(self):
        record = record_runs.Record("milestone", "a/b:c")
        self.assertEqual(record.key, "milestone:a/b:c")
        self.assertEqual(record.slug, "milestone-a_b_c")

    def test_a_milestone_is_read_with_its_details_and_project(self):
        tracker = MilestoneTracker()
        record = record_runs.read(tracker, "milestone", "m-1")
        self.assertEqual((tracker.read, record.project, record.details["name"]), (["m-1"], "A Gaia", "Claude-v1"))

    def test_another_record_type_is_taken_as_it_is(self):
        tracker = MilestoneTracker()
        self.assertEqual(record_runs.read(tracker, "wikipage", "w-1"), record_runs.Record("wikipage", "w-1"))
        self.assertEqual(tracker.read, [])


class ContextTest(unittest.TestCase):
    def test_a_milestone_context_names_it_its_dates_and_issues(self):
        text = record_runs.context(record_runs.Record("milestone", "m-1", "A Gaia", MILESTONE))
        self.assertIn("milestone with id m-1 in project A Gaia", text)
        self.assertIn("Milestone 'Claude-v1': status completed, 2026-10-01 to 2026-10-31, 3 issues "
                      "(2 done, 1 in_review)", text)
        self.assertIn("not an issue", text)

    def test_a_record_without_details_names_its_type_and_id(self):
        text = record_runs.context(record_runs.Record("wikipage", "w-1"))
        self.assertEqual(text.splitlines()[0], "This run is about the wikipage with id w-1.")
        self.assertEqual(len(text.splitlines()), 2)

    def test_the_context_comes_before_the_prompt(self):
        prompt = record_runs.with_context("Write the retro.", record_runs.Record("wikipage", "w-1"))
        self.assertTrue(prompt.startswith("This run is about"))
        self.assertTrue(prompt.endswith("\n\nWrite the retro."))


class ReportTest(unittest.TestCase):
    def test_done_reports_the_summary(self):
        self.assertEqual(record_runs.report("done", "a & b", "<p>c</p>"), ("done", "<p>a &amp; b</p>"))

    def test_needs_input_and_blocked_report_the_comment_or_the_summary(self):
        self.assertEqual(record_runs.report("needs_input", "s", "<p>q</p>"), ("needs_input", "<p>q</p>"))
        self.assertEqual(record_runs.report("blocked", "stuck", ""), ("blocked", "<p>stuck</p>"))

    def test_escalate_is_needs_input(self):
        self.assertEqual(record_runs.report("escalate", "s", "<p>hard</p>"), ("needs_input", "<p>hard</p>"))

    def test_the_repo_outcomes_and_failures_are_failed(self):
        outcome, comment = record_runs.report("ready_to_ship", "s", "")
        self.assertEqual(outcome, "failed")
        self.assertIn("'ready_to_ship', which a record run can't use", comment)
        outcome, comment = record_runs.report("failed", "no result from Claude", "tail <x>")
        self.assertEqual((outcome, comment), ("failed", "<p>no result from Claude</p><pre>tail &lt;x&gt;</pre>"))


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for p in (mock.patch.object(db, "DB_FILE", self.tmp / "talos.db"),
                  mock.patch.object(record_runs, "RECORDS_DIR", self.tmp / "records")):
            p.start()
            self.addCleanup(p.stop)

    def test_sessions_are_kept_per_agent_and_record(self):
        m1, m2 = record_runs.Record("milestone", "1"), record_runs.Record("milestone", "2")
        record_runs.save_session("odysseus", m1, "s1", 0.5, "2026-11-05T10:00:00+00:00")
        record_runs.save_session("odysseus", m1, "s2", 0.75, "2026-11-05T11:00:00+00:00")
        self.assertEqual(record_runs.session("odysseus", m1)["session_id"], "s2")
        self.assertEqual(record_runs.session("odysseus", m2), {})
        self.assertEqual(record_runs.session("nestor", m1), {})

    def test_the_scratch_folder_is_per_agent_and_record_under_the_state(self):
        path = record_runs.folder("odysseus", record_runs.Record("milestone", "m:1"))
        self.assertEqual(path, self.tmp / "records" / "odysseus" / "milestone-m_1")
        self.assertTrue(path.is_dir())


class DispatcherRecordTest(unittest.TestCase):
    """Dispatcher.run_record: Claude in the scratch folder, the context on a fresh session, the report on the step."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for p in (mock.patch.object(db, "DB_FILE", self.tmp / "talos.db"),
                  mock.patch.object(record_runs, "RECORDS_DIR", self.tmp / "records"),
                  mock.patch.object(dispatcher_mod, "log", lambda *a, **k: None),
                  mock.patch.object(dispatcher_mod, "lease_file", lambda name: self.tmp / f"{name}-leases.json"),
                  mock.patch.object(dispatcher_mod.runner.workspaces, "ensure", self.fail_workspace)):
            p.start()
            self.addCleanup(p.stop)
        self.builds, self.runs = [], []
        self.record = record_runs.Record("milestone", "m-1", "A Gaia", MILESTONE)

    def fail_workspace(self, *a):
        raise AssertionError("a record run made a workspace")

    def run_record(self, outcomes, resume=False, model=None, max_turns=0):
        tracker = StubTracker(make_issue())
        replies = iter(outcomes)

        def fake_run(cmd, env, agent, name, prompt, cwd=None):
            self.runs.append({"name": name, "prompt": prompt, "cwd": cwd})
            return next(replies), Path("log.json")

        def fake_build(claude, agent, model, sid=None, new_session_id=None, limits=None, **kw):
            self.builds.append({"agent": agent, "model": model, "resume": sid, "limits": limits})
            return ["claude", "--model", model]

        agent = make_agent(max_parallel=2, test_slots=2)
        run = workflow_steps.StepRun("s-1", workflow_steps.with_spec(agent, SPEC), max_turns=max_turns)
        with mock.patch.object(dispatcher_mod.runner, "run", fake_run), \
                mock.patch.object(dispatcher_mod.runner, "build_command", fake_build):
            d = dispatcher_mod.Dispatcher(agent, tracker, "claude")
            leases = []
            acquire = d._gate.acquire
            d._gate.acquire = lambda *a: leases.append(acquire(*a)) or leases[-1]
            outcome = d.run_record(self.record, "Write the retro.", resume, model, run)
        return outcome, run, tracker, leases

    def test_it_runs_in_the_records_scratch_folder_with_the_context(self):
        outcome, run, tracker, leases = self.run_record([result("done")])
        self.assertEqual(outcome, "done")
        self.assertEqual(self.runs[0]["cwd"], self.tmp / "records" / "odysseus" / "milestone-m-1")
        self.assertEqual(self.runs[0]["name"], "milestone-m-1")
        self.assertTrue(self.runs[0]["prompt"].startswith("This run is about the milestone with id m-1"))
        self.assertTrue(self.runs[0]["prompt"].endswith("Write the retro."))
        self.assertEqual((leases[0].issue, leases[0].repo, leases[0].slot), ("milestone:m-1", "", 0))
        self.assertEqual((run.outcome, run.comment, run.summary), ("done", "<p>done</p>", "done"))
        self.assertEqual((tracker.comments, tracker.handoffs, tracker.statuses), ([], [], []))

    def test_the_spec_agent_the_step_turn_cap_and_model_reach_claude(self):
        self.run_record([result("done")], model="opus", max_turns=12)
        self.run_record([result("done")])
        self.assertEqual(self.builds[0]["agent"].role_prompt, "spec role")
        self.assertEqual((self.builds[0]["model"], self.builds[0]["limits"].max_turns), ("opus", 12))
        self.assertEqual((self.builds[1]["model"], self.builds[1]["limits"].max_turns), ("sonnet", 40))

    def test_resume_continues_the_records_warm_session_without_the_context_again(self):
        record_runs.save_session("odysseus", self.record, "old", 0.2, dispatcher_mod._iso(dispatcher_mod._now()))
        self.run_record([result("done", session="old")], resume=True)
        self.assertEqual(self.builds[0]["resume"], "old")
        self.assertEqual(self.runs[0]["prompt"], "Write the retro.")

    def test_a_new_session_is_kept_for_the_record(self):
        self.run_record([result("done", session="fresh")])
        self.assertEqual(record_runs.session("odysseus", self.record)["session_id"], "fresh")

    def test_a_hand_off_is_recorded_on_the_step_not_the_tracker(self):
        _, run, tracker, _ = self.run_record([result("needs_input")])
        self.assertEqual((run.outcome, run.comment), ("needs_input", "<p>why</p>"))
        self.assertEqual(workflow_steps.report("needs_input", run), ("needs_input", "<p>why</p>"))
        self.assertEqual((tracker.comments, tracker.handoffs), ([], []))

    def test_a_usage_limit_pauses_and_leaves_the_report_open(self):
        limited = {"is_error": True, "subtype": "error", "result": "Claude AI usage limit reached|1792581600"}
        outcome, run, _, _ = self.run_record([limited])
        self.assertEqual((outcome, run.outcome), (policy.LIMITED, None))

    def test_a_run_that_breaks_is_failed(self):
        outcome, run, _, _ = self.run_record([None])
        self.assertEqual((outcome, run.outcome), ("failed", "failed"))

    def test_a_bookkeeping_failure_still_closes_the_run(self):
        with mock.patch.object(record_runs, "save_session", side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            self.run_record([result("done")])
        row = db.connect().execute("SELECT outcome, ended_at FROM runs WHERE issue = 'milestone:m-1'").fetchone()
        self.assertEqual(row[0], "talos error")
        self.assertIsNotNone(row[1])


if __name__ == "__main__":
    unittest.main()
