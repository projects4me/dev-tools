"""`ai mergecheck` / `ai merge`: the fixed gate an agent PR passes before Agent Agamemnon approves and merges it.

Every check reads the PR's current head commit. A check is ok, pending (it may pass later: CI or CodeRabbit still
running, GitHub still computing mergeability) or failed. The gate passes only when every check is ok.
"""

import json

from . import coderabbit
from .common import REPOS, AiError, agamemnon, config, say
from .review import _api, _is_bot, open_threads, resolved_threads

OK, PENDING, FAIL = "ok", "pending", "fail"
EXIT_CODES = {OK: 0, FAIL: 1, PENDING: 2}
CI_PASSED = ("success", "neutral", "skipped")
TESTS_PASSED = "RESULT: PASS"
DO_NOT_MERGE = "do-not-merge"
CONFLICTED = ("dirty",)
BEHIND = ("behind",)


def _check(name, state, detail):
    return {"name": name, "state": state, "detail": detail}


def check_open(pr):
    if pr.get("state") != "open":
        return _check("open", FAIL, f"the PR is {pr.get('state')}")
    if pr.get("draft"):
        return _check("open", FAIL, "the PR is a draft")
    return _check("open", OK, "open, not a draft")


def check_coderabbit(enabled, verdict):
    """verdict is coderabbit.classify()'s result for the head commit."""
    if not enabled:
        return _check("coderabbit", FAIL, "CodeRabbit doesn't review this repo")
    state = verdict["state"]
    if state == "clean":
        return _check("coderabbit", OK, "clean, no open CodeRabbit threads")
    if state == "pending":
        return _check("coderabbit", PENDING, "review still running")
    if state == "findings":
        return _check("coderabbit", FAIL, f"{verdict['open']} open CodeRabbit thread(s)")
    return _check("coderabbit", FAIL, f"{state}" + (f": {verdict['detail']}" if verdict.get("detail") else ""))


def check_ci(check_runs, statuses):
    """Every check run and the newest status of every context on the head commit has passed."""
    running, failed = [], []
    for run in check_runs:
        name = run.get("name") or (run.get("app") or {}).get("slug") or "check"
        if run.get("status") != "completed":
            running.append(name)
        elif run.get("conclusion") not in CI_PASSED:
            failed.append(f"{name} ({run.get('conclusion')})")
    newest = {}
    for status in statuses:                    # GitHub lists them newest first
        newest.setdefault(status["context"], status)
    for context, status in newest.items():
        if status["state"] == "pending":
            running.append(context)
        elif status["state"] != "success":
            failed.append(f"{context} ({status['state']})")
    if failed:
        return _check("ci", FAIL, "failed: " + ", ".join(failed))
    if running:
        return _check("ci", PENDING, "still running: " + ", ".join(running))
    total = len(check_runs) + len(newest)
    return _check("ci", OK, f"{total} check(s) passed" if total else "no CI checks on this commit")


def check_tests(body):
    """`ai ship` records the full run's RESULT in the PR body's How tested section."""
    if TESTS_PASSED in (body or ""):
        return _check("tests", OK, "the PR records a passing full test run")
    return _check("tests", FAIL, "the PR body records no passing full test run (ai ship)")


def check_mergeable(pr):
    state = pr.get("mergeable_state") or "unknown"
    if pr.get("mergeable") is None:
        return _check("mergeable", PENDING, "GitHub is still computing mergeability")
    if not pr.get("mergeable") or state in CONFLICTED:
        return _check("mergeable", FAIL, "conflicts with the base branch")
    if state in BEHIND:
        return _check("mergeable", FAIL, "behind the base branch")
    return _check("mergeable", OK, f"no conflicts ({state})")


def is_human(user, agents):
    return not _is_bot(user) and user.get("login") not in agents


def check_humans(reviews, issue_comments, review_comments, resolved, head_date, agents):
    """No human asks for changes, has an open review thread, or commented on the PR after the last push."""
    problems = []
    latest = {}
    for review in reviews:                     # oldest first: the last one per reviewer counts
        if is_human(review["user"], agents) and review.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[review["user"]["login"]] = review["state"]
    problems += [f"{login} requested changes" for login, state in latest.items() if state == "CHANGES_REQUESTED"]
    author = next(iter(agents))
    threads = [c for c in open_threads(review_comments, resolved, author)[0] if is_human(c["user"], agents)]
    if threads:
        problems.append(f"{len(threads)} open review thread(s) from people")
    late = [c for c in issue_comments if is_human(c["user"], agents) and (c.get("created_at") or "") > head_date]
    if late:
        problems.append(f"{len(late)} comment(s) from people since the last push")
    if problems:
        return _check("people", FAIL, "; ".join(problems))
    return _check("people", OK, "no change requests or unanswered comments from people")


