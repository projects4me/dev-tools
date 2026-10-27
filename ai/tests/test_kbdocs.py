"""Unit tests for the Gaia docs part of the knowledge base (kbdocs + kbstore) on a fixture docs tree.
Run: python -m unittest discover -s tests"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import kbdocs  # noqa: E402
from lib.kbstore import Store  # noqa: E402

MODEL_DOC = """# Model Issue

> Generated. Do not edit.

<a id="fields"></a>
## Fields

- subject varchar(255) required

<a id="relationships"></a>
## Relationships

- project belongsTo Project

```
# not a heading
```
"""

BUNDLES = """# Gaia change bundles

## A. api-tester cases and seed

**Exemplar:** #193 (full set for 4 resources). #179 for an ACL-only rule.

## G. Framework or core change

**Exemplars:**
- #179 (an ACL rule with `AclSystemRecordTest`)
- #186 (router)

**Touchpoints, in order:**

1. The core file.
"""

GRAPH = {"nodes": [
    {"id": "model:Issue", "type": "model", "key": "Issue", "attributes": {"tableName": "issues", "acl": None}},
    {"id": "route:GET /api/v1/issue", "type": "route", "key": "GET /api/v1/issue",
     "attributes": {"type": "rest", "path": "/api/v1/issue"}},
    {"id": "action:issue.list", "type": "action", "key": "issue.list", "attributes": {}},
]}


def build_tree(root):
    for rel, text in (("docs/generated/models/Issue.md", MODEL_DOC), ("docs/dev/bundles.md", BUNDLES),
                      ("docs/generated/graph.json", json.dumps(GRAPH)),
                      ("docs/architecture/rbac/decisions.md", "# Decisions\n\n" + "\n".join(f"line {i}" for i in range(95)))):
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")


class KbDocsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "gaia"
        build_tree(self.root)
        self.store = Store(Path(self.tmp.name) / "kb.db")
        self.addCleanup(self.store.close)

    def test_sections_use_the_anchor_line_else_the_slug_and_ignore_fenced_headings(self):
        keys = {r["key"] for r in kbdocs.collect(self.root)}
        self.assertIn("docs/generated/models/Issue.md#fields", keys)
        self.assertIn("docs/generated/models/Issue.md#relationships", keys)
        self.assertIn("docs/dev/bundles.md#a-api-tester-cases-and-seed", keys)
        self.assertFalse([k for k in keys if "not-a-heading" in k])

    def test_long_sections_are_split_into_parts_of_at_most_40_lines(self):
        parts = [r for r in kbdocs.collect(self.root) if r["file"].endswith("rbac/decisions.md")]
        self.assertGreater(len(parts), 2)
        self.assertTrue(all(r["size"] <= kbdocs.SECTION_LINES for r in parts))
        self.assertIn("docs/architecture/rbac/decisions.md#decisions~2", {r["key"] for r in parts})

    def test_graph_nodes_cover_the_wanted_types_only(self):
        nodes = {r["key"]: r for r in kbdocs.collect(self.root) if r["kind"] == "node"}
        self.assertEqual({"docs/generated/graph.json#model:Issue",
                          "docs/generated/graph.json#route:GET /api/v1/issue"}, set(nodes))
        self.assertIn("tableName: issues", nodes["docs/generated/graph.json#model:Issue"]["body"])
        self.assertNotIn("acl:", nodes["docs/generated/graph.json#model:Issue"]["body"])

    def test_exemplar_prs_are_merged_across_bundles(self):
        prs = {r["key"]: r["body"] for r in kbdocs.collect(self.root) if r["kind"] == "pr"}
        self.assertEqual({f"docs/dev/bundles.md#pr-{n}" for n in (179, 186, 193)}, set(prs))
        self.assertIn("A. api-tester cases and seed", prs["docs/dev/bundles.md#pr-179"])
        self.assertIn("G. Framework or core change", prs["docs/dev/bundles.md#pr-179"])
        self.assertIn("router", prs["docs/dev/bundles.md#pr-186"])

    def test_sync_is_idempotent_and_incremental(self):
        first = kbdocs.sync(self.store, self.root)
        self.assertGreater(first[0], 0)
        self.assertEqual((0, 0, 0), kbdocs.sync(self.store, self.root)[:3])
        before = self.store.doc_count()

        (self.root / "docs/generated/models/Issue.md").write_text(MODEL_DOC.replace("varchar(255)", "text"),
                                                                    encoding="utf-8")
        (self.root / "docs/architecture/rbac/decisions.md").unlink()
        added, updated, removed, _ = kbdocs.sync(self.store, self.root)
        self.assertEqual((0, 1), (added, updated))
        self.assertGreater(removed, 0)
        self.assertLess(self.store.doc_count(), before)
        self.assertIsNone(self.store.doc("docs/architecture/rbac/decisions.md#decisions"))

    def test_search_filters_by_kind_and_section_returns_the_body(self):
        kbdocs.sync(self.store, self.root)
        hits = self.store.search_docs("subject")
        self.assertEqual("docs/generated/models/Issue.md#fields", hits[0]["key"])
        self.assertEqual(["pr"], {h["kind"] for h in self.store.search_docs("router", kind="pr")} and ["pr"])
        self.assertEqual([], self.store.search_docs("subject", kind="pr"))
        self.assertEqual(["node"], list({h["kind"] for h in self.store.search_docs("tableName issues", kind="node")}))
        self.assertIn("subject varchar", self.store.doc("docs/generated/models/Issue.md#fields")["body"])

    def test_empty_checkout_yields_no_records(self):
        self.assertEqual([], kbdocs.collect(Path(self.tmp.name) / "nothing"))


if __name__ == "__main__":
    unittest.main()
