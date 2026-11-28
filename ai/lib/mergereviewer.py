"""The review an agent PR needs before Agent Agamemnon merges it: the clean design review `ai ship` records in the
PR body (see reviewrecord). Shared by `ai ship`, the merge gate and Talos."""

import json

from . import reviewrecord
from .common import REPOS, ody, say

EXIT_CODES = {"clean": 0, "unreviewed": 1}


def review_state(repo, pr):
    """clean when the PR body records the design review, else unreviewed. Nothing changes between pushes that the
    recorded line doesn't already bind to the tree (see `ai ship`)."""
    body = ody(repo, "gh", "api", f"repos/{REPOS[repo]}/pulls/{pr}", "--jq", ".body // empty")
    if reviewrecord.recorded_clean(body):
        return {"state": "clean"}
    return {"state": "unreviewed", "detail": "the PR body records no clean design review"}


def cmd_merge_review(args):
    """`ai merge-review`: the PR's merge review, clean or unreviewed (exit 0/1)."""
    result = review_state(args.repo, args.pr)
    say(json.dumps(result) if args.json else result["state"] + (f" - {result['detail']}" if "detail" in result else ""))
    return EXIT_CODES[result["state"]]
