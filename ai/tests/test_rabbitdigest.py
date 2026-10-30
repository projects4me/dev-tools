"""CodeRabbit digest parsing on fixture bodies; no network. Run: python -m unittest discover -s tests"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import rabbitdigest as rd  # noqa: E402

BOT = {"login": "coderabbitai[bot]", "type": "Bot"}
AGENT = {"login": "agent-odysseus", "type": "User"}
OLD = "_⚠️ Potential issue_ | _🟠 Major_\n\n**Guard the null case**\n\nBody text."
NEW = "**🛠️ Refactor suggestion** | **🟡 Minor**\n\n**Extract the helper**\n\nBody."
REVIEW = """**Actionable comments posted: 1**
<details><summary>⚠️ Outside diff range comments (1)</summary><blockquote>
<details><summary>ai/lib/a.py (1)</summary><blockquote>
`10-12`: **Stale docstring**

_🧹 Nitpick_ | _🔵 Trivial_

text
</blockquote></details>
</blockquote></details>
<details><summary>🧹 Nitpick comments (2)</summary><blockquote>
<details><summary>ai/lib/b.py (2)</summary><blockquote>
`5`: **Rename x**

text

`9-10`: **Use pathlib**

**🧹 Nitpick** | **🔵 Trivial**
</blockquote></details>
</blockquote></details>
<details><summary>Walkthrough</summary>`3`: **not a finding**</details>"""


def c(i, body, user=BOT, reply=None, path="a.py"):
    return {"id": i, "user": user, "body": body, "in_reply_to_id": reply, "path": path}


class DigestTest(unittest.TestCase):
    def test_old_and_new_inline_formats(self):
        a = rd.parse_inline(c(1, OLD), False)
        self.assertEqual((a["category"], a["severity"], a["title"]), ("Potential issue", "Major", "Guard the null case"))
        b = rd.parse_inline(c(2, NEW), True)
        self.assertEqual((b["category"], b["severity"], b["title"]),
                         ("Refactor suggestion", "Minor", "Extract the helper"))
        self.assertTrue(b["addressed"])

    def test_review_body_sections(self):
        f = rd.parse_review_body(REVIEW)
        self.assertEqual([(x["file"], x["title"], x["severity"], x["source"]) for x in f], [
            ("ai/lib/a.py", "Stale docstring", "Trivial", "outside diff range"),
            ("ai/lib/b.py", "Rename x", "Trivial", "nitpick"),
            ("ai/lib/b.py", "Use pathlib", "Trivial", "nitpick")])
        self.assertEqual(f[0]["category"], "Nitpick")

    def test_replies_and_walkthrough_are_skipped(self):
        comments = [c(1, OLD), c(2, "Fixed in abc", user=AGENT, reply=1)]
        reviews = [{"user": BOT, "body": "<details><summary>Walkthrough</summary>`3`: **x**</details>"},
                   {"user": AGENT, "body": REVIEW}]
        found = rd.findings_for_pr(reviews, comments, set(), "agent-odysseus")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["addressed"])      # the agent answered it

    def test_unanswered_is_open_and_resolved_is_addressed(self):
        found = rd.findings_for_pr([], [c(1, OLD), c(2, NEW)], {2}, "agent-odysseus")
        self.assertEqual([f["addressed"] for f in found], [False, True])

    def test_digest_counts_and_buckets(self):
        f = rd.parse_inline(c(1, OLD), False)
        prs = [{"repo": "gaia", "number": 1, "files": 3, "lines": 500, "findings": [f, f]},
               {"repo": "gaia", "number": 2, "files": 20, "lines": 2000, "findings": [f]}]
        d = rd.digest(prs)
        self.assertEqual(d["severity"]["Major"], 3)
        self.assertEqual(d["buckets"]["1-5 files"]["findings"], 2)
        self.assertEqual(d["buckets"]["16+ files"]["lines"], 2000)
        text = rd.render(d)
        self.assertIn("4.0", text)                  # 2 findings per 0.5k lines
        self.assertIn("gaia #2", text)


if __name__ == "__main__":
    unittest.main()
