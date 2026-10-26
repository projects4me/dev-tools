"""`ai doctor gaia`: is the Gaia test environment sound? One call, each failing check with its fix command.

Each check takes an Env (the facts about the machine) and returns (ok, detail, fix). The real Env asks docker,
git and Postgres; tests pass a fake one. `ai start` runs this for Gaia issues and only reports problems.
"""

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from . import baseline
from .common import STATE_DIR, base_branch, git, ody, p4m, repo_dir, run, say, succeeds

HARNESS = "tools/api-tester/harness"
BACKEND_JSON = "tools/api-tester/apis/backend.json"
GENERATOR = "tools/api-tester/bin/generate-backend-apis.php"
PG_CONTAINER = "postgres-dev"
PG_USER = "nouman"
SEED_MARKER = "api-test-user-0001"      # the user prepare-test-db.sh seeds
SEED_TABLE = "users"


@dataclass
class Result:
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""

    def as_dict(self):
        return {"check": self.name, "ok": self.ok, "detail": self.detail, "fix": self.fix}


class Env:
    """Facts about this machine; every method may raise, which the check turns into a failed result."""

    def __init__(self, repo="gaia"):
        self.repo = repo
        self.src = repo_dir(repo)

    def mount_state(self):
        """(serving checkout?, note): does the shared gaia-test serve this checkout, else can a throwaway work."""
        sys.path.insert(0, str(Path(p4m()[1]).parent))
        import testenv  # noqa: E402  (dev-tools/installer/testenv.py)
        shared = testenv.inspect("gaia-test")
        if not shared:
            return False, "gaia-test does not exist"
        source = testenv.mount_source(shared, "/var/www/html")
        if testenv._running(shared) and source and testenv.norm(source) == testenv.norm(self.src):
            return True, "gaia-test serves this checkout"
        return False, f"gaia-test serves {source or 'nothing'}"

    def crlf_scripts(self):
        """Harness shell scripts with CRLF line endings."""
        return [p.name for p in sorted((self.src / HARNESS).glob("*.sh")) if b"\r\n" in p.read_bytes()]

    def psql(self, db, sql):
        return run(["docker", "exec", "-i", PG_CONTAINER, "psql", "-U", PG_USER, "-d", db, "-Atc", sql])

    def seed_marker_rows(self):
        return int(self.psql("pr4m_test", f"SELECT count(*) FROM {SEED_TABLE} WHERE id = '{SEED_MARKER}'") or 0)

    def table_counts(self):
        """(tables in pr4m, tables in pr4m_test)."""
        sql = "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
        return int(self.psql("pr4m", sql)), int(self.psql("pr4m_test", sql))

    def backend_drift(self):
        """Regenerate backend.json in the test container; True if the file changed."""
        path = self.src / BACKEND_JSON
        before = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        run(p4m("test", "api", "--mode", "backend", "--filter", "no-such-case-doctor", "--regenerate-backend"),
            check=False)
        after = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        return before != after

    def baseline_recorded(self):
        sha = git(self.repo, "merge-base", "HEAD", f"origin/{base_branch(self.repo)}", check=False)
        return bool(sha) and "api" in baseline.load(self.repo, sha)

    def odysseus_reachable(self):
        return succeeds(["git", "ls-remote", "--exit-code", "-q", "odysseus", "HEAD"], cwd=self.src)


# --- checks --------------------------------------------------------------------------------------

def check_mount(env):
    serves, note = env.mount_state()
    if serves:
        return Result("test-container", True, note)
    return Result("test-container", True, f"{note}; a throwaway container is used for this checkout "
                                          "(`ai test` creates it)")


def check_crlf(env):
    bad = env.crlf_scripts()
    if not bad:
        return Result("crlf", True, "harness scripts use LF")
    return Result("crlf", False, "CRLF in " + ", ".join(bad), "dev-tools/installer/p4m repos fix-eol")


def check_seed(env):
    if env.seed_marker_rows() < 1:
        return Result("test-db", False, "pr4m_test is not seeded", "dev-tools/installer/p4m db prepare-test")
    prod, test = env.table_counts()
    if prod != test:
        return Result("test-db", False, f"pr4m has {prod} tables, pr4m_test {test}: seeded before a migration",
                      "dev-tools/installer/p4m db migrate && dev-tools/installer/p4m db prepare-test")
    return Result("test-db", True, "pr4m_test seeded and matches pr4m")


def check_backend(env):
    if env.backend_drift():
        return Result("backend.json", False, "regeneration changed backend.json (it was stale; now rewritten)",
                      "review `git diff gaia/" + BACKEND_JSON + "`; commit it with the metadata/route change")
    return Result("backend.json", True, "regenerates with no drift")


def check_baseline(env):
    if env.baseline_recorded():
        return Result("baseline", True, "api baseline recorded for the merge-base")
    return Result("baseline", False, "no api baseline for the merge-base (first unexpected failure will "
                                     "trigger a baseline run)", "dev-tools/ai/ai baseline gaia")


def check_remote(env):
    if env.odysseus_reachable():
        return Result("odysseus-remote", True, "reachable")
    return Result("odysseus-remote", False, "git ls-remote odysseus failed",
                  "dev-tools/odysseus/odysseus git remote -v  (check the remote and network)")


CHECKS = [check_mount, check_crlf, check_seed, check_backend, check_baseline, check_remote]


def diagnose(env, checks=None):
    results = []
    for check in checks or CHECKS:
        try:
            results.append(check(env))
        except Exception as e:  # a check that cannot run is itself a finding
            name = check.__name__.removeprefix("check_")
            results.append(Result(name, False, f"could not check: {str(e).splitlines()[0][:160] if str(e) else e!r}",
                                  "dev-tools/installer/p4m status"))
    return results


def render(results, quiet_when_ok=False):
    """Terse lines: failures with their fix, one summary line."""
    bad = [r for r in results if not r.ok]
    if not bad:
        return "" if quiet_when_ok else "doctor gaia: all " + str(len(results)) + " checks OK"
    lines = []
    for r in bad:
        lines.append(f"FAIL {r.name}: {r.detail}")
        lines.append(f"  fix: {r.fix}")
    lines.append(f"doctor gaia: {len(bad)} of {len(results)} checks failed")
    return "\n".join(lines)


def cmd_doctor(args):
    results = diagnose(Env(args.repo))
    if args.json:
        say(json.dumps([r.as_dict() for r in results], indent=2))
    else:
        say(render(results))
    return 0 if all(r.ok for r in results) else 1


def preflight(repo):
    """The `ai start` hook: Gaia only, silent when healthy, never fails the start."""
    if repo != "gaia":
        return
    try:
        text = render(diagnose(Env(repo)), quiet_when_ok=True)
    except Exception as e:
        text = f"doctor gaia could not run: {e}"
    if text:
        say(text)
