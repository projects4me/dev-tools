"""The shared usage-limit pause: one pause for concurrent hits, interruptible wait."""

import sys
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.usage_pause import UsagePause  # noqa: E402


class UsagePauseTest(unittest.TestCase):
    def test_concurrent_hits_start_one_pause(self):
        pause, started = UsagePause(), []
        reset = datetime.now().astimezone() + timedelta(hours=1)
        threads = [threading.Thread(target=lambda: started.append(pause.hit(reset))) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(len([s for s in started if s is not None]), 1)
        self.assertEqual(pause._streak, 1)

    def test_backoff_doubles_and_success_resets_it(self):
        now = [datetime.now().astimezone()]
        pause = UsagePause(now=lambda: now[0])
        first = pause.hit(None) - now[0]
        now[0] += first + timedelta(seconds=1)
        self.assertEqual(pause.hit(None) - now[0], first * 2)
        pause.succeeded()
        now[0] += first * 2 + timedelta(seconds=1)
        self.assertEqual(pause.hit(None) - now[0], first)

    def test_stop_interrupts_a_wait(self):
        pause, result = UsagePause(), []
        pause.hit(datetime.now().astimezone() + timedelta(hours=5))
        t = threading.Thread(target=lambda: result.append(pause.wait()))
        t.start()
        pause.stop()
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(result, [False])

    def test_wait_returns_true_once_when_the_pause_ends(self):
        now = [datetime.now().astimezone()]
        pause = UsagePause(now=lambda: now[0])
        pause.hit(None)
        now[0] += timedelta(hours=7)
        self.assertTrue(pause.wait())
        self.assertFalse(pause.wait())


if __name__ == "__main__":
    unittest.main()
