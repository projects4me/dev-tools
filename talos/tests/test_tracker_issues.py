"""Tracker.issues_in: one Gaia query per project over the matching milestones, finished statuses left out.
Run: python -m unittest discover -s tests"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.tracker import Tracker  # noqa: E402


class StubGaia:
    def __init__(self, milestones, rows=()):
        self.milestones, self.rows, self.finds = milestones, list(rows), []

    def project_options(self, project, needs):
        return {"id": "p1", "milestones": self.milestones}, {}

    def find(self, module, query, rels=None, limit=None):
        self.finds.append((module, query, rels, limit))
        return self.rows


def row(number, status, milestone):
    return {"issueNumber": str(number), "id": f"i{number}", "issuestatus": {"name": status},
            "issuemilestone": {"name": milestone}, "project": {"name": "A Gaia"}, "description": ""}


class IssuesInTest(unittest.TestCase):
    def tracker(self, gaia):
        return Tracker(SimpleNamespace(name="agamemnon"), gaia=gaia, env={})

    def test_one_query_over_the_matching_milestones_without_the_skipped_statuses(self):
        gaia = StubGaia([{"id": "m1", "name": "aga-v1"}, {"id": "m2", "name": "Claude-v1"},
                         {"id": "m3", "name": "AGA-Workspace"}], [row(5, "new", "aga-v1")])
        issues = self.tracker(gaia).issues_in("A Gaia", "aga-", skip_statuses=("done", "wont_fix"))
        self.assertEqual([(i["number"], i["status"], i["milestone"]) for i in issues], [(5, "new", "aga-v1")])
        self.assertEqual(len(gaia.finds), 1)
        module, query, rels, limit = gaia.finds[0]
        self.assertEqual((module, rels, limit), ("issue", Tracker.ISSUE_RELS, -1))
        self.assertEqual(query, "((Issue.projectId : p1) AND ((Issue.milestoneId : m1) OR (Issue.milestoneId : m3))"
                                " AND (issuestatus.name !: done) AND (issuestatus.name !: wont_fix))")

    def test_every_status_without_skips(self):
        gaia = StubGaia([{"id": "m1", "name": "aga-v1"}])
        self.tracker(gaia).issues_in("A Gaia", "aga-")
        self.assertEqual(gaia.finds[0][1], "((Issue.projectId : p1) AND ((Issue.milestoneId : m1)))")

    def test_no_query_without_a_matching_milestone(self):
        gaia = StubGaia([{"id": "m2", "name": "Claude-v1"}])
        self.assertEqual(self.tracker(gaia).issues_in("A Gaia", "aga-", skip_statuses=("done",)), [])
        self.assertEqual(gaia.finds, [])


if __name__ == "__main__":
    unittest.main()
