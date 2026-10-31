"""Unit tests for the Agamemnon premise, classifier and incident packet. Run: python -m unittest discover -s tests"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.agamemnon import classifier, packet, premise  # noqa: E402
from lib.config import TalosError  # noqa: E402

TYPES = {"test_contention", "ci_failed", "merge_conflict", "dirty_generated", "docker_down", "handback_failed"}


class PremiseTests(unittest.TestCase):
    def test_shipped_premise_lists_the_six_incident_types(self):
        p = premise.load()
        self.assertEqual(set(p.incidents), TYPES)
        self.assertEqual(p.tiers_for("docker_down"), ("read", "pause", "notify"))
        self.assertFalse(p.allows("docker_down", "restore"))

    def test_unlisted_type_is_not_handled(self):
        p = premise.load()
        self.assertFalse(p.handles("other"))
        self.assertEqual(p.tiers_for("other"), ())

    def test_unknown_tier_is_rejected(self):
        with self.assertRaises(TalosError):
            premise.parse({"tiers": {"read": ""}, "incidents": {"x": {"tiers": ["write"]}}})

    def test_other_cannot_be_listed(self):
        with self.assertRaises(TalosError):
            premise.parse({"tiers": {}, "incidents": {"other": {"tiers": []}}})

    def test_missing_file_is_a_talos_error(self):
        with self.assertRaises(TalosError):
            premise.load(Path("no-such-premise.json"))

    def test_every_shipped_incident_is_a_classifier_output(self):
        self.assertEqual({r[0] for r in classifier.RULES}, TYPES)


class ClassifierTests(unittest.TestCase):
    def test_cases(self):
        cases = [
            ("handoff_failed", "", "", "handback_failed"),
            ("talos_error", "", "Cannot connect to the Docker daemon at npipe", "docker_down"),
            ("agent_blocked", "", "CONFLICT (content): Merge conflict in a.py", "merge_conflict"),
            ("agent_blocked", "tests: tree changed after a parallel run", "", "test_contention"),
            ("run_error", "", "the runner hung, no free slot", "test_contention"),
            ("ci_failed", "", "", "ci_failed"),
            ("agent_blocked", "merge gate failed: ci", "", "ci_failed"),
            ("agent_blocked", "generated files modified", "", "dirty_generated"),
            ("agent_needs_input", "which design?", "", "other"),
            (None, None, None, "other"),
        ]
        for reason, summary, log, expected in cases:
            with self.subTest(reason=reason, summary=summary):
                self.assertEqual(classifier.classify(reason, summary, log), expected)


class PacketTests(unittest.TestCase):
    def test_build(self):
        comments = [{"author": "Agent Agamemnon", "at": "2", "text": "b"}, {"author": "Someone", "at": "1", "text": "x"},
                    {"author": "Agent Agamemnon", "at": "1", "text": "a"}]
        log = "\n".join(str(i) for i in range(200))
        p = packet.build(4228, 90, {"reason": "ci_failed", "detail": "d"}, {"summary": "s", "log": log},
                         {"state": "fail"}, {"dirty": []}, {"up": True}, {4228: ["a"], 5: ["z", "b"]}, comments,
                         incident="ci_failed")
        self.assertEqual(p["issue"], 4228)
        self.assertEqual(p["handback"], {"reason": "ci_failed", "detail": "d"})
        self.assertEqual(p["run"]["logTail"].splitlines()[-1], "199")
        self.assertEqual(len(p["run"]["logTail"].splitlines()), packet.LOG_TAIL_LINES)
        self.assertEqual(p["inFlight"], [{"issue": 5, "files": ["b", "z"]}])
        self.assertEqual([c["text"] for c in p["earlierComments"]], ["a", "b"])
        json.dumps(p)

    def test_build_without_pr_or_run(self):
        p = packet.build("7", None, None, None, None, None, None, None, None)
        self.assertEqual(p["inFlight"], [])
        self.assertEqual(p["run"]["logTail"], "")


if __name__ == "__main__":
    unittest.main()
