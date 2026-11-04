"""The recorded result of the last `ai design-review` per repo, tied to the tree it reviewed like `ai test`'s run
(results.tree_fingerprint), and the PR-body line `ai ship` derives from it.

The verdict is read from the design-reviewer's own report, not taken from the agent (no ship.json field).
"""

import json
import re

from . import results
from .common import STATE_DIR, checkout_state_file

REVIEW_LINE = "DESIGN REVIEW: clean"      # `ai ship` writes it into the PR body, the merge gate reads it
FINDING = re.compile(r"^\s*[-*]\s*\[(blocker|should-fix|nit)\]", re.I)
OPEN = re.compile(r"\[(blocker|should-fix)\]", re.I)            # however the bullet is formatted
NO_FINDINGS = re.compile(r"^\W*no findings\W*$", re.I | re.M)      # the report's own closing line


def verdict(report):
    """{"clean", "findings", "open"} from the reviewer's report. Clean means no blocker or should-fix finding and
    a report that says so (nits are listed but don't block); an empty or unreadable report is not clean and has
    `open` None. Any line naming a blocker or should-fix counts as open, also one not formatted as a bullet."""
    findings = [line.strip() for line in report.splitlines() if FINDING.match(line)]
    open_ = [line for line in report.splitlines() if OPEN.search(line)]
    readable = bool(findings or open_) or bool(NO_FINDINGS.search(report))
    return {"clean": readable and not open_, "findings": findings, "open": len(open_) if readable else None}


def describe(rec):
    """Why a review is or isn't clean, in a few words."""
    if rec.get("stale"):
        return "stale: the tree changed during the review"
    if rec.get("open") is None:
        return "unclear report: no findings list and no 'No findings'"
    return f"{rec['open']} open finding(s)" if rec["open"] else "clean"


def _file(repo):
    return checkout_state_file(f"design-review-{repo}", ".json")


def last(repo):
    path = _file(repo)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def record(repo, fingerprint, found, report, stale=False):
    """Store a review of the tree `fingerprint`; a stale one (the tree changed while it ran) is never clean."""
    STATE_DIR.mkdir(exist_ok=True)
    _file(repo).write_text(json.dumps({"fingerprint": fingerprint, "clean": found["clean"] and not stale,
                                       "stale": stale, "open": found["open"], "findings": found["findings"],
                                       "report": report}, indent=2), encoding="utf-8")


def ship_line(repo):
    """(REVIEW_LINE or None, why): the line `ai ship` puts in the PR body for the current tree."""
    rec = last(repo)
    if not rec:
        return None, f"no recorded design review: run `dev-tools/ai/ai design-review {repo}` on the final tree"
    if rec["fingerprint"] != results.tree_fingerprint(repo):
        return None, f"the tree changed since the last design review: run `dev-tools/ai/ai design-review {repo}`"
    if not rec["clean"]:
        return None, f"the last design review is not clean ({describe(rec)})"
    return REVIEW_LINE, "the design review recorded for this tree is clean"
