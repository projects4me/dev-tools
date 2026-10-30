"""Ship size gate. Run: python -m unittest discover -s tests"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import sizecheck  # noqa: E402
from lib.common import AiError  # noqa: E402

CFG = {"warnOver": 10, "refuseOver": 15,
       "generated": ["gaia/docs/generated/**", "docs/generated/**", "tools/api-tester/apis/backend.json",
                     "**/package-lock.json"]}


def files(n):
    return [f"src/f{i}.py" for i in range(n)]


class SizeTest(unittest.TestCase):
    def test_generated_paths_are_excluded(self):
        spec = files(2) + ["docs/generated/graph.json", "gaia/docs/generated/a/b.md",
                           "tools/api-tester/apis/backend.json", "package-lock.json", "web/package-lock.json"]
        r = sizecheck.check(spec, cfg=CFG)
        self.assertEqual(r["count"], 2)
        self.assertIn("5 generated excluded", r["line"])

    def test_tests_count(self):
        self.assertEqual(sizecheck.check(files(3) + ["tests/test_a.py"], cfg=CFG)["count"], 4)

    def test_warning_over_ten_only(self):
        self.assertIsNone(sizecheck.check(files(10), cfg=CFG)["warning"])
        self.assertIn("consider splitting", sizecheck.check(files(11), cfg=CFG)["warning"])

    def test_refuses_over_fifteen_without_reason(self):
        self.assertIsNotNone(sizecheck.check(files(15), cfg=CFG)["warning"])
        with self.assertRaises(AiError):
            sizecheck.check(files(16), cfg=CFG)
        with self.assertRaises(AiError):
            sizecheck.check(files(16), size_reason="  ", cfg=CFG)

    def test_size_reason_overrides_and_reaches_notes(self):
        r = sizecheck.check(files(16), "one rename touches every caller", cfg=CFG)
        self.assertIn("allowed because", r["warning"])
        self.assertIn("one rename", sizecheck.pr_note(r))
        self.assertIn("one rename", sizecheck.tracker_note(r))
        self.assertEqual(sizecheck.pr_note(sizecheck.check(files(2), cfg=CFG)), "")

    def test_estimate_is_printed(self):
        self.assertEqual(sizecheck.estimate_from("<p>Estimated files: 7</p>"), 7)
        self.assertIsNone(sizecheck.estimate_from("<p>nothing</p>"))
        self.assertIn("estimated 7", sizecheck.check(files(3), estimate=7, cfg=CFG)["line"])


if __name__ == "__main__":
    unittest.main()
