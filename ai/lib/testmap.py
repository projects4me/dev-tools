"""Map a branch's changed files to the tests that cover them, so a fix can be checked in seconds.

The rules are fixed and visible (no guessing by a model): anything they can't map is reported as
"unmapped", and the full suite still runs once before shipping (`ai ship` requires a full run).
"""

import re
import subprocess
from pathlib import Path

# Prometheus app folders whose files have a unit or integration test under the same path.
_UNIT_KINDS = ("models", "services", "utils", "adapters", "serializers", "routes", "controllers", "transforms")
_INTEGRATION_KINDS = ("components", "helpers", "modifiers")
_ROUTE_KINDS = ("controllers", "routes", "templates")


def changed_files(checkout, base_ref):
    """Files changed on this branch against `base_ref` (committed, staged, unstaged and untracked)."""
    def lines(*cmd):
        return subprocess.run(["git", "-C", str(checkout), *cmd], capture_output=True, text=True,
                              encoding="utf-8", errors="replace").stdout.splitlines()
    merge_base = (lines("merge-base", "HEAD", base_ref) or [base_ref])[0]
    files = set(lines("diff", "--name-only", merge_base)) | set(lines("ls-files", "-o", "--exclude-standard"))
    return sorted(f for f in files if f)


def added_lines(checkout, base_ref, path):
    """Lines added to `path` on this branch (for catalogs where new case ids are the signal)."""
    merge_base = subprocess.run(["git", "-C", str(checkout), "merge-base", "HEAD", base_ref],
                                capture_output=True, text=True).stdout.strip() or base_ref
    diff = subprocess.run(["git", "-C", str(checkout), "diff", "-U0", merge_base, "--", path],
                          capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    return [l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]


# --- prometheus ----------------------------------------------------------------------------------

def test_modules(checkout):
    """{test file: QUnit module name} for the Gherkin features and *-test.js files."""
    checkout = Path(checkout)
    modules = {}
    for f in checkout.glob("tests/**/*.feature"):
        m = re.search(r"^\s*Feature:\s*(.+?)\s*$", f.read_text(encoding="utf-8", errors="replace"), re.M)
        if m:
            modules[f.relative_to(checkout).as_posix()] = f"Feature: {m.group(1)}"
    for f in checkout.glob("tests/**/*-test.js"):
        m = re.search(r"""\bmodule\(\s*(['"`])(.+?)\1""", f.read_text(encoding="utf-8", errors="replace"))
        if m:
            modules[f.relative_to(checkout).as_posix()] = m.group(2)
    return modules


def _stem(path):
    name = Path(path).name
    for suffix in ("-steps.js", "-test.js", ".feature", ".js", ".hbs", ".ts", ".scss"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return Path(path).stem


def prometheus_tests(changed, modules):
    """(test files to run, unmapped changed files)."""
    tests, unmapped = set(), []
    paths = list(modules)
    for f in changed:
        hits = set()
        if f in modules:
            hits.add(f)
        parts = f.split("/")
        if f.startswith("tests/acceptance/steps/") and len(parts) >= 5:      # steps/<area>/<x>-steps.js
            area, stem = parts[3], _stem(f)
            hits |= {p for p in paths if p.startswith(f"tests/acceptance/{area}/")
                     and (_stem(p) == stem or stem == area)}
        elif f.startswith("app/") and len(parts) >= 3:
            kind, rest = parts[1], "/".join(parts[2:])
            rest = re.sub(r"\.(js|ts|hbs)$", "", rest)
            if kind == "templates" and rest.startswith("components/"):
                kind, rest = "components", rest[len("components/"):]
            if kind in _INTEGRATION_KINDS:
                hits |= {p for p in paths if p == f"tests/integration/{kind}/{rest}-test.js"}
            if kind in _UNIT_KINDS:
                hits |= {p for p in paths if p == f"tests/unit/{kind}/{rest}-test.js"}
            if kind in _ROUTE_KINDS:
                # app/controllers/app/settings/email.js -> features under .../settings/email(.feature|/)
                segs = rest.split("/")
                tail = "/".join(segs[-2:]) if len(segs) >= 2 else segs[-1]
                hits |= {p for p in paths if p.startswith("tests/acceptance/")
                         and (p.endswith(f"/{tail}.feature") or f"/{tail}/" in p)}
        elif f.startswith("mirage/"):
            stem = _stem(f)
            hits |= {p for p in paths if p.startswith("tests/acceptance/") and stem in p}
        if not hits:                       # last resort: a test file with the same name
            stem = _stem(f)
            hits |= {p for p in paths if _stem(p) == stem}
        if hits:
            tests |= hits
        else:
            unmapped.append(f)
    return sorted(tests), unmapped


def qunit_filter(test_files, modules):
    """A QUnit /regex/i filter matching exactly these modules, or None (case-insensitive: ember test
    lowercases the filter it passes to QUnit)."""
    names = sorted({modules[t] for t in test_files if t in modules})
    if not names:
        return None
    return "/(" + "|".join(re.sub(r"([\\^$.|?*+()\[\]{}/])", r"\\\1", n) for n in names) + ")/i"


# --- gaia ----------------------------------------------------------------------------------------

_RESOURCE_PATTERNS = (
    r"^app/api/v1/controllers/(\w+)Controller\.php$",
    r"^app/api/v1/models/(\w+)\.php$",
    r"^app/metadata/model/(\w+)\.php$",
)
_CATALOGS = ("tools/api-tester/apis/client.json", "tools/api-tester/apis/acl.json")


def gaia_api_filter(checkout, base_ref, changed):
    """(api-tester --filter value or None, mapped terms, unmapped files). The filter matches case ids/paths."""
    terms, unmapped = set(), []
    for f in changed:
        term = next((m.group(1).lower() for p in _RESOURCE_PATTERNS for m in [re.match(p, f)] if m), None)
        if term:
            terms.add(term)
        elif f in _CATALOGS:
            # case ids only: record ids inside request bodies are UUIDs, placeholders are {…}
            ids = [m.group(1) for l in added_lines(checkout, base_ref, f)
                   for m in [re.search(r'"id"\s*:\s*"([^"{]+)"', l)]
                   if m and not re.fullmatch(r"[0-9a-f-]{20,}", m.group(1))]
            terms |= set(ids)
            if not ids:
                unmapped.append(f)
        elif f.startswith(("tests/", "tools/api-tester/src/", "docs/")) or f.endswith(".md"):
            continue                       # phpunit runs in full; tester code/docs need no case filter
        else:
            unmapped.append(f)
    return ("|".join(sorted(terms)) if terms else None), sorted(terms), unmapped


def backend_catalog_stale(changed):
    """True when a changed file feeds the generated api-tester backend.json (metadata or routes)."""
    return any(f.startswith("app/metadata/model/") or f == "core/config/routes.php" for f in changed)
