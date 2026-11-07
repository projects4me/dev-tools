"""Unit tests for the Merger's CI reads with stubbed gh output. Run: python -m unittest discover -s tests"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.orchestrator import github, merge  # noqa: E402


class CiReadsTest(unittest.TestCase):
    def merger(self, answers):
        calls = []

        def gh(*args):
            calls.append(args)
            return next(v for k, v in answers.items() if k in " ".join(args))
        patcher = mock.patch.object(github, "known_repos", return_value={"gaia": "o/gaia"})
        patcher.start()
        self.addCleanup(patcher.stop)
        return merge.Merger(lambda: {}, gh)

    def test_failed_logs_keep_the_tail_per_job(self):
        log = "\n".join(f"tests\tRun\t2026 line{i}" for i in range(5)) + "\nlint\tRun\t2026 bad"
        tails = self.merger({"--log-failed": log}).failed_logs("gaia", "9", tail=2)
        self.assertEqual(tails, {"tests": "2026 line3\n2026 line4", "lint": "2026 bad"})

    def test_base_run_none_when_the_base_has_no_completed_run(self):
        m = self.merger({"actions/runs/9": "5\n", "workflows/5/runs": "[]"})
        self.assertIsNone(m.base_run("gaia", "9", "dev"))

    def test_base_run_lists_the_jobs(self):
        m = self.merger({"actions/runs/9": "5\n",
                         "workflows/5/runs": '[{"run": 4, "sha": "b", "conclusion": "failure"}]',
                         "actions/runs/4/jobs": '[{"name": "tests", "conclusion": "failure"}]'})
        self.assertEqual(m.base_run("gaia", "9", "dev"),
                         {"run": 4, "sha": "b", "conclusion": "failure",
                          "jobs": [{"name": "tests", "conclusion": "failure"}]})


if __name__ == "__main__":
    unittest.main()
