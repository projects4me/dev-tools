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


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


class IdeaGaia:
    """A fake Gaia for the idea tools: records requests, answers finds from `rows`."""
    email = "nestor@example.com"

    def __init__(self):
        self.rows = {}
        self.requests = []
        self.uploads = []
        self.issue = {"id": "i-1", "issueNumber": 4100, "projectId": "p-1"}

    flatten = staticmethod(server.Gaia.flatten)

    def project(self, name, rels=None):
        return {"id": "p-1", "name": "A Dev Tool", "shortCode": "adt"}

    def find(self, module, query=None, rels=None, **kw):
        return list(self.rows.get(module, []))

    def issue_by_number(self, number, rels=None):
        return dict(self.issue, issueNumber=number)

    def request(self, method, path, params=None, body=None):
        self.requests.append((method, path, body))
        if method == "POST":
            return {"data": {"type": path, "id": f"{path}-1", "attributes": body["data"]["attributes"]}}
        return {}

    def upload(self, name, content, fields):
        self.uploads.append((fields, content, name))
        return {"data": {"type": "upload", "id": "up-1", "attributes": {"name": name}}}


class IdeaToolsTest(unittest.TestCase):
    def setUp(self):
        self.g = IdeaGaia()
        self.g.rows["idea"] = [{"id": "idea-1", "title": "T", "status": "open", "projectId": "p-1",
                                "project": {"id": "p-1", "shortCode": "adt"},
                                "messages": [{"id": "m2", "dateCreated": "2"}, {"id": "m1", "dateCreated": "1"}],
                                "plans": [{"id": "pl2", "version": 2}, {"id": "pl1", "version": 1}]}]
        self.tools = server.Tools(self.g)

    def posted(self, path):
        return next(b["data"]["attributes"] for m, p, b in self.g.requests if p == path)

    def test_get_idea_returns_ordered_messages_plans_and_plan_issues(self):
        self.g.rows["ideaplanissue"] = [{"id": "x", "ideaplanId": "pl1", "key": "a", "issueId": "i-1",
                                         "issue": {"id": "i-1", "issueNumber": 4100, "projectShortcode": "adt"}}]
        self.g.rows["idea"][0]["createdUser"] = "1"
        out = self.tools.get_idea({"idea": "idea-1"})
        self.assertEqual(out["createdUser"], "1")
        self.assertEqual(out["projectShortCode"], "adt")
        self.assertEqual([m["id"] for m in out["messages"]], ["m1", "m2"])
        self.assertEqual([p["id"] for p in out["plans"]], ["pl1", "pl2"])
        self.assertEqual((out["planIssues"][0]["issueNumber"], out["planIssues"][0]["projectShortcode"]), (4100, "adt"))

    def test_get_idea_not_found_is_a_gaia_error(self):
        self.g.rows["idea"] = []
        with self.assertRaises(server.GaiaError):
            self.tools.get_idea({"idea": "nope"})

    def test_list_ideas(self):
        out = self.tools.list_ideas({"project": "adt", "status": "open"})
        self.assertEqual((out["count"], out["ideas"][0]["id"]), (1, "idea-1"))

    def test_add_message_encodes_json_columns(self):
        out = self.tools.add_idea_message({"idea": "idea-1", "body": " hi ", "kind": "progress", "costUsd": 0.5,
                                           "steps": ["a"], "screens": [{"uploadId": "up-1"}], "ideaplanId": "pl1"})
        a = self.posted("ideamessage")
        self.assertEqual((out["id"], a["body"], a["kind"], a["costUsd"]), ("ideamessage-1", "hi", "progress", 0.5))
        self.assertEqual((a["steps"], json.loads(a["screens"])), ('["a"]', [{"uploadId": "up-1"}]))
        with self.assertRaises(server.GaiaError):
            self.tools.add_idea_message({"idea": "idea-1", "body": " "})

    def test_add_plan_returns_the_version(self):
        self.g.rows["ideaplan"] = [{"id": "ideaplan-1", "version": 3, "status": "proposed"}]
        out = self.tools.add_idea_plan({"idea": "idea-1", "body": {"issues": []}})
        self.assertEqual(self.posted("ideaplan")["body"], '{"issues": []}')
        self.assertEqual((out["id"], out["version"]), ("ideaplan-1", 3))

    def test_update_plan_needs_a_change(self):
        self.tools.update_idea_plan({"plan": "pl1", "status": "failed", "error": "boom"})
        method, path, body = self.g.requests[-1]
        self.assertEqual((method, path, body["data"]["attributes"]), ("PATCH", "ideaplan/pl1",
                                                                      {"status": "failed", "error": "boom"}))
        with self.assertRaises(server.GaiaError):
            self.tools.update_idea_plan({"plan": "pl1"})

    def test_add_plan_issue_resolves_the_issue_and_checks_the_project(self):
        self.g.rows["ideaplan"] = [{"id": "pl1", "ideaId": "idea-1"}]
        out = self.tools.add_idea_plan_issue({"plan": "pl1", "key": "k", "issue_number": 4100, "project": "adt"})
        a = self.posted("ideaplanissue")
        self.assertEqual((a["ideaId"], a["ideaplanId"], a["issueId"], out["issueNumber"]), ("idea-1", "pl1", "i-1", 4100))
        self.g.issue["projectId"] = "p-other"
        with self.assertRaises(server.GaiaError):
            self.tools.add_idea_plan_issue({"plan": "pl1", "key": "k", "issue_number": 4100, "project": "adt"})

    def test_upload_screen_from_base64_and_path(self):
        import base64
        import tempfile
        out = self.tools.upload_idea_screen({"idea": "idea-1", "key": "home page", "png_base64": base64.b64encode(PNG).decode()})
        fields, content, name = self.g.uploads[-1]
        self.assertEqual((out["uploadId"], name, content, fields), ("up-1", "home_page.png", PNG,
                                                                    {"relatedId": "idea-1", "relatedTo": "idea"}))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.png"
            path.write_bytes(PNG)
            self.tools.upload_idea_screen({"idea": "idea-1", "key": "s.png", "path": str(path)})
            self.assertEqual(self.g.uploads[-1][2], "s.png")
        with self.assertRaises(server.GaiaError):
            self.tools.upload_idea_screen({"idea": "idea-1", "key": "k", "png_base64": base64.b64encode(b"nope").decode()})

    def test_tools_are_registered(self):
        names = {t[0] for t in server.TOOLS}
        for n in ("get_idea", "list_ideas", "add_idea_message", "add_idea_plan", "update_idea_plan",
                  "add_idea_plan_issue", "upload_idea_screen"):
            self.assertIn(f"gaia_{n}", names)
            self.assertTrue(callable(getattr(server.Tools, n)))


if __name__ == "__main__":
    unittest.main()
