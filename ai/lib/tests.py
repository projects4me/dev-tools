"""Run a repo's test suites against the workspace checkout and print only summaries and new failures.

- Suites run through `p4m test`, which uses a container serving this checkout (never another folder's).
- A failure is "known" if it is in config.json knownFailures or failed on the base commit too (the
  name-based baseline, see baseline.py); anything else is unexpected.
- A suite passes only if every failure is known AND its exit code is explained by them: a crash, a parse
  miss or a targeted run that executed no test is a failure.
- `--changed` runs only the tests mapped from the branch's changed files (testmap.py); `ai ship` still
  needs a full run on the final tree (results.py).
- Raw output goes to dev-tools/ai/state/test-<repo>-<suite>.log.
"""

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import baseline, results, testmap
from .common import STATE_DIR, WORKSPACE, base_branch, config, git, p4m, repo_dir, say

PHPUNIT_FOLDERS = ("tests/libraries/security", "tests/libraries/utils")


@dataclass
class SuiteResult:
    suite: str
    summary: str
    failures: list = field(default_factory=list)
    log: Path = None
    rc: int = 0
    failed_count: int = None     # failures the runner itself reported, when it reports a count
    ran: int = None              # tests executed, when known


def _run_logged(name, cmd, cwd=WORKSPACE):
    STATE_DIR.mkdir(exist_ok=True)
    log = STATE_DIR / f"test-{name}.log"
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    output = (result.stdout or "") + (result.stderr or "")
    log.write_text(output, encoding="utf-8")
    return result.returncode, output, log


def _p4m(*args):
    return p4m(*args)


def _tag(src):
    """Log-name suffix, so a baseline run doesn't overwrite the branch run's log."""
    return "-baseline" if baseline.BASELINE_DIR in Path(src).resolve().parents else ""


def _int(m):
    return int(m.group(1)) if m else None


# --- suites --------------------------------------------------------------------------------------

def phpunit(folder, src):
    name = folder.split("/")[-1]
    code, out, log = _run_logged(f"gaia-phpunit-{name}{_tag(src)}",
                                 _p4m("test", "phpunit", folder, "--src", str(src)))
    summary = next((l.strip() for l in reversed(out.splitlines())
                    if l.startswith(("OK (", "Tests:", "FAILURES!", "ERRORS!"))), "no summary (see log)")
    failures = re.findall(r"^\d+\) (\S+)", out, flags=re.M)
    ran, failed = parse_phpunit_counts(summary)
    return SuiteResult(f"phpunit-{name}", f"phpunit {folder}: {summary}", failures, log, code, failed, ran)


def parse_phpunit_counts(summary):
    """(tests run, errors + failures) from PHPUnit's last line, or (None, None)."""
    ok = re.match(r"OK \((\d+) tests?", summary)
    if ok:
        return int(ok.group(1)), 0
    tests = re.search(r"Tests: (\d+)", summary)
    if not tests:
        return None, None
    bad = sum(int(m.group(1)) for k in ("Errors", "Failures") for m in [re.search(rf"{k}: (\d+)", summary)] if m)
    return int(tests.group(1)), bad


def api(src, api_filter=None):
    cmd = _p4m("test", "api", "--prepare", "--mode", "backend,acl,client", "--src", str(src))
    if api_filter:
        cmd += ["--filter", api_filter]
    code, out, log = _run_logged(f"gaia-api{_tag(src)}", cmd)
    summaries = re.findall(r"^Summary: (\d+) passed, (\d+) failed, (\d+) skipped", out, flags=re.M)
    modes = ["backend", "acl", "client"] if len(summaries) == 3 else [f"run {i + 1}" for i in range(len(summaries))]
    lines = [f"api {m}: {p} passed, {f} failed, {s} skipped" for m, (p, f, s) in zip(modes, summaries)]
    failed = sum(int(f) for _, f, _ in summaries) if summaries else None
    ran = sum(int(p) + int(f) for p, f, _ in summaries) if summaries else None
    return SuiteResult("api", "\n".join(lines) or "api: no summary (see log)",
                       re.findall(r"^FAIL (\S+)", out, flags=re.M), log, code, failed, ran)


