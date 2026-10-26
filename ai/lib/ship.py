"""`ai ship`: finish an issue in one call from a single JSON file.

The agent writes what only it knows (title, summary, decisions, the file list); this does the rest in a
fixed order: pre-flight → test gate → commit + push → PR → tracker notes with the PR link, time, in_review.

Each finished step is recorded in dev-tools/ai/state/ship-<repo>-<issue>.json, so after a failure (network,
GitHub, tracker) running the same command again resumes at the failed step instead of repeating or
refusing earlier ones. The record is removed when the hand-off is done.

ship.json:
{
  "title":      "feat(settings): ...",             commit subject and PR title (required)
  "body":       "commit body, plain text",         optional
  "files":      ["path", ...],                     exactly the files to commit (required)
  "what":       "markdown",                        PR "What" (required)
  "why":        "markdown",                        PR "Why" (required)
  "howTested":  "markdown",                        optional, added under the recorded test result
  "contract":   "markdown",                        optional, default "None"
  "migration":  "markdown",                        optional, default "None"
  "outOfScope": "markdown",                        optional
  "notes":      "<p>html</p>",                     tracker notes: decisions, deviations, follow-ups
  "timeNote":   "what the last stretch covered"    optional
}
"""

import html
import json
from pathlib import Path

from . import cost, results, workflow
from .common import (AiError, PROJECT_FOR_REPO, REPOS, STATE_DIR, base_branch, config, git, ody, say)

SHORTCODE = {"A Gaia": "agaia", "A Prometheus": "aprom", "A Hermes": "ah", "A Ares": "aa", "A Dev Tool": "adt"}
REQUIRED = ("title", "files", "what", "why")


def load_spec(path):
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    missing = [k for k in REQUIRED if not spec.get(k)]
    if missing:
        raise AiError(f"{path}: missing {', '.join(missing)}")
    if not isinstance(spec["files"], list):
        raise AiError(f"{path}: files must be a list of paths")
    return spec


def issue_ref(repo, issue):
    return f"{SHORTCODE.get(PROJECT_FOR_REPO.get(repo), 'agaia')}-{issue}"


def test_gate(repo):
    """The recorded test result lines, if a full, passing run covers exactly the current tree."""
    last = results.last(repo)
    if not last:
        raise AiError(f"no recorded test run: run `dev-tools/ai/ai test {repo}` on the final tree first")
    if last["fingerprint"] != results.tree_fingerprint(repo):
        raise AiError(f"the tree changed since the last test run: run `dev-tools/ai/ai test {repo}` again")
    if not last["full"]:
        raise AiError(f"the last run was targeted: run the full `dev-tools/ai/ai test {repo}` before shipping")
    if not last["ok"]:
        raise AiError("the last full test run failed (see its RESULT); fix or explain before shipping")
    return last["lines"]


def commit_message(spec, ref):
    parts = [spec["title"].strip()]
    if spec.get("body"):
        parts.append(spec["body"].strip())
    parts.append(f"Refs {ref}")
    return "\n\n".join(parts)


def pr_body(spec, ref, test_lines):
    sections = [("What", spec["what"]), ("Why", spec["why"]),
                ("How tested", "```\n" + "\n".join(test_lines) + "\n```"
                 + (f"\n\n{spec['howTested'].strip()}" if spec.get("howTested") else "")),
                ("Contract impact", spec.get("contract") or "None"),
                ("Migration impact", spec.get("migration") or "None")]
    if spec.get("outOfScope"):
        sections.append(("Out of scope", spec["outOfScope"]))
    return "\n\n".join(f"## {t}\n{b.strip()}" for t, b in sections) + f"\n\nRefs {ref}"


def notes_html(spec, test_lines):
    tested = "<p><strong>Tests</strong></p><pre>" + html.escape("\n".join(test_lines)) + "</pre>"
    return (spec.get("notes") or "") + tested


# --- progress ------------------------------------------------------------------------------------

def _progress_file(repo, issue):
    return STATE_DIR / f"ship-{repo}-{issue}.json"


def load_progress(repo, issue):
    p = _progress_file(repo, issue)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_progress(repo, issue, progress):
    STATE_DIR.mkdir(exist_ok=True)
    _progress_file(repo, issue).write_text(json.dumps(progress, indent=2), encoding="utf-8")


def existing_pr(repo, issue):
    out = ody(repo, "gh", "pr", "list", "--repo", REPOS[repo], "--head", workflow.branch_name(issue),
              "--state", "open", "--json", "url", "--jq", ".[0].url // empty", check=False)
    return out.strip() or None


# --- command -------------------------------------------------------------------------------------

def cost_note(issue):
    """The issue's token cost as a tracker line; never blocks a ship."""
    try:
        line = cost.cost_comment(issue)
    except Exception:  # noqa: BLE001 - accounting is best effort
        return ""
    return f"<p>{html.escape(line)}</p>" if line else ""


def cmd_ship(args):
    repo, issue = args.repo, args.issue
    spec = load_spec(args.file)
    ref = issue_ref(repo, issue)
    if not config().get("reviewer"):            # before anything is posted
        raise AiError("no reviewer configured: run `dev-tools/installer/p4m configure` (sets ai/config.local.json)")
    progress = load_progress(repo, issue)
    head = git(repo, "rev-parse", "HEAD")
    committed = progress.get("commit") == head

    if committed:
        test_lines = progress["testLines"]
        say(f"resuming: {head[:8]} already committed and pushed")
    else:
        test_lines = (["(tests not applicable: " + args.no_tests + ")"] if args.no_tests else test_gate(repo))
    message, body, notes = commit_message(spec, ref), pr_body(spec, ref, test_lines), notes_html(spec, test_lines)

    if args.dry_run:
        say("--- commit message ---\n" + message + "\n--- files ---\n" + "\n".join(spec["files"]))
        say("--- PR: " + spec["title"] + " ---\n" + body + "\n--- tracker notes ---\n" + notes)
        if not committed:
            workflow.check_commit(repo, issue, spec["files"])
        say("\n[dry-run] checks passed; nothing committed")
        return 0

    if not committed:
        workflow.commit_files(repo, issue, message, spec["files"], push=True)
        progress = {"commit": git(repo, "rev-parse", "HEAD"), "testLines": test_lines}
        save_progress(repo, issue, progress)
    elif not progress.get("pr"):
        ody(repo, "git", "push", "-q", "-u", "odysseus", workflow.branch_name(issue))   # in case the push failed
    if not progress.get("pr"):
        progress["pr"] = existing_pr(repo, issue) or workflow.open_pr(repo, issue, spec["title"], body)
        save_progress(repo, issue, progress)
    notes += cost_note(issue)
    workflow.pr_hand_off(issue, progress["pr"], git(repo, "rev-parse", "--short", "HEAD"),
                         git(repo, "rev-parse", "--short", f"origin/{base_branch(repo)}"),
                         spec.get("timeNote") or f"commit and PR ({spec['title']})", notes_html=notes)
    _progress_file(repo, issue).unlink(missing_ok=True)
    return 0
