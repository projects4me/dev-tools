"""Unit tests for the issue links lines of `ai context` and `ai kb show`. Run: python -m unittest discover -s tests"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import issuelinks  # noqa: E402


def end(number, status="new", code="adt", subject="S"):
    return {"issueNumber": number, "projectShortcode": code, "status": status, "subject": subject}


def links(**groups):
    base = {"blocks": [], "blockedBy": [], "relates": [], "splitInto": [], "splitFrom": [], "parent": None,
            "children": []}
    base.update(groups)
    return base


class LinesTest(unittest.TestCase):
    def test_each_group_has_a_line(self):
        out = issuelinks.lines(links(blocks=[end(2)], blockedBy=[end(1)], relates=[end(3)], splitInto=[end(4)],
                                     splitFrom=[end(5)], parent=end(6), children=[end(7)]))
        self.assertEqual([l.split(":")[0] for l in out],
                         ["Blocked by", "Blocks", "Parent", "Children", "Split from", "Split into", "Relates"])
        self.assertIn("Parent: adt-6 [new] S", out)

    def test_open_blockers_first_and_marked(self):
        out = issuelinks.lines(links(blockedBy=[end(1, "done"), end(2, "in_progress")]))
        self.assertEqual(out, ["Blocked by: ! adt-2 [in_progress] S; adt-1 [done] S"])

    def test_legacy_line_joins_blocked_by(self):
        out = issuelinks.lines(links(), legacy=[4846])
        self.assertEqual(out, ["Blocked by: #4846 (line)"])

    def test_cap_at_ten(self):
        out = issuelinks.lines(links(children=[end(n) for n in range(1, 14)]))
        self.assertTrue(out[0].endswith("… 3 more"))
        self.assertEqual(out[0].count("adt-"), 10)

    def test_legacy_blockers_skip_self_and_real_links(self):
        text = "Depends on #4846, blocked by #4850 and after #4801"
        self.assertEqual(issuelinks.legacy_blockers(text, 4850, {4801}), [4846])


class ReportTest(unittest.TestCase):
    def test_none(self):
        self.assertEqual(issuelinks.report(1, fetcher=lambda n: (links(), None)), ["no links"])

    def test_unreachable_is_a_message(self):
        out = issuelinks.report(1, fetcher=lambda n: (None, "connection refused"))
        self.assertEqual(out, ["links unavailable: connection refused"])

    def test_fetch_swallows_errors(self):
        original = issuelinks.tracker
        issuelinks.tracker = lambda: (_ for _ in ()).throw(RuntimeError("no env"))
        try:
            self.assertEqual(issuelinks.fetch(1), (None, "no env"))
        finally:
            issuelinks.tracker = original


if __name__ == "__main__":
    unittest.main()
