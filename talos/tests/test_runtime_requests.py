"""The runtime's one run-request socket: routing to the agents' step runners, an unknown agent's request, reconnect."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import runtime_requests, workflow_steps  # noqa: E402


def envelope(sid="s-1", name=workflow_steps.STEP_EVENT):
    return {"eventName": name, "projectId": "runtimes", "meta": {"stepId": sid}}


def runner(user_id):
    r = mock.Mock(user_id=user_id)
    r.agent.name = f"agent-{user_id}"
    return r


class RouterTest(unittest.TestCase):
    def test_request_goes_to_every_registered_runner(self):
        router, a, b = runtime_requests.Router(), runner("u1"), runner("u2")
        router.register(a)
        router.register(b)
        router.on_event(envelope())
        a.submit.assert_called_once_with("s-1", "runtimes")
        b.submit.assert_called_once_with("s-1", "runtimes")

    def test_unknown_agent_is_ignored(self):
        router = runtime_requests.Router()
        router.on_event(envelope())     # nobody runs here: nothing to hand it to, nothing raised
        a = runner("u1")
        unregister = router.register(a)
        unregister()
        router.on_event(envelope())
        a.submit.assert_not_called()

    def test_other_events_are_ignored(self):
        router, a = runtime_requests.Router(), runner("u1")
        router.register(a)
        router.on_event(envelope(name="issue.status.changed"))
        router.on_event(None)
        a.submit.assert_not_called()

    def test_reconnect_reconciles_every_runner_even_when_one_fails(self):
        router, a, b = runtime_requests.Router(), runner("u1"), runner("u2")
        a.reconcile.side_effect = RuntimeError("down")
        router.register(a)
        router.register(b)
        router.on_ready()
        b.reconcile.assert_called_once_with()

    def test_listener_asks_for_the_runtimes_room_with_a_fresh_service_token(self):
        identity = mock.Mock()
        identity.token.side_effect = ["t1", "t2"]
        listener = runtime_requests.listener(runtime_requests.Router(), identity, "http://hermes")
        self.assertEqual([{"projectId": "runtimes", "eventName": workflow_steps.STEP_EVENT}], listener.intents)
        self.assertEqual("t1", listener.tracker.token())
        self.assertEqual("t2", listener.tracker.token())


if __name__ == "__main__":
    unittest.main()
