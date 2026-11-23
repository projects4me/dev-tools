"""Tracker.issues_in: one Gaia query per project over the matching milestones, finished statuses left out.
Run: python -m unittest discover -s tests"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.tracker import LinkRefused, Tracker  # noqa: E402


class StubGaia:
    def __init__(self, milestones, rows=(), blockers=None, links=None):
        self.milestones, self.rows, self.finds = milestones, list(rows), []
        self.blockers, self.links, self.blocker_calls = blockers or {}, links or {}, 0

    def blockers_in_project(self, project_id):
        self.blocker_calls += 1
        return self.blockers

    def links_of(self, number):
        return {"blockedBy": [{"issueNumber": n} for n in self.links.get(number, [])]}

    def issue_by_number(self, number, rels=None):
        return next(r for r in self.rows if int(r["issueNumber"]) == number)

    def project_options(self, project, needs):
        return {"id": "p1", "milestones": self.milestones}, {}

    def find(self, module, query, rels=None, limit=None):
        self.finds.append((module, query, rels, limit))
        return self.rows


def row(number, status, milestone, description=""):
    return {"issueNumber": str(number), "id": f"i{number}", "issuestatus": {"name": status},
            "issuemilestone": {"name": milestone}, "project": {"name": "A Gaia"}, "description": description}


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


class DependsOnTest(unittest.TestCase):
    LINE = "<p><strong>Depends on:</strong> adt-12, #11</p>"

    def issue(self, description, links):
        gaia = StubGaia([], [row(5, "new", "aga-v1", description)], links={5: links})
        return Tracker(SimpleNamespace(name="a"), gaia=gaia, env={}).issue(5)["dependsOn"]

    def test_links_only(self):
        self.assertEqual(self.issue("", [9, 7]), [7, 9])

    def test_lines_only(self):
        self.assertEqual(self.issue(self.LINE, []), [11, 12])

    def test_both_deduplicated(self):
        self.assertEqual(self.issue(self.LINE, [12, 3]), [3, 11, 12])

    def test_none(self):
        self.assertEqual(self.issue("", []), [])

    def test_issues_in_makes_one_link_query_for_the_scan(self):
        gaia = StubGaia([{"id": "m1", "name": "aga-v1"}],
                        [row(5, "new", "aga-v1", self.LINE), row(6, "new", "aga-v1")], blockers={5: [12, 2]})
        issues = Tracker(SimpleNamespace(name="a"), gaia=gaia, env={}).issues_in("A Gaia", "aga-")
        self.assertEqual([i["dependsOn"] for i in issues], [[2, 11, 12], []])
        self.assertEqual(gaia.blocker_calls, 1)


class LinkIssuesTest(unittest.TestCase):
    def test_a_refusal_is_its_own_error_with_gaias_message(self):
        import server as gaia_mcp

        class Refusing:
            def link_issues(self, *a):
                raise gaia_mcp.GaiaValidationError("Would create a cycle", 422)

        t = Tracker(SimpleNamespace(name="a"), gaia=Refusing(), env={})
        with self.assertRaises(LinkRefused) as c:
            t.link_issues(1, 2, "blocks")
        self.assertIn("cycle", str(c.exception))


if __name__ == "__main__":
    unittest.main()
