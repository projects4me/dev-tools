"""The CodeRabbit loop (adt-4190) with a faked `ai rabbit` and `ai handoff`: wait, bot rounds, cap, timeout, absent,
restart and reassignment while waiting."""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import bot_review, db, dispatcher as dispatcher_mod, policy  # noqa: E402
from test_dispatcher import StubTracker, result  # noqa: E402
from test_policy import AGENT_ID, make_agent, make_issue  # noqa: E402
from lib.registry import Action  # noqa: E402

REVIEWER = "1"
BOT_PROMPT = "Bot round {round}/{rounds} on PR {pr} for #{issue} in {repo}"


def awaiting(pr=77, session="s1"):
    r = result(policy.AWAITING_BOT_REVIEW, session=session)
    r["structured_output"]["pr"] = pr
    return r


class Clock:
    def __init__(self):
        self.moment = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.moment

    def advance(self, minutes):
        self.moment += timedelta(minutes=minutes)


class Rabbit:
    """A fake `ai rabbit`: returns the scripted states in turn (the last one repeats)."""

    def __init__(self, *states):
        self.states, self.calls = list(states), []

    def __call__(self, repo, pr):
        self.calls.append((repo, pr))
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]


class BotReviewTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.patches = [mock.patch.object(db, "DB_FILE", tmp / "talos.db"),
                        mock.patch.object(dispatcher_mod, "log", lambda *a, **k: None),
                        mock.patch.object(bot_review, "log", lambda *a, **k: None),
                        mock.patch.object(dispatcher_mod, "lease_file", lambda name: tmp / f"{name}-leases.json"),
                        mock.patch.object(dispatcher_mod.runner.workspaces, "ensure",
                                          lambda root, tools, number, repo: tmp / f"issue-{number}")]
        for p in self.patches:
            p.start()
        self.clock = Clock()
        self.prompts, self.resumed = [], []
        self.handoffs = []
        self.submitted = []

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def agent(self, **overrides):
        fields = dict(actions={"in_progress": Action("Continue #{issue}", resume=True)}, pickup_status="in_progress",
                      bot_round_prompt=BOT_PROMPT, bot_review_rounds=3, bot_review_wait_minutes=20)
        fields.update(overrides)
        return make_agent(**fields)

    def dispatcher(self, rabbit, issue=None, **agent_overrides):
        self.tracker = StubTracker(issue or make_issue(status="in_progress"))
        d = dispatcher_mod.Dispatcher(self.agent(**agent_overrides), self.tracker, "claude")
        d.submit = lambda number, source, reconcile=False: self.submitted.append(number) or True
        d.bot_review = bot_review.Watcher(d.agent, AGENT_ID, self.tracker, d.submit, rabbit=rabbit,
                                          handoff=self.fake_handoff, now=self.clock)
        return d

    def fake_handoff(self, number):
        self.handoffs.append(number)
        self.tracker._issue.update(status="in_review", assignee=REVIEWER)

    def run_with(self, d, outcomes):
        replies = iter(outcomes)

        def fake_run(cmd, env, agent, number, prompt, cwd=None):
            self.prompts.append(prompt)
            return next(replies), Path("log.json")

        def fake_build(claude, agent, model, sid=None, new_session_id=None, limits=None, mcp=None, memory=None):
            self.resumed.append(sid)
            return ["claude", "--model", model]

        with mock.patch.object(dispatcher_mod.runner, "run", fake_run), \
                mock.patch.object(dispatcher_mod.runner, "build_command", fake_build):
            return d.handle(42)

    def ship(self, d):
        """The ship session ends with awaiting_bot_review: the issue waits, still with the agent."""
        self.assertEqual(self.run_with(d, [awaiting()]), policy.AWAITING_BOT_REVIEW)
        record = bot_review.get("odysseus", 42)
        self.assertEqual((record["phase"], record["repo"], record["pr"], record["rounds"]),
                         (bot_review.WAITING, "gaia", 77, 0))
        self.assertEqual(self.tracker.handoffs, [])
        self.assertEqual(self.tracker.comments, [])

    # -- the flows of the issue -------------------------------------------------------------------

    def test_clean_on_the_first_pass_hands_off(self):
        d = self.dispatcher(Rabbit("clean"))
        self.ship(d)
        self.assertEqual(d.bot_review.check_one(bot_review.get("odysseus", 42)), bot_review.HAND_OFF)
        self.assertEqual(self.handoffs, [42])
        self.assertEqual(bot_review.get("odysseus", 42), {})
        self.assertEqual(self.tracker.comments, [])     # nothing about the loop on the tracker

    def test_pending_keeps_waiting_without_a_run(self):
        d = self.dispatcher(Rabbit("pending"))
        self.ship(d)
        d.bot_review.check()
        self.assertEqual((self.handoffs, self.submitted), ([], []))
        self.assertEqual(bot_review.get("odysseus", 42)["phase"], bot_review.WAITING)

    def test_findings_then_clean(self):
        rabbit = Rabbit("findings", "clean")
        d = self.dispatcher(rabbit)
        self.ship(d)
        d.bot_review.check()
        self.assertEqual(self.submitted, [42])
        record = bot_review.get("odysseus", 42)
        self.assertEqual((record["phase"], record["rounds"]), (bot_review.ROUND, 1))

        # the bot round resumes the ship session with the bot-round prompt, then waits again
        self.assertEqual(self.run_with(d, [awaiting(session="s1")]), policy.AWAITING_BOT_REVIEW)
        self.assertEqual(self.prompts[-1], "Bot round 1/3 on PR 77 for #42 in gaia")
        self.assertEqual(self.resumed[-1], "s1")
        self.assertEqual(bot_review.get("odysseus", 42)["phase"], bot_review.WAITING)

        d.bot_review.check()
        self.assertEqual(self.handoffs, [42])
        self.assertEqual(rabbit.calls, [("gaia", 77), ("gaia", 77)])
        self.assertEqual(bot_review.get("odysseus", 42), {})

    def test_round_cap_hands_off_with_findings_open(self):
        d = self.dispatcher(Rabbit("findings"), bot_review_rounds=2)
        self.ship(d)
        for _ in range(2):
            d.bot_review.check()
            self.run_with(d, [awaiting()])
        self.assertEqual(self.submitted, [42, 42])
        d.bot_review.check()
        self.assertEqual(self.submitted, [42, 42])       # no third round
        self.assertEqual(self.handoffs, [42])

    def test_bot_rounds_count_towards_max_iterations(self):
        d = self.dispatcher(Rabbit("findings"), max_iterations=2)
        self.ship(d)                                     # iteration 1
        d.bot_review.check()
        self.run_with(d, [awaiting()])                   # iteration 2
        d.bot_review.check()
        self.assertEqual(self.run_with(d, []), "limit")
        self.assertEqual(self.tracker.handoffs, [("pending", "1")])
        self.assertEqual(bot_review.get("odysseus", 42), {})

    def test_timeout_hands_off(self):
        d = self.dispatcher(Rabbit("pending"))
        self.ship(d)
        self.clock.advance(19)
        d.bot_review.check()
        self.assertEqual(self.handoffs, [])
        self.clock.advance(2)
        d.bot_review.check()
        self.assertEqual(self.handoffs, [42])

    def test_absent_and_paused_hand_off_straight_away(self):
        for state in ("absent", "paused"):
            with self.subTest(state):
                d = self.dispatcher(Rabbit(state))
                self.handoffs = []
                self.ship(d)
                d.bot_review.check()
                self.assertEqual(self.handoffs, [42])

    def test_restart_while_waiting(self):
        d = self.dispatcher(Rabbit("clean"))
        self.ship(d)
        # a new Talos: reconcile submits the assigned issue, the record keeps it from running again
        restarted = self.dispatcher(Rabbit("clean"), issue=dict(self.tracker._issue))
        restarted.recover_interrupted()
        self.assertIsNone(self.run_with(restarted, []))
        self.assertEqual(self.prompts, [self.prompts[0]])
        restarted.bot_review.check()
        self.assertEqual(self.handoffs, [42])

    def test_restart_with_a_queued_round_waits_again(self):
        d = self.dispatcher(Rabbit("findings"))
        self.ship(d)
        d.bot_review.check()                            # round queued, Talos stops before it runs
        restarted = self.dispatcher(Rabbit("clean"), issue=dict(self.tracker._issue))
        restarted.recover_interrupted()
        record = bot_review.get("odysseus", 42)
        self.assertEqual((record["phase"], record["rounds"]), (bot_review.WAITING, 0))
        restarted.bot_review.check()
        self.assertEqual(self.handoffs, [42])

    def test_reassignment_while_waiting_drops_the_wait(self):
        d = self.dispatcher(Rabbit("findings"))
        self.ship(d)
        self.tracker._issue.update(assignee=REVIEWER)
        self.assertEqual(d.bot_review.check_one(bot_review.get("odysseus", 42)), "dropped")
        self.assertEqual((self.handoffs, self.submitted), ([], []))
        self.assertEqual(bot_review.get("odysseus", 42), {})

    def test_reassignment_with_a_queued_round_works_it_as_usual(self):
        d = self.dispatcher(Rabbit("findings"), actions={"in_progress": Action("Continue #{issue}", resume=True),
                                                         "feedback": Action("Feedback #{issue}", resume=True)})
        self.ship(d)
        d.bot_review.check()                            # round queued, then the reviewer answers first
        self.tracker._issue.update(status="feedback")
        self.assertEqual(self.run_with(d, [result("needs_input")]), "needs_input")
        self.assertEqual(self.prompts[-1], "Feedback #42")
        self.assertEqual(bot_review.get("odysseus", 42), {})

    def test_round_breaking_in_talos_waits_again_counted(self):
        d = self.dispatcher(Rabbit("findings"))
        self.ship(d)
        d.bot_review.check()
        with mock.patch.object(d, "_run", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.run_with(d, [])
        record = bot_review.get("odysseus", 42)
        self.assertEqual((record["phase"], record["rounds"]), (bot_review.WAITING, 1))

    def test_round_still_queued_keeps_the_deadline(self):
        d = self.dispatcher(Rabbit("findings"))
        self.ship(d)
        deadline = bot_review.get("odysseus", 42)["deadline"]
        d.bot_review.submit = lambda number, source: False
        self.clock.advance(5)
        d.bot_review.check()
        record = bot_review.get("odysseus", 42)
        self.assertEqual((record["phase"], record["rounds"], record["deadline"]), (bot_review.WAITING, 0, deadline))

    def test_no_round_for_a_dropped_record(self):
        d = self.dispatcher(Rabbit("findings"))
        self.ship(d)
        record = bot_review.get("odysseus", 42)
        bot_review.drop("odysseus", 42)
        self.assertEqual(d.bot_review.check_one(record), bot_review.WAIT)
        self.assertEqual(self.submitted, [])

    def test_handed_back_in_another_status_works_it_as_usual(self):
        d = self.dispatcher(Rabbit("pending"), actions={"in_progress": Action("Continue #{issue}", resume=True),
                                                        "feedback": Action("Feedback #{issue}", resume=True)})
        self.ship(d)
        self.tracker._issue.update(status="feedback")       # the reviewer answered before the watcher noticed
        self.assertEqual(self.run_with(d, [result("needs_input")]), "needs_input")
        self.assertEqual(self.prompts[-1], "Feedback #42")
        self.assertEqual(bot_review.get("odysseus", 42), {})

    def test_round_ending_otherwise_leaves_the_loop(self):
        d = self.dispatcher(Rabbit("findings"))
        self.ship(d)
        d.bot_review.check()
        self.assertEqual(self.run_with(d, [result("blocked")]), "blocked")
        self.assertEqual(bot_review.get("odysseus", 42), {})
        self.assertEqual(self.tracker.handoffs, [("pending", "1")])

    def test_awaiting_without_a_pr_fails(self):
        d = self.dispatcher(Rabbit("clean"))
        self.assertEqual(self.run_with(d, [result(policy.AWAITING_BOT_REVIEW)]), "failed")
        self.assertEqual(bot_review.get("odysseus", 42), {})

    def test_no_bot_round_prompt_means_no_rounds(self):
        d = self.dispatcher(Rabbit("findings"), bot_round_prompt="")
        self.ship(d)
        d.bot_review.check()
        self.assertEqual((self.submitted, self.handoffs), ([], [42]))

    def test_failed_handoff_is_retried(self):
        d = self.dispatcher(Rabbit("clean"))
        self.ship(d)

        def broken(number):
            raise bot_review.TalosError("Gaia is down")

        d.bot_review.handoff = broken
        d.bot_review.check()
        self.assertEqual(bot_review.get("odysseus", 42)["phase"], bot_review.WAITING)
        d.bot_review.handoff = self.fake_handoff
        d.bot_review.check()
        self.assertEqual(self.handoffs, [42])


class NextStepTest(unittest.TestCase):
    def test_states(self):
        self.assertEqual(bot_review.next_step("clean", 0, 3, False), bot_review.HAND_OFF)
        self.assertEqual(bot_review.next_step("findings", 2, 3, False), bot_review.RUN_ROUND)
        self.assertEqual(bot_review.next_step("findings", 3, 3, False), bot_review.HAND_OFF)
        self.assertEqual(bot_review.next_step("pending", 0, 3, False), bot_review.WAIT)
        self.assertEqual(bot_review.next_step("pending", 0, 3, True), bot_review.HAND_OFF)
        self.assertEqual(bot_review.next_step("absent", 0, 3, False), bot_review.HAND_OFF)
        self.assertEqual(bot_review.next_step("paused", 0, 3, False), bot_review.HAND_OFF)

    def test_read_pr(self):
        self.assertEqual(policy.read_pr(awaiting(pr=12)), 12)
        self.assertEqual(policy.read_pr(awaiting(pr="#12")), 12)
        self.assertIsNone(policy.read_pr(result("done")))
        self.assertIsNone(policy.read_pr(None))


class AiRabbitTest(unittest.TestCase):
    def test_reads_the_state_from_the_json_line(self):
        done = mock.Mock(returncode=1, stdout='{"state": "findings", "open": 2, "sha": "abc"}\n', stderr="")
        with mock.patch.object(bot_review.subprocess, "run", return_value=done) as run:
            self.assertEqual(bot_review.ai_rabbit({}, "gaia", 77), "findings")
        self.assertEqual(run.call_args[0][0][1:], [str(bot_review.AI), "rabbit", "gaia", "77", "--json"])

    def test_no_json_is_an_error(self):
        done = mock.Mock(returncode=1, stdout="", stderr="gh: not found")
        with mock.patch.object(bot_review.subprocess, "run", return_value=done):
            with self.assertRaises(bot_review.TalosError):
                bot_review.ai_rabbit({}, "gaia", 77)


if __name__ == "__main__":
    unittest.main()