def check_labels(pr):
    names = [label.get("name", "").lower() for label in pr.get("labels") or []]
    if DO_NOT_MERGE in names:
        return _check("labels", FAIL, f"labelled {DO_NOT_MERGE}")
    return _check("labels", OK, f"no {DO_NOT_MERGE} label")


def verdict(checks):
    """FAIL when any check failed, else PENDING when any is still pending, else OK."""
    states = {c["state"] for c in checks}
    return FAIL if FAIL in states else PENDING if PENDING in states else OK


def gate(repo, pr_number):
    """{"state", "sha", "checks"} for the PR's current head commit, read from GitHub."""
    full = REPOS[repo]
    cfg = config()
    author, merger = cfg["agentLogin"], cfg.get("mergerLogin", "agent-agamemnon")
    pr = _api(repo, f"repos/{full}/pulls/{pr_number}")
    sha = pr["head"]["sha"]
    head_date = _api(repo, f"repos/{full}/commits/{sha}")["commit"]["committer"]["date"]
    payload = _api(repo, f"repos/{full}/commits/{sha}/check-runs")
    check_runs = payload.get("check_runs", []) if isinstance(payload, dict) else []
    statuses = _api(repo, f"repos/{full}/commits/{sha}/statuses")
    issue_comments = _api(repo, f"repos/{full}/issues/{pr_number}/comments")
    review_comments = _api(repo, f"repos/{full}/pulls/{pr_number}/comments")
    reviews = _api(repo, f"repos/{full}/pulls/{pr_number}/reviews")
    resolved = resolved_threads(repo, pr_number)
    enabled = coderabbit.enabled(repo)
    rabbit = coderabbit.classify(coderabbit.review_status(check_runs, statuses), issue_comments, review_comments,
                                 resolved, author) if enabled else None
    checks = [check_open(pr), check_coderabbit(enabled, rabbit), check_ci(check_runs, statuses),
              check_tests(pr.get("body")), check_mergeable(pr),
              check_humans(reviews, issue_comments, review_comments, resolved, head_date, (author, merger)),
              check_labels(pr)]
    return {"state": verdict(checks), "sha": sha, "checks": checks}


def report(result, as_json):
    if as_json:
        say(json.dumps(result))
        return
    say(f"{result['state']} @ {result['sha'][:7]}")
    for c in result["checks"]:
        say(f"  {c['state']:<8}{c['name']:<12}{c['detail']}")


def cmd_mergecheck(args):
    """Print the gate (ok / pending / fail, exit 0 / 2 / 1) for the PR's head commit."""
    result = gate(args.repo, args.pr)
    report(result, args.json)
    return EXIT_CODES[result["state"]]


def cmd_merge(args):
    """Run the gate again; when it passes, approve and merge the PR as Agent Agamemnon (a merge commit, pinned to
    the commit the gate read), and print the merge commit. Refuses otherwise."""
    result = gate(args.repo, args.pr)
    if result["state"] != OK:
        report(result, args.json)
        return EXIT_CODES[result["state"]]
    full, sha = REPOS[args.repo], result["sha"]
    agamemnon("pr", "review", str(args.pr), "--repo", full, "--approve",
              "--body", "Approved by Agent Agamemnon: the merge gate passed on " + sha[:7] + ".")
    agamemnon("pr", "merge", str(args.pr), "--repo", full, "--merge", "--match-head-commit", sha)
    merged = agamemnon("api", f"repos/{full}/pulls/{args.pr}", "--jq", ".merge_commit_sha").strip()
    if not merged:
        raise AiError(f"PR #{args.pr} was approved but GitHub reports no merge commit")
    result["merged"] = merged
    if args.json:
        say(json.dumps(result))
    else:
        say(f"merged #{args.pr} @ {sha[:7]} as {merged[:7]}")
    return 0