def ember(src, qunit_filter=None):
    cmd = _p4m("test", "ember", "--src", str(src))
    if qunit_filter:
        cmd += ["--filter", qunit_filter]
    code, out, log = _run_logged(f"prometheus-ember{_tag(src)}", cmd)
    passed = _int(re.search(r"^# pass\s+(\d+)", out, flags=re.M))
    failed = _int(re.search(r"^# fail\s+(\d+)", out, flags=re.M))
    # "not ok 340 Chrome 97.0 - [41 ms] - Integration | Component | role/card: it renders"
    failures = [m.strip() for m in re.findall(r"^not ok \d+ .*? - (?:\[[^\]]*\] - )?(.*)$", out, flags=re.M)]
    if "No tests matched the filter" in out:
        return SuiteResult("ember", "ember: the filter matched no test (see log)", [], log, code or 1, None, 0)
    if passed is None and failed is None:
        tail = "\n".join(out.strip().splitlines()[-6:])
        return SuiteResult("ember", f"no summary (see log)\n{tail}", failures, log, code)
    return SuiteResult("ember", f"ember: pass {passed}, fail {failed}", failures, log, code, failed,
                       (passed or 0) + (failed or 0))


def hermes(src):
    code, out, log = _run_logged(f"hermes{_tag(src)}", _p4m("test", "hermes", "--src", str(src)))
    tail = [l.strip() for l in out.splitlines() if re.search(r"^# (tests|pass|fail)\b", l.strip())]
    failures = re.findall(r"^not ok \d+ - (.*)$", out, flags=re.M)
    failed = _int(re.search(r"^# fail (\d+)", out, flags=re.M))
    return SuiteResult("hermes", "hermes: " + (", ".join(tail) or "no summary (see log)"), failures, log, code,
                       failed, _int(re.search(r"^# tests (\d+)", out, flags=re.M)))


def devtools(src):
    lines, failures, code, ran_total = [], [], 0, 0
    for name, folder in (("talos", "talos/tests"), ("ai", "ai/tests"), ("installer", "installer/tests"),
                         ("nestor", "nestor/tests"), ("sim", "sim/tests"),
                         ("gaia-mcp", "gaia-mcp/tests")):
        if not (Path(src) / folder).exists():
            continue
        c, out, log = _run_logged(f"dev-tools-{name}", [sys.executable, "-m", "unittest", "discover", "-s", folder,
                                                        "-t", folder], cwd=src)
        ran = _int(re.search(r"^Ran (\d+) tests?", out, flags=re.M))
        ran_total += ran or 0
        lines.append(f"{name}: ran {ran if ran is not None else '?'}, {'OK' if c == 0 else 'FAILED'}")
        failures += [f"{name}:{m}" for m in re.findall(r"^(?:FAIL|ERROR): (\S+ \(\S+\))", out, flags=re.M)]
        code = code or c
    return SuiteResult("unittest", "unittest " + "; ".join(lines), failures, STATE_DIR, code, len(failures),
                       ran_total)


SUITES = {
    "gaia": [*(f"phpunit-{f.split('/')[-1]}" for f in PHPUNIT_FOLDERS), "api"],
    "prometheus": ["ember"],
    "hermes": ["hermes"],
    "dev-tools": ["unittest"],
}


def run_suite(repo, suite, src, targets=None):
    targets = targets or {}
    if suite.startswith("phpunit-"):
        return phpunit(next(f for f in PHPUNIT_FOLDERS if f.endswith(suite[len("phpunit-"):])), src)
    return {"api": lambda: api(src, targets.get("api")),
            "ember": lambda: ember(src, targets.get("ember")),
            "hermes": lambda: hermes(src),
            "unittest": lambda: devtools(src)}[suite]()


# --- targeting -----------------------------------------------------------------------------------

def targets_for(repo, src):
    """({suite: filter}, notes) for --changed. A suite without a filter runs in full."""
    base_ref = f"origin/{base_branch(repo)}"
    changed = testmap.changed_files(src, base_ref)
    if not changed:
        return {}, ["no changed files: running everything"]
    if repo == "prometheus":
        modules = testmap.test_modules(src)
        tests, unmapped = testmap.prometheus_tests(changed, modules)
        flt = testmap.qunit_filter(tests, modules)
        notes = [f"ember: {len(tests)} test module(s) from {len(changed)} changed file(s)" if flt
                 else "ember: no test mapped from the changed files: running everything"]
    elif repo == "gaia":
        flt, terms, unmapped = testmap.gaia_api_filter(src, base_ref, changed)
        notes = [f"api filter: {flt}" if flt else "api: nothing mapped: running everything",
                 "phpunit: always both folders (fast)"]
    else:
        return {}, [f"{repo}: no targeting rules, running everything"]
    if unmapped:
        notes.append("unmapped (covered only by the full run): " + ", ".join(unmapped[:12])
                     + (" …" if len(unmapped) > 12 else ""))
    return ({"ember" if repo == "prometheus" else "api": flt} if flt else {}), notes


