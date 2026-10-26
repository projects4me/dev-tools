"""Unit tests for dev-tools/ai's deterministic helpers. Run: python -m unittest discover -s tests"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import context, review, ship, testmap, tests  # noqa: E402
from lib.common import AiError  # noqa: E402

MODULES = {
    "tests/acceptance/settings/email.feature": "Feature: Settings | email",
    "tests/acceptance/settings/issue-types.feature": "Feature: Settings | issue types",
    "tests/integration/components/role/card-test.js": "Integration | Component | role/card",
    "tests/unit/services/acl-test.js": "Unit | Service | acl",
}


class PrometheusMappingTest(unittest.TestCase):
    def map(self, *changed):
        return testmap.prometheus_tests(list(changed), MODULES)

    def test_route_files_map_to_their_feature(self):
        for f in ("app/controllers/app/settings/email.js", "app/templates/app/settings/email.hbs",
                  "app/routes/app/settings/email.js"):
            self.assertEqual(self.map(f), (["tests/acceptance/settings/email.feature"], []), f)

    def test_steps_map_to_the_feature_of_the_same_name(self):
        tests, _ = self.map("tests/acceptance/steps/settings/email-steps.js")
        self.assertEqual(tests, ["tests/acceptance/settings/email.feature"])

    def test_component_template_and_class_map_to_the_integration_test(self):
        for f in ("app/components/role/card.js", "app/templates/components/role/card.hbs"):
            self.assertEqual(self.map(f)[0], ["tests/integration/components/role/card-test.js"], f)

    def test_service_maps_to_unit_test(self):
        self.assertEqual(self.map("app/services/acl.js")[0], ["tests/unit/services/acl-test.js"])

    def test_unmappable_files_are_reported(self):
        tests, unmapped = self.map("translations/en-us.json", "app/styles/app.scss")
        self.assertEqual(tests, [])
        self.assertEqual(unmapped, ["translations/en-us.json", "app/styles/app.scss"])

    def test_filter_is_a_case_insensitive_regex_of_escaped_module_names(self):
        flt = testmap.qunit_filter(["tests/acceptance/settings/email.feature",
                                    "tests/integration/components/role/card-test.js"], MODULES)
        self.assertEqual(flt, r"/(Feature: Settings \| email|Integration \| Component \| role\/card)/i")
        self.assertIsNone(testmap.qunit_filter([], MODULES))


class GaiaMappingTest(unittest.TestCase):
    def test_metadata_or_routes_change_makes_backend_catalog_stale(self):
        self.assertTrue(testmap.backend_catalog_stale(["app/metadata/model/Idea.php"]))
        self.assertTrue(testmap.backend_catalog_stale(["core/config/routes.php"]))
        self.assertFalse(testmap.backend_catalog_stale(["app/api/v1/models/Idea.php"]))

    def test_controllers_and_models_map_to_resource_terms(self):
        flt, terms, unmapped = testmap.gaia_api_filter(".", "origin/dev", [
            "app/api/v1/controllers/SettingController.php", "app/metadata/model/Issue.php",
            "tests/libraries/utils/X.php", "core/libs/utils/SmtpSettings.php"])
        self.assertEqual(terms, ["issue", "setting"])
        self.assertEqual(flt, "issue|setting")
        self.assertEqual(unmapped, ["core/libs/utils/SmtpSettings.php"])


class FoldTest(unittest.TestCase):
    BODY = ("**Major**\n<details>\n<summary>🧩 Analysis chain</summary>\nscript output\n</details>\n"
            "<!-- cr-marker -->\nThe finding text.\n"
            "<details><summary>🧹 Nitpick comments (1)</summary>keep this nit</details>\n"
            "<details>\n<summary>🤖 Prompt for AI Agents</summary>\nrestated\n</details>")

    def test_folds_boilerplate_and_markers_but_keeps_findings(self):
        out = review.fold(self.BODY)
        self.assertIn("The finding text.", out)
        self.assertIn("keep this nit", out)
        self.assertNotIn("script output", out)
        self.assertNotIn("restated", out)
        self.assertNotIn("cr-marker", out)
        self.assertIn("[folded: 🧩 Analysis chain", out)

    def test_nested_details_fold_inside_a_kept_block(self):
        out = review.fold("<details><summary>ℹ️ Review info</summary>a<details><summary>📥 Commits</summary>"
                          "x</details>b</details>")
        self.assertIn("[folded: 📥 Commits", out)
        self.assertIn("a", out)
        self.assertNotIn(">x<", out)


class OutlineTest(unittest.TestCase):
    def test_php_and_js_declarations_only(self):
        with tempfile.TemporaryDirectory() as d:
            php = Path(d) / "A.php"
            php.write_text("<?php\nclass A extends B\n{\n    public function f($x)\n    {\n"
                           "        if ($x) { return 1; }\n    }\n    protected static function g() {}\n}\n")
            js = Path(d) / "c.js"
            js.write_text("export default class C {\n  @tracked a = 1;\n  @action save() {\n"
                          "    if (x) {\n    }\n  }\n  get dirty() {\n  }\n}\n")
            self.assertEqual(context.outline(php)[1:], ["2: class A extends B", "4: public function f($x)",
                                                        "8: protected static function g() {}"])
            out = context.outline(js)[1:]
            self.assertIn("3: @action save() {", out)
            self.assertIn("7: get dirty() {", out)
            self.assertFalse(any("if (" in l for l in out))

    def test_candidates(self):
        paths, classes, endpoints = context._candidates(
            "Change <code>core/libs/utils/SmtpSettings.php</code> and SettingController via PATCH /api/v1/setting")
        self.assertIn("core/libs/utils/SmtpSettings.php", paths)
        self.assertIn("SettingController", classes)
        self.assertEqual(endpoints, {"setting"})


class JudgeTest(unittest.TestCase):
    def r(self, **kw):
        base = dict(suite="api", summary="api: x", failures=[], rc=0, failed_count=0, ran=5)
        base.update(kw)
        return tests.SuiteResult(**base)

    def test_clean_run_passes(self):
        self.assertTrue(tests.judge(self.r(), set())[0])

    def test_known_failure_explaining_the_exit_code_passes(self):
        self.assertTrue(tests.judge(self.r(failures=["k"], rc=1, failed_count=1), {"k"})[0])

    def test_unexpected_failure_fails(self):
        ok, unexpected = tests.judge(self.r(failures=["k", "new"], rc=1, failed_count=2), {"k"})
        self.assertFalse(ok)
        self.assertEqual(unexpected, ["new"])

    def test_exit_code_not_explained_by_named_failures_fails(self):
        self.assertFalse(tests.judge(self.r(failures=["k"], rc=1, failed_count=2), {"k"})[0])
        self.assertFalse(tests.judge(self.r(rc=2), set())[0])
        self.assertFalse(tests.judge(self.r(failures=["k"], rc=1, failed_count=None), {"k"})[0])

    def test_run_that_executed_nothing_fails(self):
        self.assertFalse(tests.judge(self.r(ran=0), set())[0])

    def test_phpunit_counts(self):
        self.assertEqual(tests.parse_phpunit_counts("OK (61 tests, 139 assertions)"), (61, 0))
        self.assertEqual(tests.parse_phpunit_counts("Tests: 9, Assertions: 20, Errors: 1, Failures: 2."), (9, 3))
        self.assertEqual(tests.parse_phpunit_counts("no summary (see log)"), (None, None))


class ShipTest(unittest.TestCase):
    SPEC = {"title": "feat(x): y", "files": ["a.php"], "what": "W", "why": "Y", "body": "B",
            "notes": "<p>n</p>"}

    def test_spec_requires_the_core_fields(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ship.json"
            p.write_text(json.dumps({"title": "t"}))
            with self.assertRaises(AiError):
                ship.load_spec(p)
            p.write_text(json.dumps(self.SPEC))
            self.assertEqual(ship.load_spec(p)["title"], "feat(x): y")

    def test_message_body_and_notes(self):
        self.assertEqual(ship.commit_message(self.SPEC, "agaia-1"), "feat(x): y\n\nB\n\nRefs agaia-1")
        body = ship.pr_body(self.SPEC, "agaia-1", ["RESULT: PASS"])
        for part in ("## What\nW", "## Why\nY", "```\nRESULT: PASS\n```", "## Contract impact\nNone",
                     "Refs agaia-1"):
            self.assertIn(part, body)
        self.assertIn("<pre>RESULT: PASS</pre>", ship.notes_html(self.SPEC, ["RESULT: PASS"]))

    def test_issue_ref(self):
        self.assertEqual(ship.issue_ref("prometheus", 7), "aprom-7")
        self.assertEqual(ship.issue_ref("dev-tools", 7), "adt-7")
        self.assertEqual(ship.issue_ref("hermes", 7), "ah-7")
        self.assertEqual(ship.issue_ref("ares", 7), "aa-7")


if __name__ == "__main__":
    unittest.main()


class BadFilterTest(unittest.TestCase):
    def test_converted_windows_path_is_rejected(self):
        from lib import tests
        self.assertTrue(tests.bad_filter("C:/Program Files/Git/idea/i"))
        self.assertTrue(tests.bad_filter("c:\\x"))

    def test_plain_and_regex_filters_pass(self):
        from lib import tests
        self.assertIsNone(tests.bad_filter("/idea/i"))
        self.assertIsNone(tests.bad_filter("idea"))


class StaleResultTest(unittest.TestCase):
    def test_changed_since_compares_with_the_current_tree(self):
        from unittest import mock
        from lib import results
        with mock.patch.object(results, "tree_fingerprint", return_value="now"):
            self.assertTrue(results.changed_since("gaia", "before"))
            self.assertFalse(results.changed_since("gaia", "now"))
            self.assertFalse(results.changed_since("gaia", None))

    def test_stale_run_is_not_shippable(self):
        from unittest import mock
        from lib import results
        with tempfile.TemporaryDirectory() as d, mock.patch.object(results, "STATE_DIR", Path(d)):
            results.record("gaia", True, True, ["x"], "before", stale=True)
            last = results.last("gaia")
            self.assertFalse(last["ok"])
            self.assertTrue(last["stale"])
            self.assertEqual(last["fingerprint"], "before")
            with mock.patch.object(ship.results, "last", return_value=last):
                with self.assertRaises(AiError):
                    ship.test_gate("gaia")

    def test_fresh_run_keeps_its_verdict(self):
        from unittest import mock
        from lib import results
        with tempfile.TemporaryDirectory() as d, mock.patch.object(results, "STATE_DIR", Path(d)):
            results.record("gaia", True, True, ["x"], "same")
            self.assertTrue(results.last("gaia")["ok"])
            self.assertFalse(results.last("gaia")["stale"])


class TerminateAsInterruptTest(unittest.TestCase):
    def test_handlers_are_restored_after_the_block(self):
        import signal
        before = signal.getsignal(signal.SIGTERM)
        with tests.terminate_as_interrupt():
            self.assertIsNot(signal.getsignal(signal.SIGTERM), before)
        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_handlers_are_restored_when_the_run_raises(self):
        import signal
        before = signal.getsignal(signal.SIGTERM)
        with self.assertRaises(RuntimeError):
            with tests.terminate_as_interrupt():
                raise RuntimeError
        self.assertIs(signal.getsignal(signal.SIGTERM), before)


class BaselineContainerTest(unittest.TestCase):
    """A baseline run's container is removed, also when an earlier run was killed before its cleanup."""

    def _require(self, have, suites):
        from unittest import mock
        from lib import baseline
        with tempfile.TemporaryDirectory() as d, mock.patch.object(baseline, "BASELINE_DIR", Path(d)), \
                mock.patch.object(baseline, "load", return_value=have), \
                mock.patch.object(baseline, "run") as run, mock.patch.object(baseline, "say"):
            (Path(d) / "gaia").mkdir()
            baseline.require("gaia", "abc", suites, runner=None)
            return [c.args[0] for c in run.call_args_list], Path(d) / "gaia"

    def test_leftover_container_is_removed_when_the_baseline_is_complete(self):
        calls, src = self._require({"api": []}, ["api"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-4:], ["test", "clean", "--src", str(src)])
