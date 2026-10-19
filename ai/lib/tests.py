"""Run a repo's full test suite and print only summaries and unexpected failures.

Raw output goes to dev-tools/ai/state/test-<repo>-<suite>.log so it can be read on demand
instead of flooding the conversation.
"""

import re
import subprocess

from .common import P4M, STATE_DIR, WORKSPACE, config, say


def _run_logged(name, cmd, cwd=WORKSPACE):
    STATE_DIR.mkdir(exist_ok=True)
    log = STATE_DIR / f"test-{name}.log"
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    output = (result.stdout or "") + (result.stderr or "")
    log.write_text(output, encoding="utf-8")
    return result.returncode, output, log


def _phpunit(folder):
    code, out, log = _run_logged(f"gaia-phpunit-{folder.split('/')[-1]}",
                                 ["sh", str(P4M), "test", "phpunit", folder])
    summary = next((l.strip() for l in reversed(out.splitlines())
                    if l.startswith(("OK (", "Tests:", "FAILURES!", "ERRORS!"))), "no summary (see log)")
    failures = re.findall(r"^\d+\) (\S+)", out, flags=re.M)
    ok = summary.startswith("OK (")
    return ok, f"phpunit {folder}: {summary}", failures, log


def _api():
    _run_logged("gaia-prepare-test", ["sh", str(P4M), "db", "prepare-test"])
    code, out, log = _run_logged("gaia-api", ["sh", str(P4M), "test", "api", "--mode", "backend,acl,client"])
    summaries = re.findall(r"^Summary: .*$", out, flags=re.M)
    failures = re.findall(r"^FAIL (\S+)", out, flags=re.M)
    known = set(config()["knownFailures"]["gaia"]["api"])
    unexpected = [f for f in failures if f not in known]
    modes = ["backend", "acl", "client"]
    lines = [f"api {modes[i] if i < len(modes) else i}: {s[len('Summary: '):]}" for i, s in enumerate(summaries)]
    return not unexpected, "\n".join(lines) or "api: no summary (see log)", failures, log, known


def run_gaia():
    all_ok = True
    for folder in ("tests/libraries/security", "tests/libraries/utils"):
        ok, summary, failures, log = _phpunit(folder)
        say(summary)
        for f in failures:
            say(f"  FAIL {f}")
        all_ok &= ok
    ok, summary, failures, log, known = _api()
    say(summary)
    for f in failures:
        say(f"  FAIL {f}" + ("  (known)" if f in known else "  <-- unexpected"))
    all_ok &= ok
    return all_ok


def run_prometheus():
    cmd = ["docker", "exec", "-e", "TRAVIS=true", "-e", "CI=true", "prometheus-dev",
           "./node_modules/.bin/ember", "test"]
    code, out, log = _run_logged("prometheus-ember", cmd)
    passed = re.search(r"^# pass\s+(\d+)", out, flags=re.M)
    failed = re.search(r"^# fail\s+(\d+)", out, flags=re.M)
    failures = re.findall(r"^not ok \d+ \S+ - (.*)$", out, flags=re.M)
    cfg = config()["knownFailures"]["prometheus"]
    known = set(cfg.get("ember", []))
    fail_count = int(failed.group(1)) if failed else -1
    say(f"ember: pass {passed.group(1) if passed else '?'}, fail {fail_count} "
        f"(baseline {cfg.get('emberBaselineFailCount')})")
    unexpected = []
    for f in failures:
        is_known = f in known
        say(f"  FAIL {f}" + ("  (known)" if is_known else ""))
        if not is_known:
            unexpected.append(f)
    if known:
        return not unexpected
    return fail_count != -1 and fail_count <= cfg.get("emberBaselineFailCount", 0)


def run_hermes():
    code, out, log = _run_logged("hermes", ["sh", str(P4M), "test", "hermes"])
    tail = [l for l in out.splitlines() if re.search(r"(pass|fail|tests?)\b", l, flags=re.I)][-4:]
    for l in tail:
        say(f"hermes: {l.strip()}")
    return code == 0


def cmd_test(args):
    runner = {"gaia": run_gaia, "prometheus": run_prometheus, "hermes": run_hermes}.get(args.repo)
    if not runner:
        say(f"no test recipe for {args.repo}")
        return 2
    ok = runner()
    say(f"\nRESULT: {'PASS' if ok else 'FAIL'} (full logs in {STATE_DIR})")
    return 0 if ok else 1
