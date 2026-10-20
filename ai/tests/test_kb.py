"""Unit tests for the issue knowledge base (kbstore, kbsync) against a fake Gaia. Run: python -m unittest discover -s tests"""

import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.kbstore import Store, fts_query  # noqa: E402
from lib.kbsync import PAGE, Sync  # noqa: E402


class FakeGaia:
    """Answers the queries kbsync makes, the way Gaia's Clause parser would."""

    def __init__(self):
        self.issues, self.comments, self.calls = [], [], []

    def issue(self, number, subject, modified, room=None, project="A Gaia", code="agaia", status="new",
              description="", parent=None):
        self.issues.append({"id": f"id-{number}", "issueNumber": number, "subject": subject, "dateModified": modified,
                            "conversationRoomId": room, "projectShortcode": code, "project": {"name": project},
                            "issuestatus": {"name": status}, "issuetype": {"name": "Task"},
                            "description": description, "parentId": parent and f"id-{parent}"})

    def comment(self, cid, room, body, modified):
        self.comments.append({"id": cid, "relatedId": room, "comment": f"<p>{body}</p>", "createdUserName": "Hammad",
                              "dateCreated": modified, "dateModified": modified, "relatedTo": "conversationrooms"})

    def find(self, module, query=None, rels=None, fields=None, sort=None, order=None, limit=None, page=None):
        self.calls.append((module, query))
        rows = self.issues if module == "issue" else self.comments
        rows = [r for r in rows if self._matches(r, query or "")]
        if module == "issue":
            rows = sorted(rows, key=lambda r: r["issueNumber"])
        if limit and limit > 0:
            start = ((page or 1) - 1) * limit
            rows = rows[start:start + limit]
        return [dict(r) for r in rows]

    @staticmethod
    def _matches(row, query):
        for field, op, value in re.findall(r"\(\w+\.(\w+) (>:|:|CONTAINS) ([^()]+)\)", query):
            have = row.get(field)
            if op == ">:" and not (have and have >= value):
                return False
            if op == ":" and have != value:
                return False
            if op == "CONTAINS" and have not in [v.strip("'") for v in value.split(",")]:
                return False
        return True


class KbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "kb.db")
        self.gaia = FakeGaia()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def sync(self, full=False):
        self.gaia.calls.clear()
        return Sync(self.gaia, self.store).run(full=full)

    def numbers(self, text, **kw):
        return [h["number"] for h in self.store.search(text, **kw)]

    def test_first_sync_indexes_issues_and_comments(self):
        g = self.gaia
        g.issue(1, "Email settings page", "2026-10-01 10:00:00", room="r1",
                description="<p>SMTP form</p><p>Complexity: simple</p>")
        g.issue(2, "Recurring issues", "2026-10-02 10:00:00", room="r2", code="aprom", project="A Prometheus")
        g.comment("c1", "r2", "Needs a cron in Gaia for the schedules", "2026-10-03 09:00:00")
        self.assertEqual(self.sync()["issues"], 2)
        self.assertEqual(self.numbers("schedules"), [2])           # found through its comment
        self.assertEqual(self.numbers("emails"), [1])              # stemmed
        one = self.store.issue(1)
        self.assertEqual((one["complexity"], one["description"]), ("simple", "SMTP form"))
        self.assertEqual(self.store.issue(2)["comments"][0]["body"], "Needs a cron in Gaia for the schedules")

    def test_incremental_sync_reads_changed_issues_and_issues_with_new_comments_only(self):
        g = self.gaia
        g.issue(1, "Alpha", "2026-10-01 10:00:00", room="r1")
        g.issue(2, "Beta", "2026-10-01 11:00:00", room="r2")
        g.issue(3, "Gamma", "2026-10-01 12:00:00", room="r3")
        self.sync()
        g.issues[0]["subject"], g.issues[0]["dateModified"] = "Alpha renamed", "2026-10-05 10:00:00"
        g.comment("c9", "r2", "zebra crossing idea", "2026-10-05 11:00:00")   # Beta itself is unchanged
        result = self.sync()
        self.assertEqual(self.numbers("zebra"), [2])
        self.assertEqual(self.store.issue(1)["subject"], "Alpha renamed")
        self.assertIn(("issue", "(Issue.dateModified >: 2026-10-01 12:00:00)"), self.gaia.calls)
        # Alpha (changed), Gamma (the cursor's own second, read again on purpose) and Beta (new comment)
        self.assertEqual((result["issues"], result["via_comments"]), (3, 1))

    def test_issue_queries_page_until_a_short_page(self):
        for n in range(1, PAGE + 6):
            self.gaia.issue(n, f"Issue {n}", "2026-10-01 10:00:00")
        self.assertEqual(self.sync()["issues"], PAGE + 5)
        self.assertEqual(sum(1 for m, _ in self.gaia.calls if m == "issue"), 2)

    def test_full_sync_drops_issues_that_are_gone(self):
        self.gaia.issue(1, "Keep", "2026-10-01 10:00:00")
        self.gaia.issue(2, "Removed", "2026-10-01 10:00:00")
        self.sync()
        del self.gaia.issues[1]
        self.sync(full=True)
        self.assertIsNone(self.store.issue(2))
        self.assertIsNotNone(self.store.issue(1))

    def test_failed_full_sync_leaves_the_index_as_it_was(self):
        self.gaia.issue(1, "Survivor", "2026-10-01 10:00:00", room="r1")
        self.sync()
        cursor = self.store.meta("issue_cursor")

        def down(*args, **kwargs):
            raise OSError("Gaia is down")
        self.gaia.find = down
        with self.assertRaises(OSError):
            self.sync(full=True)
        self.assertEqual(self.numbers("survivor"), [1])
        self.assertEqual(self.store.meta("issue_cursor"), cursor)

    def test_search_filters_and_parent_links(self):
        g = self.gaia
        g.issue(1, "Ideas module", "2026-10-01 10:00:00", status="done")
        g.issue(2, "Ideas page", "2026-10-01 10:00:00", code="aprom", project="A Prometheus", parent=1)
        self.sync()
        self.assertEqual(self.numbers("ideas", project="aprom"), [2])
        self.assertEqual(self.numbers("ideas", project="A Gaia", status="done"), [1])
        self.assertEqual(self.store.issue(2)["parent_number"], 1)
        self.assertEqual(self.store.issue(1)["children"], [2])

    def test_search_text_is_never_fts_syntax(self):
        self.assertEqual(fts_query('ideas AND "(NOT x'), '"ideas"* OR "AND" OR "NOT" OR "x"')
        self.assertIsNone(fts_query("()*"))
        self.assertEqual(self.store.search('"unbalanced (quote'), [])


if __name__ == "__main__":
    unittest.main()
