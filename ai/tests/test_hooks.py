"""Unit tests for hooks/guard.py. Run: python -m unittest discover -s tests"""

import json
import os
import subprocess
import tempfile
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS))

import guard  # noqa: E402

REST = "D:/Pr4M/gaia/core/mvc/controllers/RestController.php"


class GuardTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(guard, "log")
        p.start()
        self.addCleanup(p.stop)
        self.changed = set()
        c = mock.patch.object(guard, "changed_set", side_effect=lambda root: self.changed)
        c.start()
        self.addCleanup(c.stop)

    def test_read_of_framework_file_is_blocked_with_the_gaia_dev_command(self):
        kind, reason = guard.decide("Read", {"file_path": REST.replace("/", "\\")})
        self.assertEqual(kind, "read")
        self.assertIn("gaia-dev", reason)

    def test_each_listed_file_is_protected(self):
        for f in ("core/libs/security/Acl.php", "core/mvc/models/Model.php", "tools/api-tester/src/Runner.php",
                  "tools/api-tester/apis/x.json", "tools/api-tester/harness/h.php", "fixtures/default.json"):
            self.assertIsNotNone(guard.decide("Read", {"file_path": "D:/Pr4M/gaia/" + f}), f)

    def test_other_files_are_allowed(self):
        self.assertIsNone(guard.decide("Read", {"file_path": "D:/Pr4M/gaia/core/mvc/models/Task.php"}))
        self.assertIsNone(guard.decide("Write", {"file_path": REST}))

    def test_changed_file_is_allowed(self):
        self.changed = {"core/mvc/controllers/RestController.php"}
        self.assertIsNone(guard.decide("Read", {"file_path": REST}))

    def test_bash_read_is_blocked_and_raw_escapes(self):
        cmd = "cat gaia/core/libs/security/Acl.php"
        self.assertEqual(guard.decide("Bash", {"command": cmd})[0], "read")
        self.assertIsNone(guard.decide("Bash", {"command": cmd + " # raw"}))
        self.assertIsNone(guard.decide("Bash", {"command": "grep --raw foo gaia/core/libs/security/Acl.php"}))

    def test_bash_without_read_verb_or_protected_path_is_allowed(self):
        self.assertIsNone(guard.decide("Bash", {"command": "git log -- gaia/core/libs/security/Acl.php"}))
        self.assertIsNone(guard.decide("Bash", {"command": "cat README.md"}))

    def test_docker_suite_is_denied(self):
        for cmd in ("docker exec gaia vendor/bin/phpunit", "docker run --rm img npm test", "docker exec x pytest -q"):
            self.assertEqual(guard.decide("Bash", {"command": cmd})[0], "docker", cmd)
        self.assertIsNone(guard.decide("Bash", {"command": "docker ps"}))
        self.assertIsNone(guard.decide("Bash", {"command": "dev-tools/ai/ai test gaia"}))

    def test_script_emits_deny_json_and_runs_fast(self):
        payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "D:/x/gaia/core/libs/security/Acl.php"}})
        t = time.perf_counter()
        out = subprocess.run([sys.executable, str(HOOKS / "guard.py")], input=payload, capture_output=True, text=True,
                             env={**os.environ, "AI_HOOK_LOG": os.path.join(tempfile.gettempdir(), "ai-hook-test.jsonl")}).stdout
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertLess(time.perf_counter() - t, 1.0)   # includes interpreter start; the 100 ms budget is checked below

    def test_decide_is_under_budget_for_a_plain_command(self):
        t = time.perf_counter()
        for _ in range(100):
            guard.decide("Bash", {"command": "git status"})
        self.assertLess((time.perf_counter() - t) / 100, 0.1)


if __name__ == "__main__":
    unittest.main()
