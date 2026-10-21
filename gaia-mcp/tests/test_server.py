"""gaia-mcp tools against a fake Gaia client: milestones (created when missing) and create_issue's milestone choice.
Run: python -m unittest discover -s tests (from dev-tools/gaia-mcp)"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


class FakeGaia:
    def __init__(self, milestones=("Agents",)):
        self.milestones = [{"id": f"m-{n}", "name": n} for n in milestones]
        self.posts = []

    def me(self):
        return {"id": "u-agent", "name": "Agent Nestor"}

    def project(self, name, rels=None):
        return {"id": "p-1", "name": "A Dev Tool", "shortCode": "adt", "milestones": list(self.milestones),
                "issuetypes": [{"id": "t-task", "name": "Task"}], "members": [{"id": "1", "name": "Hammad Hassan"}]}

    def project_options(self, project):
        return self.project(project), [{"id": "s-new", "name": "new"}]

    pick = staticmethod(server.Gaia.pick)

    def member(self, project, who):
        return server.Gaia.member(self, project, who)

    def request(self, method, path, params=None, body=None):
        self.posts.append((method, path, body))
        attrs = body["data"]["attributes"]
        if path == "milestone":
            self.milestones.append({"id": f"m-{attrs['name']}", "name": attrs["name"]})
            return {"data": {"type": "milestone", "id": f"m-{attrs['name']}", "attributes": attrs}}
        return {"data": {"type": "issue", "id": "i-1", "attributes": dict(attrs, issueNumber=4100)}}

    flatten = staticmethod(server.Gaia.flatten)

    def issue_by_number(self, number, rels=None):
        return {"issueNumber": number, "subject": "x", "projectShortcode": "adt"}


class MilestoneTest(unittest.TestCase):
    def setUp(self):
        self.g = FakeGaia()
        self.tools = server.Tools(self.g)

    def issue_post(self):
        return next(b["data"]["attributes"] for m, path, b in self.g.posts if path == "issue")

    def test_create_issue_creates_the_missing_default_milestone(self):
        out = self.tools.create_issue({"project": "adt", "subject": "S", "complexity": "simple"})
        self.assertEqual(out["milestoneCreated"], "Claude-v1")
        created = next(b["data"]["attributes"] for m, path, b in self.g.posts if path == "milestone")
        self.assertEqual((created["name"], created["status"], created["projectId"]), ("Claude-v1", "in_progress", "p-1"))
        self.assertEqual(self.issue_post()["milestoneId"], "m-Claude-v1")

    def test_an_existing_milestone_is_used_and_named_ones_are_created_too(self):
        out = self.tools.create_issue({"project": "adt", "subject": "S", "complexity": "simple", "milestone": "agents"})
        self.assertNotIn("milestoneCreated", out)
        self.assertEqual(self.issue_post()["milestoneId"], "m-Agents")
        self.g.posts.clear()
        out = self.tools.create_issue({"project": "adt", "subject": "S", "complexity": "simple", "milestone": "v2"})
        self.assertEqual(out["milestoneCreated"], "v2")

    def test_an_empty_milestone_means_the_backlog(self):
        self.tools.create_issue({"project": "adt", "subject": "S", "complexity": "simple", "milestone": ""})
        self.assertEqual(self.issue_post()["milestoneId"], "")
        self.assertFalse([p for p in self.g.posts if p[1] == "milestone"])

    def test_create_milestone_is_idempotent(self):
        first = self.tools.create_milestone({"project": "adt", "name": "Claude-v1"})
        again = self.tools.create_milestone({"project": "adt", "name": "claude-v1"})
        self.assertEqual((first["created"], again["created"]), (True, False))
        self.assertEqual(again["milestone"]["id"], first["milestone"]["id"])
        self.assertIn("gaia_create_milestone", json.dumps([t[0] for t in server.TOOLS]))


if __name__ == "__main__":
    unittest.main()
