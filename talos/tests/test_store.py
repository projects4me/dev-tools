"""SQLite store, legacy import, per-run cost, transcript stats and the warm-resume rule."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import db, policy, runner, runs as run_log, sessions  # noqa: E402
from lib.registry import Action  # noqa: E402

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


class TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [mock.patch.object(db, "DB_FILE", self.tmp / "talos.db"),
                        mock.patch.object(db, "LEGACY_SESSIONS", self.tmp / "sessions.json")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()


class SessionsTest(TempDb):
    def test_update_creates_then_merges(self):
        sessions.update("odysseus", 7, runs=1, last_model="sonnet")
        sessions.update("odysseus", 7, running=True)
        state = sessions.get("odysseus", 7)
        self.assertEqual((state["runs"], state["last_model"], state["running"]), (1, "sonnet", 1))
        self.assertEqual(sessions.interrupted("odysseus"), [7])

    def test_handoff_fields_round_trip(self):
        sessions.update("odysseus", 7, handoff_reason="turn_budget", handoff_detail='{"a": 1}',
                        handoff_at="2026-10-31T01:00:00+00:00", auto_retries=2)
        state = sessions.get("odysseus", 7)
        self.assertEqual((state["handoff_reason"], state["handoff_detail"], state["auto_retries"]),
                         ("turn_budget", '{"a": 1}', 2))

    def test_migration_adds_columns_to_an_old_schema_and_runs_twice(self):
        import sqlite3
        path = self.tmp / "old.db"
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE issues (agent TEXT NOT NULL, issue INTEGER NOT NULL, runs INTEGER DEFAULT 0, "
                    "PRIMARY KEY (agent, issue))")
        old.execute("INSERT INTO issues (agent, issue, runs) VALUES ('odysseus', 1, 3)")
        old.commit()
        old.close()
        for _ in range(2):
            db._initialized.discard(str(path))
            conn = db.connect(path)
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(issues)")}
            row = conn.execute("SELECT runs, auto_retries FROM issues").fetchone()
            conn.close()
            self.assertLessEqual({"handoff_reason", "handoff_detail", "handoff_at", "auto_retries"}, cols)
            self.assertEqual((row["runs"], row["auto_retries"]), (3, 0))

    def test_reset_clears_runs_and_auto_retries(self):
        import argparse
        import talos
        sessions.update("odysseus", 7, runs=4, auto_retries=2)
        agent = mock.Mock(max_iterations=10)
        agent.name = "odysseus"
        with mock.patch.object(talos, "load_agents", lambda name: [agent]):
            talos.cmd_reset(argparse.Namespace(agent="odysseus", issue=7, model=False))
        state = sessions.get("odysseus", 7)
        self.assertEqual((state["runs"], state["auto_retries"]), (0, 0))

    def test_unknown_field_rejected(self):
        with self.assertRaises(ValueError):
            sessions.update("odysseus", 7, sessionId="x")

    def test_missing_issue_is_empty(self):
        self.assertEqual(sessions.get("odysseus", 99), {})


class LegacyImportTest(TempDb):
    def test_imports_and_renames(self):
        (self.tmp / "sessions.json").write_text(json.dumps({
            "odysseus:3967": {"running": True, "runs": 1, "lastModel": "sonnet", "liveSessionId": "ac18",
                              "startedAt": "2026-10-18T02:53:57+05:00", "sessionId": "ac18"}}), encoding="utf-8")
        self.assertEqual(db.import_legacy(), 1)
        state = sessions.get("odysseus", 3967)
        self.assertEqual((state["running"], state["runs"], state["session_id"]), (1, 1, "ac18"))
        self.assertFalse((self.tmp / "sessions.json").exists())
        self.assertTrue((self.tmp / "sessions.json.imported").exists())

    def test_nothing_to_import(self):
        self.assertEqual(db.import_legacy(), 0)


class RunsTest(TempDb):
    def test_start_and_finish(self):
        rid = run_log.start("odysseus", 7, 1, "sonnet", "s1", False, "new", "2026-10-18T12:00:00+00:00")
        self.assertIsNone(run_log.recent(1)[0]["ended_at"])
        run_log.finish(rid, "2026-10-18T12:05:00+00:00", 300.0, "done", "ok", 0.3, 0.3,
                       {"turns": 12, "peak_context_tokens": 48000}, "x.json")
        row = run_log.recent(1)[0]
        self.assertEqual((row["outcome"], row["turns"], row["peak_context_tokens"]), ("done", 12, 48000))

    def test_close_unfinished(self):
        run_log.start("odysseus", 7, 1, "sonnet", "s1", False, "new", "2026-10-18T12:00:00+00:00")
        run_log.close_unfinished("odysseus", 7, "2026-10-18T13:00:00+00:00")
        self.assertEqual(run_log.recent(1)[0]["outcome"], "interrupted")


class IncrementalCostTest(unittest.TestCase):
    def test_resumed_session_subtracts_previous_total(self):
        self.assertAlmostEqual(run_log.incremental_cost(1.67, 1.46, True), 0.21)

    def test_fresh_session_is_all_this_run(self):
        self.assertAlmostEqual(run_log.incremental_cost(0.30, 1.46, False), 0.30)

    def test_unknown_or_lower_report(self):
        self.assertIsNone(run_log.incremental_cost(None, 1.0, True))
        self.assertAlmostEqual(run_log.incremental_cost(0.5, 1.0, True), 0.5)


class ResumeSessionTest(unittest.TestCase):
    resume = Action("x", resume=True)

    def state(self, minutes_ago):
        return {"session_id": "s1", "last_ended_at": (NOW - timedelta(minutes=minutes_ago)).isoformat()}

    def test_warm(self):
        self.assertEqual(policy.resume_session(self.resume, self.state(20), NOW, 55)[0], "s1")

    def test_cold(self):
        self.assertIsNone(policy.resume_session(self.resume, self.state(56), NOW, 55)[0])

    def test_zero_means_always_resume(self):
        self.assertEqual(policy.resume_session(self.resume, self.state(600), NOW, 0)[0], "s1")

    def test_action_without_resume(self):
        self.assertIsNone(policy.resume_session(Action("x"), self.state(1), NOW, 55)[0])

    def test_no_end_time(self):
        self.assertIsNone(policy.resume_session(self.resume, {"session_id": "s1"}, NOW, 55)[0])


class UsageSinceTest(unittest.TestCase):
    def test_counts_only_new_messages_once(self):
        def msg(ts, mid, read, write, out):
            return json.dumps({"type": "assistant", "timestamp": ts, "message": {"id": mid, "usage": {
                "input_tokens": 2, "cache_read_input_tokens": read, "cache_creation_input_tokens": write,
                "output_tokens": out}}})
        lines = [msg("2026-10-18T11:00:00Z", "old", 90000, 0, 50),        # earlier run: excluded
                 msg("2026-10-18T12:01:00Z", "a", 30000, 5000, 100),
                 msg("2026-10-18T12:01:01Z", "a", 30000, 5000, 100),       # streamed part of "a"
                 msg("2026-10-18T12:02:00Z", "b", 35000, 1000, 200),
                 json.dumps({"type": "user", "timestamp": "2026-10-18T12:03:00Z"})]
        path = Path(tempfile.mkdtemp()) / "t.jsonl"
        path.write_text("\n".join(lines), encoding="utf-8")
        stats = runner.usage_since(path, NOW)
        self.assertEqual(stats["turns"], 2)
        self.assertEqual(stats["cache_read_tokens"], 65000)
        self.assertEqual(stats["output_tokens"], 300)
        self.assertEqual(stats["peak_context_tokens"], 36002)

    def test_no_transcript(self):
        self.assertEqual(runner.usage_since(None, NOW)["turns"], 0)


if __name__ == "__main__":
    unittest.main()
