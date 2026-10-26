"""Unit tests for `ai cost` on a fixture transcript. Run: python -m unittest discover -s tests"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import cost  # noqa: E402


def usage(out, inp, create, read):
    return {"output_tokens": out, "input_tokens": inp, "cache_creation_input_tokens": create,
            "cache_read_input_tokens": read}


def assistant(mid, u, *blocks):
    return {"type": "assistant", "timestamp": "2026-10-22T10:00:00Z", "gitBranch": "claude-issue-4000",
            "message": {"id": mid, "usage": u, "content": list(blocks)}}


def tool_use(tid, name, **inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def result(tid, text):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]}}


LINES = [
    {"type": "user", "message": {"content": [{"type": "text", "text": "Work issue #4000"}]}},
    assistant("m1", usage(100, 2, 500, 1000), {"type": "text", "text": "x"}),
    assistant("m1", usage(100, 2, 500, 1000), tool_use("t1", "Read", file_path="D:\Pr4M\gaia\core\A.php")),
    result("t1", "x" * 50),
    assistant("m2", usage(40, 3, 0, 2500), tool_use("t2", "Bash", command="dev-tools/ai/ai test gaia --changed")),
    result("t2", "y" * 20),
]


class CostTest(unittest.TestCase):
    def session(self, name="s1", lines=LINES):
        d = Path(tempfile.mkdtemp())
        path = d / f"{name}.jsonl"
        path.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
        return d, path

    def test_parse_counts_a_message_once(self):
        _, path = self.session()
        s = cost.parse_session(path)
        self.assertEqual((s["turns"], s["output"], s["input"]), (2, 140, 5))
        self.assertEqual((s["cache_creation"], s["cache_read"], s["peak_context"]), (500, 3500, 2503))
        self.assertEqual((s["issue"], s["repo"]), (4000, "gaia"))
        self.assertEqual(s["tool_chars"], {"Read": 50, "Bash:tests": 20})

    def test_rollups_and_repeated_files(self):
        d, _ = self.session("s1")
        self.session("s2")
        (d / "s2.jsonl").write_text((d / "s1.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
        sessions = cost.load_sessions([d])
        report = cost.build_report(sessions)
        self.assertEqual(report["issues"][0]["sessions"], 2)
        self.assertEqual(report["repos"]["gaia"]["median_output"], 140)
        self.assertEqual(report["files_read_in_multiple_sessions"][0]["sessions"], 2)
        self.assertEqual(cost.select(sessions, repo="ares"), [])
        self.assertIn("2 sessions", cost.cost_comment(4000, sessions))

    def test_bash_categories(self):
        self.assertEqual(cost.bash_category("git status"), "git")
        self.assertEqual(cost.bash_category("docker ps"), "docker")
        self.assertEqual(cost.bash_category("cat x"), "file-reads")


if __name__ == "__main__":
    unittest.main()
