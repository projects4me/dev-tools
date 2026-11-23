"""Issue links in gaia-mcp: Gaia.links_of, blockers_in_project, link_issues/unlink and the 422 mapping, against a fake
Gaia HTTP layer, and the tools on top. Run: python -m unittest discover -s tests (from dev-tools/gaia-mcp)"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402

ISSUES = {
    "i-1": {"issueNumber": 1, "projectShortcode": "adt", "subject": "Me", "parentId": "i-9"},
    "i-2": {"issueNumber": 2, "projectShortcode": "adt", "subject": "Two", "status": "new"},
    "i-3": {"issueNumber": 3, "projectShortcode": "aprom", "subject": "Three", "status": "done"},
    "i-9": {"issueNumber": 9, "projectShortcode": "adt", "subject": "Parent", "status": "in_progress"},
    "i-c1": {"issueNumber": 11, "projectShortcode": "adt", "subject": "Child", "status": "new", "parentId": "i-1"},
}


def resource(type_, id_, attributes, **rels):
    out = {"type": type_, "id": id_, "attributes": attributes}
    if rels:
        out["relationships"] = {k: {"data": {"type": "issue", "id": v}} for k, v in rels.items()}
    return out


def link(id_, source, target, type_, **extra):
    return resource("issuelink", id_, dict({"type": type_}, **extra), sourceIssue=source, targetIssue=target)


def document(links):
    ids = {i for l in links for i in (l["relationships"]["sourceIssue"]["data"]["id"],
                                      l["relationships"]["targetIssue"]["data"]["id"])}
    return {"data": links, "included": [resource("issue", i, ISSUES[i]) for i in sorted(ids)]}


class FakeHttp:
    """Answers Gaia._http: issues by id/number/parent, issuelink reads and writes."""

    def __init__(self, links=(), post_reply=None):
        self.links, self.post_reply, self.calls = list(links), post_reply, []

    def __call__(self, method, path, params=None, body=None, **kw):
        self.calls.append((method, path, params, body))
        query = (params or {}).get("query") or ""
        if method == "GET" and path == "issue":
            if "Issue.issueNumber" in query:
                number = int(query.split(":")[1].strip(" )"))
                row = next((i, v) for i, v in ISSUES.items() if v["issueNumber"] == number)
                return 200, {"data": [resource("issue", row[0], dict(row[1], id=row[0]))]}
            if "Issue.id" in query:
                i = query.split(":")[1].strip(" )")
                return 200, {"data": [resource("issue", i, ISSUES[i])]}
            if "Issue.parentId" in query:
                return 200, {"data": [resource("issue", "i-c1", ISSUES["i-c1"])]}
        if method == "GET" and path == "issuelink":
            return 200, document(self.links)
        if method == "POST" and path == "issuelink":
            return self.post_reply or (201, {"data": {"type": "issuelink", "id": "l-new", "attributes": {}}})
        if method == "DELETE":
            return 204, {}
        return 404, {}


def gaia(http):
    g = server.Gaia()
    g._http = http
    return g


class LinksOfTest(unittest.TestCase):
    def links_of(self, *links):
        return gaia(FakeHttp(links)).links_of(1)

    def test_blocks_in_both_directions(self):
        out = self.links_of(link("l-1", "i-1", "i-2", "blocks"), link("l-2", "i-3", "i-1", "blocks"))
        self.assertEqual([e["issueNumber"] for e in out["blocks"]], [2])
        self.assertEqual([e["issueNumber"] for e in out["blockedBy"]], [3])
        self.assertEqual(out["blocks"][0], {"issueNumber": 2, "projectShortcode": "adt", "subject": "Two",
                                            "status": "new", "linkId": "l-1"})
        self.assertEqual(out["blockedBy"][0]["projectShortcode"], "aprom")

    def test_splits_in_both_directions(self):
        out = self.links_of(link("l-1", "i-1", "i-2", "splits"), link("l-2", "i-3", "i-1", "splits"))
        self.assertEqual([e["issueNumber"] for e in out["splitInto"]], [2])
        self.assertEqual([e["issueNumber"] for e in out["splitFrom"]], [3])

    def test_relates_counts_either_way(self):
        out = self.links_of(link("l-1", "i-1", "i-2", "relates"), link("l-2", "i-3", "i-1", "relates"))
        self.assertEqual(sorted(e["issueNumber"] for e in out["relates"]), [2, 3])
        self.assertEqual((out["blocks"], out["blockedBy"]), ([], []))

    def test_parent_and_children(self):
        out = self.links_of()
        self.assertEqual((out["parent"]["issueNumber"], out["parent"]["status"]), (9, "in_progress"))
        self.assertEqual([c["issueNumber"] for c in out["children"]], [11])

    def test_no_parent(self):
        http = FakeHttp()
        g = gaia(http)
        g.issue_by_number = lambda n, rels=None: {"id": "i-1", "issueNumber": 1}
        self.assertIsNone(g.links_of(1)["parent"])

    def test_one_issuelink_query_covers_both_ends(self):
        http = FakeHttp()
        gaia(http).links_of(1)
        reads = [c for c in http.calls if c[1] == "issuelink"]
        self.assertEqual(len(reads), 1)
        self.assertIn("sourceIssueId : i-1", reads[0][2]["query"])
        self.assertIn("targetIssueId : i-1", reads[0][2]["query"])
        self.assertEqual(reads[0][2]["rels"], "sourceIssue,targetIssue")

    def test_deleted_links_are_skipped(self):
        out = self.links_of(link("l-1", "i-1", "i-2", "blocks", deleted="1"))
        self.assertEqual(out["blocks"], [])


class BlockersTest(unittest.TestCase):
    def test_blockers_are_grouped_by_target_in_one_query(self):
        http = FakeHttp([link("l-1", "i-2", "i-1", "blocks"), link("l-2", "i-3", "i-1", "blocks"),
                         link("l-3", "i-9", "i-2", "blocks")])
        self.assertEqual(gaia(http).blockers_in_project("p-1"), {1: [2, 3], 2: [9]})
        self.assertEqual(len(http.calls), 1)
        query = http.calls[0][2]["query"]
        self.assertIn("Issuelink.type : blocks", query)
        self.assertIn("Issuelink.targetProjectId : p-1", query)


class LinkWriteTest(unittest.TestCase):
    def test_a_link_is_posted_with_the_issue_ids(self):
        http = FakeHttp()
        out = gaia(http).link_issues(2, 3, "blocks")
        post = next(c for c in http.calls if c[0] == "POST")
        self.assertEqual(post[1], "issuelink")
        self.assertEqual(post[3]["data"]["attributes"], {"sourceIssueId": "i-2", "targetIssueId": "i-3", "type": "blocks"})
        self.assertEqual(out["linkId"], "l-new")

    def test_an_unknown_type_is_refused_before_any_call(self):
        http = FakeHttp()
        with self.assertRaises(server.GaiaError):
            gaia(http).link_issues(2, 3, "duplicates")
        self.assertEqual(http.calls, [])

    def test_a_422_keeps_gaias_message_and_is_a_validation_error(self):
        http = FakeHttp(post_reply=(422, {"errors": [{"detail": "This link would make a cycle."}]}))
        with self.assertRaises(server.GaiaValidationError) as e:
            gaia(http).link_issues(2, 3, "blocks")
        self.assertEqual(str(e.exception), "This link would make a cycle.")
        self.assertEqual(e.exception.status, 422)

    def test_a_422_with_a_plain_message(self):
        http = FakeHttp(post_reply=(422, {"message": "Issues are in different workspaces."}))
        with self.assertRaises(server.GaiaValidationError) as e:
            gaia(http).link_issues(2, 3, "blocks")
        self.assertEqual(str(e.exception), "Issues are in different workspaces.")

    def test_other_failures_are_not_validation_errors(self):
        http = FakeHttp(post_reply=(500, {"errors": [{"detail": "boom"}]}))
        with self.assertRaises(server.GaiaError) as e:
            gaia(http).link_issues(2, 3, "blocks")
        self.assertNotIsInstance(e.exception, server.GaiaValidationError)

    def test_unlink_deletes_the_link(self):
        http = FakeHttp()
        gaia(http).unlink("l-1")
        self.assertEqual(http.calls[0][:2], ("DELETE", "issuelink/l-1"))


class LinkToolsTest(unittest.TestCase):
    def test_the_tools_are_registered(self):
        names = {t[0] for t in server.TOOLS}
        self.assertTrue({"gaia_link_issues", "gaia_unlink_issues"} <= names)

    def test_the_tools_call_the_client(self):
        calls = []

        class G:
            def link_issues(self, *a):
                calls.append(a)
                return {"linkId": "l-1"}

            def unlink(self, link_id):
                calls.append(link_id)
        tools = server.Tools(G())
        self.assertEqual(tools.link_issues({"source": 2, "target": 3, "type": "blocks"}), {"linkId": "l-1"})
        self.assertEqual(tools.unlink_issues({"link_id": "l-1"}), {"unlinked": "l-1"})
        self.assertEqual(calls, [(2, 3, "blocks"), "l-1"])


if __name__ == "__main__":
    unittest.main()
