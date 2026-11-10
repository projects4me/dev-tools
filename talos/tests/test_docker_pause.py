"""The Docker hold: waits while down or pressured, resumes with hysteresis, one start per hold, stop() releases."""

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.docker_pause import DockerPause  # noqa: E402


def state(used=0, daemon=True):
    return {"cli": True, "daemon": daemon, "memTotal": 100, "memUsed": used}


class Fake:
    """A docker state that changes by hand, and a clock that moves by hand."""

    def __init__(self, st):
        self.st, self.now, self.reads = st, 0.0, 0

    def read(self):
        self.reads += 1
        return self.st

    def clock(self):
        return self.now


def make(fake, **kw):
    kw.setdefault("check_seconds", 30)
    kw.setdefault("grace_seconds", 60)
    return DockerPause(read=fake.read, clock=fake.clock, poll_seconds=0.01, **kw)


class DockerPauseTest(unittest.TestCase):
    def test_ok_does_not_wait(self):
        pause = make(Fake(state(10)))
        self.assertFalse(pause.wait())
        self.assertIsNone(pause.since)

    def test_pauses_one_container_once_and_unpauses_when_ok(self):
        fake = Fake(state(90))
        paused, resumed = [], []
        pause = make(fake, check_seconds=0, free=lambda: None,
                     pause_one=lambda: paused.append(1) or ["b"], resume=resumed.append)
        t = threading.Thread(target=pause.wait)
        t.start()
        t.join(0.3)
        self.assertEqual(paused, [1])
        self.assertEqual(resumed, [])
        fake.st = state(10)
        t.join(5)
        self.assertEqual(resumed, [["b"]])

    def test_verdict_is_reread_at_most_every_check_seconds(self):
        fake = Fake(state(10))
        pause = make(fake)
        pause.wait()
        pause.wait()
        self.assertEqual(fake.reads, 1)
        fake.now = 31
        pause.wait()
        self.assertEqual(fake.reads, 2)

    def test_waits_while_pressured_and_resumes_with_hysteresis(self):
        fake = Fake(state(90))
        pause = make(fake, check_seconds=0)
        changes = []
        pause._on_change = lambda verdict, since: changes.append((verdict, since is not None))
        result = []
        t = threading.Thread(target=lambda: result.append(pause.wait()))
        t.start()
        t.join(0.2)
        self.assertTrue(t.is_alive())
        fake.st = state(75)         # below the high mark but above ok: still held
        t.join(0.2)
        self.assertTrue(t.is_alive())
        fake.st = state(60)
        t.join(5)
        self.assertEqual(result, [True])
        self.assertEqual(changes, [("pressured", True), ("ok", False)])

    def test_down_starts_the_engine_once_after_the_grace_period(self):
        fake = Fake(state(daemon=False))
        started = []
        pause = make(fake, check_seconds=0, start=lambda: started.append(1))
        t = threading.Thread(target=pause.wait)
        t.start()
        t.join(0.1)
        self.assertEqual(started, [])       # still booting, maybe
        fake.now = 61
        t.join(0.2)
        fake.now = 200
        t.join(0.2)
        self.assertEqual(started, [1])
        pause.stop()
        t.join(5)

    def test_a_new_hold_may_start_again(self):
        fake = Fake(state(daemon=False))
        started = []
        pause = make(fake, check_seconds=0, start=lambda: started.append(1))
        pause._refresh()
        fake.now = 61
        pause._maybe_start()
        fake.st = state(10)
        self.assertTrue(pause._refresh())
        fake.st = state(daemon=False)
        pause._refresh()
        fake.now = 200
        pause._maybe_start()
        self.assertEqual(started, [1, 1])

    def test_no_start_when_auto_start_is_off_and_a_failed_start_keeps_waiting(self):
        fake = Fake(state(daemon=False))
        started = []
        off = make(fake, check_seconds=0, auto_start=False, start=lambda: started.append(1))
        off._refresh()
        fake.now = 100
        off._maybe_start()
        self.assertEqual(started, [])

        def boom():
            raise RuntimeError("no engine")
        failing = make(fake, check_seconds=0, start=boom)
        failing._refresh()
        failing._maybe_start()
        self.assertIsNotNone(failing.since)

    def test_free_is_called_once_per_pressured_hold_and_verdict_rechecked(self):
        fake = Fake(state(90))
        freed = []

        def free():
            freed.append(1)
            fake.st = state(10)         # the stopped containers gave the memory back
        pause = make(fake, check_seconds=0, free=free)
        self.assertTrue(pause.wait())
        self.assertEqual(freed, [1])
        fake.st = state(90)             # a new hold may free again
        pause._refresh()
        pause.wait()
        self.assertEqual(freed, [1, 1])

    def test_free_not_called_when_down_and_a_failure_keeps_the_hold(self):
        down = make(Fake(state(daemon=False)), check_seconds=0, free=lambda: self.fail("down must not free"))
        down._refresh()
        down._maybe_free()

        def boom():
            raise RuntimeError("no docker")
        pressured = make(Fake(state(90)), check_seconds=0, free=boom)
        pressured._refresh()
        pressured._maybe_free()
        self.assertIsNotNone(pressured.since)

    def test_stop_releases_waiters(self):
        pause = make(Fake(state(daemon=False)), check_seconds=0)
        result = []
        t = threading.Thread(target=lambda: result.append(pause.wait()))
        t.start()
        t.join(0.1)
        pause.stop()
        t.join(5)
        self.assertEqual(result, [False])


if __name__ == "__main__":
    unittest.main()
