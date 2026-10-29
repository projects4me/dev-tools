"""dev-tools/ai: fixed-sequence workflow commands for Claude Code (and humans).

Run `dev-tools/ai/ai -h` for the command list; see dev-tools/ai/README.md.
"""

import argparse
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from lib.common import AiError, REPOS  # noqa: E402
from lib import apitester, coderabbit, context, cost, doctor, kb, review, ship, tests, workflow  # noqa: E402


def build_parser():
    p = argparse.ArgumentParser(prog="ai", description="Projects4Me workflow automation (Agent Odysseus).")
    sub = p.add_subparsers(dest="command", required=True)
    repos = list(REPOS)

    def add(name, func, help_text):
        sp = sub.add_parser(name, help=help_text, description=help_text)
        sp.set_defaults(func=func)
        return sp

    sp = add("start", workflow.cmd_start, "sync dev, create/checkout claude-issue-N, set in_progress, start timer, show issue")
    sp.add_argument("repo", choices=repos)
    sp.add_argument("issue", type=int)
    sp.add_argument("--comments", type=int, default=10, help="recent comments to show (default 10)")
    sp.add_argument("--full", action="store_true", help="show the whole description and all comments")

    sp = add("cost", cost.cmd_cost, "token and turn accounting per session, issue and repo from Claude Code transcripts")
    sp.add_argument("--repo", choices=repos)
    sp.add_argument("--issue", type=int)
    sp.add_argument("--since", metavar="DATE", help="sessions started on or after YYYY-MM-DD")
    sp.add_argument("--json", action="store_true", help="machine-readable report")

    sp = add("sync", workflow.cmd_sync, "fetch origin and fast-forward local dev (after a merge)")
    sp.add_argument("repo", choices=repos)

    sp = add("issue", workflow.cmd_issue, "compact issue view (plain text, recent comments)")
    sp.add_argument("issue", type=int)
    sp.add_argument("--comments", type=int, default=10, help="recent comments to show (default 10)")
    sp.add_argument("--full", action="store_true")

    sp = add("time", workflow.cmd_time, "log actual elapsed time since the issue's last checkpoint")
    sp.add_argument("issue", type=int)
    sp.add_argument("description")
    sp.add_argument("--dry-run", action="store_true")

    sp = add("timer", workflow.cmd_timer, "start/reset an issue's time checkpoint to now")
    sp.add_argument("issue", type=int)
    sp.add_argument("--repo", choices=repos)

    sp = add("comment", workflow.cmd_comment, "add an HTML comment to an issue from a file (or - for stdin)")
    sp.add_argument("issue", type=int)
    sp.add_argument("file")

    sp = add("status", workflow.cmd_status, "set an issue's status (and optionally assignee)")
    sp.add_argument("issue", type=int)
    sp.add_argument("status")
    sp.add_argument("--assignee", help="member name/email/id, or 'reviewer' for your configured reviewer")

    sp = add("test", tests.cmd_test, "run the repo's suites on the workspace checkout; print summaries and "
                                     "failures marked known (config or failing on the base commit) / unexpected")
    sp.add_argument("repo", choices=[*tests.SUITES, "ares"])
    sp.add_argument("--changed", action="store_true",
                    help="only the tests mapped from the branch's changed files (fast; ship needs a full run)")
    sp.add_argument("--filter", help="explicit filter: QUnit text or /regex/ (prometheus), case id/path a|b (gaia api)")
    sp.add_argument("--no-baseline", action="store_true",
                    help="don't run the base commit to classify failures not seen before")
    sp.add_argument("--explain", metavar="CASE", help="gaia: expected vs actual for one failed api case, from the last log")
    sp.add_argument("--case", metavar="ID", help="gaia: run one api case on the already-seeded DB (no re-seed)")
    sp.add_argument("--slot", type=int, metavar="K",
                    help="gaia: run in test slot K (own container and database; see `p4m test slots`)")

    sp = add("devstack", workflow.cmd_devstack, "run a command under the dev-stack lock (one browser check at a "
                                                "time on gaia-dev and Prometheus :4200)")
    sp.add_argument("command", nargs=argparse.REMAINDER, help="-- <command> [args...]")

    sp = add("doctor", doctor.cmd_doctor, "check the Gaia test environment; each failure prints its fix")
    sp.add_argument("repo", choices=["gaia"])
    sp.add_argument("--json", action="store_true")

    sp = add("baseline", tests.cmd_baseline, "record which tests fail on origin/<base> now, by name")
    sp.add_argument("repo", choices=list(tests.SUITES))
    sp.add_argument("--force", action="store_true", help="re-run even if recorded for this commit")

    sp = add("context", context.cmd_context,
             "context pack for an issue: outlines of the files/classes/endpoints it names, tests, api cases, deps")
    sp.add_argument("issue", type=int)
    sp.add_argument("--repo", choices=repos)

    kb.add_parser(sub)

    sp = add("ship", ship.cmd_ship, "test gate, commit, push, PR, tracker notes, time, in_review: one call "
                                    "from a JSON spec (see lib/ship.py)")
    sp.add_argument("repo", choices=repos)
    sp.add_argument("issue", type=int)
    sp.add_argument("file", help="ship.json")
    sp.add_argument("--dry-run", action="store_true", help="print the commit, PR and notes; change nothing")
    sp.add_argument("--no-tests", metavar="REASON", help="ship without a recorded test run (docs-only changes)")
    sp.add_argument("--defer-handoff", action="store_true",
                    help="open the PR and post the notes but keep the issue in_progress with the agent "
                         "(CodeRabbit round pending); finish later with `ai handoff <issue>`")

    sp = add("commit", workflow.cmd_commit,
             "check branch/base, stage exactly FILES, commit as Agent Odysseus (+co-author), push to odysseus")
    sp.add_argument("repo", choices=repos)
    sp.add_argument("issue", type=int)
    msg = sp.add_mutually_exclusive_group(required=True)
    msg.add_argument("-m", "--message")
    msg.add_argument("-F", "--message-file")
    sp.add_argument("--files", nargs="+", required=True)
    sp.add_argument("--no-push", action="store_true")

    sp = add("pr", workflow.cmd_pr,
             "open the PR as Agent Odysseus, then comment the link, log time, set in_review, assign the reviewer")
    sp.add_argument("repo", choices=repos)
    sp.add_argument("issue", type=int)
    sp.add_argument("--title", required=True)
    sp.add_argument("--body-file", required=True)
    sp.add_argument("--time-note", help="what the final time entry covers (default: commit and PR)")
    sp.add_argument("--no-time", action="store_true")

    sp = add("handoff", workflow.cmd_handoff,
             "finish a deferred hand-off (ai ship --defer-handoff): set in_review, assign the reviewer")
    sp.add_argument("issue", type=int)

    sp = add("review", review.cmd_review, "list PR reviews and inline comments (marks what's already answered)")
    sp.add_argument("repo", choices=repos)
    sp.add_argument("pr", type=int)
    sp.add_argument("--open", action="store_true", help="only threads without an agent reply or with follow-ups")
    sp.add_argument("--raw", action="store_true", help="don't fold bot boilerplate (<details> blocks, walkthroughs)")

    sp = add("rabbit", coderabbit.cmd_rabbit, "CodeRabbit's state for the PR head: clean/findings/pending/paused/absent "
             "(exit 0/1/2/3/4)")
    sp.add_argument("repo", choices=repos)
    sp.add_argument("pr", type=int)
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--wait", action="store_true", help="poll every 30 s until the state is not pending")
    sp.add_argument("--timeout", type=float, default=10, help="minutes to wait with --wait (default 10)")

    sp = add("reply", review.cmd_reply, 'post replies from JSON {"<comment id>": "<reply>"}')
    sp.add_argument("repo", choices=repos)
    sp.add_argument("pr", type=int)
    sp.add_argument("file")

    sp = add("pr-body", review.cmd_pr_body, "replace a PR description from a file")
    sp.add_argument("repo", choices=repos)
    sp.add_argument("pr", type=int)
    sp.add_argument("file")

    sp = add("api-cases", apitester.cmd_api_cases, "append api-tester cases (JSON list file) to client|acl|live")
    sp.add_argument("catalog", choices=["client", "acl", "live"])
    sp.add_argument("file")

    sp = add("call", apitester.cmd_call, "call Gaia: test API as an api-tester profile, or dev as Agent Odysseus")
    sp.add_argument("method")
    sp.add_argument("path", help="e.g. issuestatus?limit=5 or /api/v1/setting/<id>")
    sp.add_argument("json", nargs="?")
    sp.add_argument("--target", choices=["test", "dev"], default="test")
    sp.add_argument("--profile", default="default", help="api-tester authProfile (test target only)")
    sp.add_argument("--jq", help="dot path to print, e.g. data.0.attributes")
    sp.add_argument("--max", type=int, default=3000, help="truncate output to N chars")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        code = args.func(args)
    except AiError as e:
        print(f"ai {args.command}: {e}", file=sys.stderr)
        return 1
    return code if isinstance(code, int) else 0


if __name__ == "__main__":
    sys.exit(main())