# --- classification ------------------------------------------------------------------------------

def known_failures(repo, suite):
    known = config().get("knownFailures", {}).get(repo, {})
    return set(known.get(suite, []) or known.get(suite.split("-")[0], []) or [])


def judge(r, known):
    """(ok, unexpected failures). Known-by-name is necessary but not sufficient: the exit code must be
    explained by the failures the runner reported, and a run must have executed something."""
    unexpected = [f for f in r.failures if f not in known]
    if r.summary.startswith("no summary") or unexpected:
        return False, unexpected
    if r.ran == 0:
        return False, unexpected
    if r.rc and not r.failures:
        return False, unexpected                      # non-zero exit with no failure to explain it
    if r.rc and r.failed_count is not None and r.failed_count != len(r.failures):
        return False, unexpected                      # the runner counted failures we couldn't name
    if r.rc and r.failed_count is None:
        return False, unexpected
    return True, unexpected


# --- commands ------------------------------------------------------------------------------------

def cmd_test(args):
    repo = args.repo
    if repo == "ares":
        say("ares has no test suite yet (see ares/CLAUDE.md): ship with `ai ship ares <issue> <spec> --no-tests "
            "\"<how you checked it>\"`")
        return 2
    if repo not in SUITES:
        say(f"no test recipe for {repo}")
        return 2
    src = repo_dir(repo)
    targets, notes = (targets_for(repo, src) if args.changed else ({}, []))
    if args.filter:
        targets = {"ember" if repo == "prometheus" else "api": args.filter}
        notes = [f"filter: {args.filter}"]
    for n in notes:
        say(n)
    full = not targets
    runs = [run_suite(repo, s, src, targets) for s in SUITES[repo]]

    sha = git(repo, "merge-base", "HEAD", f"origin/{base_branch(repo)}", check=False) or ""
    base = baseline.load(repo, sha) if sha else {}
    suspects = [r.suite for r in runs
                if set(r.failures) - known_failures(repo, r.suite) - set(base.get(r.suite, []))]
    if suspects and sha and not args.no_baseline and repo != "dev-tools":
        base = baseline.require(repo, sha, suspects, lambda suite, wt: run_suite(repo, suite, wt))

    ok, lines = True, []
    for r in runs:
        known = known_failures(repo, r.suite) | set(base.get(r.suite, []))
        suite_ok, _ = judge(r, known)
        lines.append(r.summary + ("" if suite_ok or r.summary.startswith("no summary") else
                                  f"  (exit {r.rc}; see {r.log})"))
        for f in r.failures:
            lines.append(f"  FAIL {f}" + ("  (known)" if f in known else "  <-- unexpected"))
        ok &= suite_ok
    for l in lines:
        say(l)
    verdict = "PASS" if ok else "FAIL"
    say(f"\nRESULT: {verdict}{'' if full else ' (targeted run; ai ship needs a full run)'} "
        f"(full logs in {STATE_DIR})")
    results.record(repo, ok, full, lines + [f"RESULT: {verdict}"])
    return 0 if ok else 1


def cmd_baseline(args):
    """Record the failing tests on the base commit now (normally done on demand by `ai test`)."""
    repo = args.repo
    git(repo, "fetch", "-q", "origin")
    sha = git(repo, "rev-parse", f"origin/{base_branch(repo)}")
    if args.force:
        (STATE_DIR / f"baseline-{repo}.json").unlink(missing_ok=True)
    base = baseline.require(repo, sha, SUITES[repo], lambda suite, wt: run_suite(repo, suite, wt))
    for suite, names in base.items():
        say(f"{suite}: {len(names)} failing on {sha[:8]}")
        for n in names:
            say(f"  {n}")
