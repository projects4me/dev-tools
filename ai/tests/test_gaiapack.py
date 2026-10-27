"""Unit tests for the Gaia context pack. Run: python -m unittest discover -s tests"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import gaiapack  # noqa: E402


def fixture_root():
    root = Path(tempfile.mkdtemp())
    (root / "docs/generated/bundles").mkdir(parents=True)
    (root / gaiapack.GRAPH).write_text(json.dumps({"nodes": [
        {"type": "model", "key": "Idea"}, {"type": "model", "key": "Issue"}]}), encoding="utf-8")
    (root / "docs/generated/bundles/A.md").write_text(
        '<a id="exemplar"></a>\n## Exemplar\n\n[#193](https://github.com/projects4me/gaia/pull/193) (Idea)\n',
        encoding="utf-8")
    return root


def fake_run(root, args, timeout=0):
    return 0, "\n".join(f"{args[0]} line {i}" for i in range(3))


def no_sections(text):
    return [("docs/generated/acl-matrix.md#idea", 12, "Idea ACL")]


class PackTest(unittest.TestCase):
    def setUp(self):
        self.root = fixture_root()

    def test_models_named_by_word_or_endpoint(self):
        graph = gaiapack.load_graph(self.root)
        self.assertEqual(gaiapack.named_models("Add cases for Idea", graph), ["Idea"])
        self.assertEqual(gaiapack.named_models("GET /api/v1/issue", graph, {"issue"}), ["Issue"])
        self.assertEqual(gaiapack.named_models("an idea of mine", graph), [])

    def test_bundle_by_words(self):
        self.assertEqual(gaiapack.pick_bundle("add acl cases to acl.json and the seed"), "A")
        self.assertEqual(gaiapack.pick_bundle("new live events over hermes"), "E")
        self.assertIsNone(gaiapack.pick_bundle("nothing relevant"))

    def test_exemplar_and_checks(self):
        self.assertEqual(gaiapack.exemplar(self.root, "A"), "#193 https://github.com/projects4me/gaia/pull/193")
        self.assertIsNone(gaiapack.exemplar(self.root, "Z"))
        self.assertEqual(gaiapack.check_commands("B", ["Idea"]),
                         ["graph export --check", "docs generate --check", "check resource Idea", "check api-cases"])

    def test_build_has_each_part(self):
        out = "\n".join(gaiapack.build(self.root, "Idea acl cases for acl.json", set(), fake_run, no_sections, 300))
        for part in ("gaia-dev model Idea", "gaia-dev acl Idea", "gaia-dev cases Idea", "bundle A",
                     "exemplar PR: #193", "acl-matrix.md#idea", "gaia-dev check api-cases"):
            self.assertIn(part, out)

    def test_cap_defers_blocks_but_keeps_checks_reference(self):
        lines = gaiapack.build(self.root, "Idea acl cases for acl.json", set(), fake_run, no_sections, 8)
        self.assertLessEqual(len([l for l in lines if l.startswith("  ")]), 8)
        self.assertIn("over the pack cap", "\n".join(lines))

    def test_unavailable_gaia_dev_is_reported_not_raised(self):
        out = "\n".join(gaiapack.build(self.root, "Idea", set(), lambda r, a, timeout=0: (None, "no php"),
                                       no_sections, 300))
        self.assertIn("(unavailable: no php)", out)

    def test_run_checks_skips_when_unavailable(self):
        results = gaiapack.run_checks("gaia", run=lambda r, a, timeout=0: (None, "no php"))
        self.assertTrue(all(ok is None for _, ok, _ in results))


if __name__ == "__main__":
    unittest.main()
