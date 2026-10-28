"""gaia-mcp tools against a fake Gaia client: milestones (created when missing) and create_issue's milestone choice.
Run: python -m unittest discover -s tests (from dev-tools/gaia-mcp)"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


class FakeGaia:
    # issue types after agaia-4152: the name is a value, the en-us label comes from GET issuetype
    TYPES = [{"id": "t-task", "name": "task"}, {"id": "t-sub", "name": "sub_task"}, {"id": "t-cr", "name": "change_request"}]
    LABELS = {"task": "Task", "sub_task": "Sub Task", "change_request": "Change Request"}

    def __init__(self, milestones=("Agents",), types=TYPES, labels=LABELS):
        self.milestones = [{"id": f"m-{n}", "name": n} for n in milestones]
        self.types, self.labels = list(types), dict(labels)
        self.posts = []

    def type_labels(self):
        return self.labels

    def me(self):
        return {"id": "u-agent", "name": "Agent Nestor"}

    def project(self, name, rels=None):
        return {"id": "p-1", "name": "A Dev Tool", "shortCode": "adt", "milestones": list(self.milestones),
                "issuetypes": list(self.types), "members": [{"id": "1", "name": "Hammad Hassan"}]}

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
        return {"issueNumber": number, "subject": "x", "projectShortcode": "adt", "id": "i-1", "projectId": "p-1",
                "issuetype": {"id": "t-sub", "name": "sub_task"}}


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


class IssueGaia:
    """A fake Gaia for get_issue: comments and time logs come only from their own queries, newest first."""

    def __init__(self, comments=12):
        # what real Gaia does with hasMany rels on a limit=1 read: the related records are cut short
        self.issue = {"id": "i-1", "issueNumber": 4075, "subject": "S", "conversationRoomId": "room-1",
                      "comments": [{"dateCreated": "2026-10-01 00:00:00", "comment": "c00"}] * 2}
        self.comments = [{"dateCreated": f"2026-10-{n + 1:02d} 00:00:00", "createdUserName": "U", "comment": f"c{n:02d}"}
                         for n in range(comments)]
        self.timelogs = [{"context": "spent", "hours": 1, "spentOn": "2026-10-02 00:00:00"},
                         {"context": "spent", "minutes": 30, "spentOn": "2026-10-03 00:00:00"},
                         {"context": "est", "days": 1, "dateCreated": "2026-10-01 00:00:00"}]
        self.issue_rels, self.finds = None, []

    def type_labels(self):
        return {}

    def issue_by_number(self, number, rels=None):
        self.issue_rels = rels
        return dict(self.issue)

    def find(self, module, query=None, rels=None, fields=None, sort=None, order=None, limit=None, page=None):
        self.finds.append({"module": module, "query": query, "sort": sort, "order": order, "limit": limit})
        rows = {"comment": sorted(self.comments, key=lambda c: c["dateCreated"], reverse=True),
                "timelog": self.timelogs}[module]
        return rows[:limit] if limit and limit > 0 else rows


class GetIssueTest(unittest.TestCase):
    def setUp(self):
        self.g = IssueGaia()
        self.tools = server.Tools(self.g)

    def test_the_issue_read_asks_for_no_has_many_rels(self):
        self.tools.get_issue({"issue_number": 4075})
        self.assertFalse({"comments", "spent", "estimated"} & set((self.g.issue_rels or "").split(",")))

    def test_the_latest_ten_comments_come_from_their_own_query_oldest_first(self):
        out = self.tools.get_issue({"issue_number": 4075})
        self.assertEqual([c["comment"] for c in out["comments"]], [f"c{n:02d}" for n in range(2, 12)])
        self.assertTrue(out["olderComments"])
        q = next(f for f in self.g.finds if f["module"] == "comment")
        self.assertIn("room-1", q["query"])
        self.assertEqual((q["sort"], q["order"]), ("Comment.dateCreated", "DESC"))

    def test_more_comments_can_be_asked_for_up_to_the_cap(self):
        out = self.tools.get_issue({"issue_number": 4075, "comments": 50})
        self.assertEqual(len(out["comments"]), 12)
        self.assertFalse(out["olderComments"])
        self.tools.get_issue({"issue_number": 4075, "comments": 10000})
        self.assertEqual(self.g.finds[-2]["limit"], server.Tools.COMMENTS_MAX + 1)

    def test_an_issue_without_a_conversation_room_has_no_comments(self):
        self.g.issue["conversationRoomId"] = None
        out = self.tools.get_issue({"issue_number": 4075})
        self.assertEqual((out["comments"], out["olderComments"]), ([], False))
        self.assertFalse([f for f in self.g.finds if f["module"] == "comment"])

    def test_spent_and_estimated_come_from_one_time_log_query(self):
        out = self.tools.get_issue({"issue_number": 4075})
        self.assertEqual((out["spent"]["total"], len(out["spent"]["entries"])), ("1h 30m", 2))
        self.assertEqual((out["estimated"]["total"], len(out["estimated"]["entries"])), ("1d", 1))
        q = next(f for f in self.g.finds if f["module"] == "timelog")
        self.assertIn("i-1", q["query"])
        self.assertGreater(q["limit"], 0)


class IssueTypeTest(unittest.TestCase):
    def setUp(self):
        self.g = FakeGaia()
        self.tools = server.Tools(self.g)

    def type_id(self, type_arg=None, **kw):
        args = dict({"project": "adt", "subject": "S", "complexity": "simple"}, **kw)
        if type_arg is not None:
            args["type"] = type_arg
        self.g.posts.clear()
        self.tools.create_issue(args)
        return next(b["data"]["attributes"] for m, path, b in self.g.posts if path == "issue")["typeId"]

    def test_a_type_is_found_by_value_or_en_us_label_in_any_case(self):
        for arg in ("Sub Task", "sub task", "sub_task", "SUB_TASK", "t-sub"):
            self.assertEqual(self.type_id(arg), "t-sub", arg)
        self.assertEqual(self.type_id("change request"), "t-cr")

    def test_the_default_type_is_task(self):
        self.assertEqual(self.type_id(), "t-task")

    def test_types_without_labels_still_match_their_name(self):
        # before the migration: names are display text and Gaia sends no translations
        self.g = FakeGaia(types=[{"id": "t-task", "name": "Task"}, {"id": "t-sub", "name": "Sub Task"}], labels={})
        self.tools = server.Tools(self.g)
        self.assertEqual(self.type_id(), "t-task")
        self.assertEqual(self.type_id("sub task"), "t-sub")

    def test_an_unknown_type_lists_the_labels(self):
        with self.assertRaises(server.GaiaError) as e:
            self.type_id("Story")
        self.assertIn("Options: Change Request, Sub Task, Task.", str(e.exception))

    def test_update_issue_matches_the_label(self):
        out = self.tools.update_issue({"issue_number": 4100, "type": "Change Request"})
        patched = next(b["data"]["attributes"] for m, path, b in self.g.posts if m == "PATCH")
        self.assertEqual(patched["typeId"], "t-cr")
        self.assertEqual(out["updated"]["type"], "Sub Task")

    def test_project_options_lists_type_labels(self):
        self.assertEqual(self.tools.project_options({"project": "adt"})["types"], ["Change Request", "Sub Task", "Task"])

    def test_issue_output_shows_the_label_and_falls_back_to_the_value(self):
        issue = {"issuetype": {"name": "sub_task"}}
        self.assertEqual(server.summarize_issue(issue, type_labels={"sub_task": "Sub Task"})["type"], "Sub Task")
        self.assertEqual(server.summarize_issue(issue, type_labels={})["type"], "sub_task")
        self.assertNotIn("type", server.summarize_issue({}))


class TypeLabelsTest(unittest.TestCase):
    def client(self, find):
        g = server.Gaia()
        g.find = find
        return g

    def test_labels_come_from_one_issuetype_read_and_are_cached(self):
        calls = []

        def find(module, **kw):
            calls.append((module, kw.get("limit")))
            return [{"name": "sub_task", "translations": {"en-us": "Sub Task", "de-de": "Unteraufgabe"}},
                    {"name": "bug", "translations": {}}, {"name": "task"}]
        g = self.client(find)
        self.assertEqual(g.type_labels(), {"sub_task": "Sub Task"})
        g.type_labels()
        self.assertEqual(calls, [("issuetype", -1)])

    def test_a_failed_read_means_no_labels(self):
        def find(module, **kw):
            raise server.GaiaError("Gaia GET issuetype returned 403")
        self.assertEqual(self.client(find).type_labels(), {})


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
                                # what real Gaia does with rels on a limit=1 read: the related records are cut short
                                "messages": [{"id": "m1", "dateCreated": "1"}], "plans": []}]
        self.g.rows["ideamessage"] = [{"id": "m2", "dateCreated": "2"}, {"id": "m3", "dateCreated": "3"},
                                      {"id": "m1", "dateCreated": "1"}]
        self.g.rows["ideaplan"] = [{"id": "pl2", "version": 2}, {"id": "pl1", "version": 1}]
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
        self.assertEqual([m["id"] for m in out["messages"]], ["m1", "m2", "m3"])   # the whole thread
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
