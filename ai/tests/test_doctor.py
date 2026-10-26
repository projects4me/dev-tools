"""Tests for `ai doctor gaia` (fake environment) and `ai test gaia --explain` (fixture log)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import doctor, tests  # noqa: E402


class FakeEnv:
    def __init__(self, **kw):
        self.facts = {"serves": True, "crlf": [], "seeded": 1, "counts": (100, 100), "drift": False,
                      "baseline": True, "remote": True, **kw}

    def mount_state(self):
        return self.facts["serves"], "note"

    def crlf_scripts(self):
        return self.facts["crlf"]

    def seed_marker_rows(self):
        return self.facts["seeded"]

    def table_counts(self):
        return self.facts["counts"]

    def backend_drift(self):
        return self.facts["drift"]

    def baseline_recorded(self):
        return self.facts["baseline"]

    def odysseus_reachable(self):
        return self.facts["remote"]


def failed(**kw):
    return {r.name: r for r in doctor.diagnose(FakeEnv(**kw)) if not r.ok}


class DoctorTest(unittest.TestCase):
    def test_healthy_environment_passes_and_is_silent_in_preflight_form(self):
        results = doctor.diagnose(FakeEnv())
        self.assertTrue(all(r.ok for r in results))
        self.assertEqual(doctor.render(results, quiet_when_ok=True), "")

    def test_other_checkout_mounted_is_not_a_failure(self):
        self.assertEqual(failed(serves=False), {})

    def test_crlf_scripts(self):
        r = failed(crlf=["prepare-test-db.sh"])["crlf"]
        self.assertIn("prepare-test-db.sh", r.detail)
        self.assertIn("fix-eol", r.fix)

    def test_unseeded_db(self):
        self.assertIn("prepare-test", failed(seeded=0)["test-db"].fix)

    def test_db_behind_a_migration(self):
        r = failed(counts=(101, 100))["test-db"]
        self.assertIn("migrate", r.fix)
        self.assertIn("prepare-test", r.fix)

    def test_backend_drift(self):
        self.assertIn("backend.json", failed(drift=True))

    def test_missing_baseline(self):
        self.assertIn("ai baseline gaia", failed(baseline=False)["baseline"].fix)

    def test_unreachable_remote(self):
        self.assertIn("odysseus-remote", failed(remote=False))

    def test_a_check_that_cannot_run_is_reported_with_a_fix(self):
        class Broken(FakeEnv):
            def seed_marker_rows(self):
                raise RuntimeError("docker is down")
        r = {x.name: x for x in doctor.diagnose(Broken())}["seed"] if False else \
            [x for x in doctor.diagnose(Broken()) if not x.ok][0]
        self.assertIn("docker is down", r.detail)
        self.assertTrue(r.fix)

    def test_render_lists_each_failure_with_its_fix(self):
        text = doctor.render(doctor.diagnose(FakeEnv(crlf=["a.sh"], remote=False)))
        self.assertEqual(text.count("  fix: "), 2)
        self.assertIn("2 of 6 checks failed", text)


LOG = """PASS get-a
FAIL post-issue-create
  - status: expected 201, got 422
  - jsonEqual: data.attributes.name expected 'x', got 'y'
SKIP other
FAIL second
  - error: expected error payload shape
"""


class ExplainTest(unittest.TestCase):
    def test_lists_the_assertions_of_one_failed_case(self):
        self.assertEqual(tests.explain_case(LOG, "post-issue-create"),
                         ["status: expected 201, got 422", "jsonEqual: data.attributes.name expected 'x', got 'y'"])

    def test_last_case_and_exact_id_match(self):
        self.assertEqual(tests.explain_case(LOG, "second"), ["error: expected error payload shape"])
        self.assertIsNone(tests.explain_case(LOG, "post-issue"))

    def test_passed_case_is_not_explained(self):
        self.assertIsNone(tests.explain_case(LOG, "get-a"))

    def test_report_context_gives_request_and_status(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d, "output")
            out.mkdir()
            (out / "api-tester-client-report.json").write_text(json.dumps({"results": [
                {"id": "post-issue-create", "status": "failed", "request": {"method": "POST", "url": "http://x/api/v1/issue"},
                 "response": {"status": 422}}]}), encoding="utf-8")
            self.assertEqual(tests.report_context("post-issue-create", d), ("POST http://x/api/v1/issue", 422))
            self.assertEqual(tests.report_context("nope", d), (None, None))


if __name__ == "__main__":
    unittest.main()
