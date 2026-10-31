"""`ai context <issue>`: a deterministic context pack for starting an issue.

From the issue's text it finds the files, classes and endpoints it names, and prints for each file an
outline (classes, functions and methods with line numbers) instead of the whole file, plus the related
tests, api-tester cases, recent commits and the state of the issues it depends on. Read only the parts
you then need with Read (offset/limit) or `sed -n a,bp`.
"""

import json
import re
import subprocess
from pathlib import Path

from . import gaiapack
from .common import PROJECT_FOR_REPO, git, repo_dir, say, strip_html, tracker_call

_PHP_DECL = re.compile(r"^\s*(?:abstract\s+|final\s+)?(?:class|interface|trait)\s+\w+"
                       r"|^\s*(?:(?:public|protected|private|static|abstract|final)\s+)*function\s+&?\w+\s*\(")
_JS_DECL = re.compile(r"^\s*(?:export\s+(?:default\s+)?)?class\s+\w+"
                      r"|^\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s*\w+\s*\("
                      r"|^\s*@(?:action|tracked|service)\b.*"
                      r"|^\s*(?:get |set |async |static )?(?!(?:if|for|while|switch|catch|return)\b)\w+\s*"
                      r"\([^)]*\)\s*\{\s*$"
                      r"|^\s*(?:export\s+)?const\s+\w+\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>"
                      r"|^\s*\w+\s*:\s*(?:attr|belongsTo|hasMany)\(")
_FEATURE_DECL = re.compile(r"^\s*(?:Feature|Scenario(?: Outline)?|Background):")
_SKIP_WORDS = {"null", "true", "false", "none", "status", "test", "id"}


def _candidates(text):
    """Path-like strings, class-like names and API endpoints mentioned in the issue text."""
    paths = set(re.findall(r"[\w./-]+/[\w.-]+\.(?:php|js|ts|hbs|json|feature|sql|py|md|scss|yml|yaml)\b", text))
    classes = set(re.findall(r"\b([A-Z][a-zA-Z0-9]+(?:Controller|Component|Service|Settings|Sender|Mapper|"
                             r"Notification|Mailer|Model|Behavior|Helper|Adapter|Serializer|Test))\b", text))
    classes |= set(re.findall(r"(?:\\|::|\b)([A-Z][a-z]+[A-Z][A-Za-z]+)::", text))
    endpoints = set(re.findall(r"/api/v1/([a-z][a-z0-9_]*)", text))
    return paths, classes, endpoints


def _ls_files(repo):
    return git(repo, "ls-files").splitlines()


def resolve(repo, paths, classes, endpoints):
    """Map the candidates to files that exist in the repo."""
    files = _ls_files(repo)
    found = {}
    for p in paths:
        p = p.lstrip("./")
        for f in files:
            if f == p or f.endswith("/" + p):
                found.setdefault(f, "named")
    for c in classes:
        for f in files:
            if Path(f).stem == c:
                found.setdefault(f, f"class {c}")
    for e in endpoints:
        ctrl = e[:1].upper() + e[1:] + "Controller.php"
        for f in files:
            if f.endswith("/" + ctrl) or f.endswith(f"/metadata/model/{e[:1].upper() + e[1:]}.php"):
                found.setdefault(f, f"endpoint /api/v1/{e}")
    return found


def outline(path, limit=60):
    """Declarations with line numbers (the file's shape, not its body)."""
    p = Path(path)
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    decl = _PHP_DECL if p.suffix == ".php" else _JS_DECL if p.suffix in (".js", ".ts") else \
        _FEATURE_DECL if p.suffix == ".feature" else None
    if not decl:
        return [f"({len(lines)} lines)"]
    out = [f"{i}: {l.strip()[:140]}" for i, l in enumerate(lines, 1) if decl.search(l)]
    if len(out) > limit:
        out = out[:limit] + [f"… {len(out) - limit} more"]
    return [f"({len(lines)} lines)"] + out


def related_tests(repo, f):
    """Tests named after the file first, then tests that mention its class as a whole word."""
    stem = Path(f).stem.replace("Controller", "")
    by_name = [t for t in git(repo, "ls-files", "--", "tests", check=False).splitlines()
               if stem.lower() in Path(t).name.lower()]
    by_word = git(repo, "grep", "-l", "-w", Path(f).stem, "--", "tests", "tools/api-tester/apis",
                  check=False).splitlines()
    seen, out = set(), []
    for t in by_name + by_word:
        if t != f and t not in seen and not t.endswith(".result.cache"):
            seen.add(t)
            out.append(t)
    return out[:6]


def api_cases(repo, endpoint):
    """api-tester case ids hitting /api/v1/<endpoint> (gaia only)."""
    d = repo_dir(repo) / "tools" / "api-tester" / "apis"
    if not d.exists():
        return {}
    out = {}
    for catalog in ("client.json", "acl.json"):
        text = (d / catalog).read_text(encoding="utf-8", errors="replace") if (d / catalog).exists() else ""
        ids = [i for i in re.findall(r'"id"\s*:\s*"([^"]*' + re.escape(endpoint) + r'[^"]*)"', text, flags=re.I)
               if not i.startswith("{")]
        if ids:
            out[catalog] = ids
    return out


def cmd_context(args):
    issue = tracker_call("get_issue", issue_number=args.issue)
    repo = args.repo or next((r for r, p in PROJECT_FOR_REPO.items() if p == issue["project"]), "gaia")
    text = strip_html(issue.get("description")) + "\n" + "\n".join(strip_html(c["comment"])
                                                                    for c in issue.get("comments") or [])
    paths, classes, endpoints = _candidates(issue.get("description", "") + text)
    found = resolve(repo, paths, classes, endpoints)
    if args.json:       # only the predicted files (Talos' overlap hold); empty when the text names none
        print(json.dumps({"issue": args.issue, "repo": repo, "files": sorted(found)}))
        return
    say(f"#{issue['issueNumber']} [{issue['status']}] {issue['subject']}  (repo {repo})")

    deps = sorted({int(n) for n in re.findall(r"(?:depends on|blocked by|after)[^#\n]{0,40}#(\d{3,5})", text, re.I)}
                  | {int(n) for n in re.findall(r"\b(?:agaia|aprom|ah|aa)-(\d{3,5})\b", text)} - {args.issue})
    if deps:
        say("\nreferenced issues:")
        for n in deps[:10]:
            try:
                d = tracker_call("get_issue", issue_number=n)
                say(f"  #{n} [{d['status']}] {d['subject']}")
            except Exception:
                say(f"  #{n} (not visible)")

    if not found:
        say("\nno files resolved from the issue text; search with Grep for its key terms")
    for f, why in sorted(found.items()):
        say(f"\n== {f}  ({why})")
        for line in outline(repo_dir(repo) / f):
            say(f"  {line}")
        tests = related_tests(repo, f)
        if tests:
            say("  tests: " + ", ".join(tests))
        log = git(repo, "log", "--oneline", "-3", "--", f, check=False)
        if log:
            say("  recent: " + " | ".join(log.splitlines()))
    for e in sorted(endpoints):
        cases = api_cases(repo, e)
        for catalog, ids in cases.items():
            say(f"\napi-tester {catalog} cases for {e} ({len(ids)}): " + ", ".join(ids[:20])
                + (" …" if len(ids) > 20 else ""))
    if repo == "gaia":
        say("\n== Gaia pack")
        for line in gaiapack.build(repo_dir(repo), text, endpoints):
            say(line)
    say("\nNext: read only the line ranges you need (Read offset/limit), not whole files.")
