"""Test containers that run a suite against a given checkout (the workspace's by default).

The shared containers (gaia-test, hermes, prometheus-dev) serve whichever folder `p4m up` was run from.
Several workspaces can exist on one machine, so `p4m test` checks what the shared container really runs:

- hermes bind-mounts its source. If the shared container mounts another folder, a throwaway container is
  started from the same image, network, env and mounts, with the source mount (and any bind under it)
  pointed at the checkout under test. It is kept for reuse and keyed by the checkout path.
- gaia always runs its tests in its own container per checkout, with the source copied in (only changed
  files on later runs) instead of bind-mounted: on Docker Desktop every PHP file read through a Windows bind
  mount crosses the file-sharing layer (~1 s per request instead of ~30 ms). It reuses gaia-test's image,
  network, env and volumes, with OPcache on and Xdebug off. Reports are copied back to the checkout.
  In a test slot (see testslots.py) it is keyed by the slot instead (`p4m-test-gaia-slot<k>`), uses the slot's
  database (DB_NAME), and is emptied and re-filled when another checkout is synced into it.
- prometheus has no source mount (a Windows bind mount makes the Ember build crawl). Its test container
  gets the source copied in (only changed files on later runs), keeps an incremental
  `ember build --environment=test --watch` running, and runs `ember test --path` on the result.

Containers are labelled `p4m.test` and removed by `p4m test clean`.
"""

import hashlib
import io
import json
import os
import queue
import re
import subprocess
import tarfile
import threading
import sys
import time
from pathlib import Path

IS_WINDOWS = os.name == "nt"
LABEL = "p4m.test"
BUILD_DIR = "/tmp/p4m-test-dist"
BUILD_LOG = "/tmp/p4m-build.log"
BUILD_PID = "/tmp/p4m-build.pid"    # pid of the watch build p4m started (it execs ember, so pid == ember)
# Pids (one per line) with the build log open whose process group is not the recorded watch build's.
STALE_SCRIPT = f"""keep=$(cat {BUILD_PID} 2>/dev/null); kg=
[ -n "$keep" ] && kg=$(awk '{{print $5}}' /proc/$keep/stat 2>/dev/null)
for d in /proc/[0-9]*; do
  p=${{d#/proc/}}; [ "$p" = "$$" ] && continue
  ls -l $d/fd 2>/dev/null | grep -q {BUILD_LOG} || continue
  g=$(awk '{{print $5}}' $d/stat 2>/dev/null)
  [ -n "$kg" ] && [ "$g" = "$kg" ] && continue
  echo $p
done"""
# The recorded watch build is alive and still ember. Its title is plain `ember` (it renames itself), so
# `pgrep -f 'ember build'` only finds builds started the old way (under `sh -c '... ember build ...'`).
RECORDED_BUILDER = (f"p=$(cat {BUILD_PID} 2>/dev/null); [ -n \"$p\" ] && kill -0 \"$p\" 2>/dev/null "
                    f"&& tr '\\0' ' ' < /proc/$p/cmdline 2>/dev/null | grep -q ember")
# [e]mber: the pattern must not match the sh -c command line running it
BUILDER_RUNNING_SCRIPT = f"{{ {RECORDED_BUILDER}; }} || pgrep -f '[e]mber build' >/dev/null"
STOP_BUILDER_SCRIPT = (f"{{ {RECORDED_BUILDER} && kill \"$p\"; }}; pkill -f '[e]mber build'; "
                       f"rm -f {BUILD_PID}; sleep 1")
TEST_TIMEOUT = 900        # seconds for one `ember test`
STUCK_GRACE = 20          # seconds after a hang marker before the run is stopped (the other tests may still finish)
# Output after which testem never reports completion: no test matched, or QUnit/TestLoader failed globally.
HANG_MARKER = re.compile(r"No tests matched the filter|Global failure|TestLoader Failures", re.I)
BUILD_DONE = re.compile(r"Build successful|Built project successfully|Build Error|Build failed", re.I)
BUILD_FAILED = re.compile(r"Build Error|Build failed", re.I)
# What `ember build --watch` rebuilds on: a sync that touches none of these won't produce a new build.
WATCHED = ("app/", "tests/", "mirage/", "translations/", "config/", "public/", "vendor/", "lib/", "addon/")
# Read once when the build starts: a change needs a fresh builder.
RESTART_ON = ("ember-cli-build.js", "package.json", "testem.js", "config/environment.js")
# Not copied into the prometheus test container: generated, large or irrelevant to the tests.
SYNC_EXCLUDE = ("node_modules/", "dist/", "tmp/", "docs/", "coverage/", ".git/", ".idea/")


class TestEnvError(Exception):
    """The test container could not be prepared."""


# --- docker helpers -----------------------------------------------------------------------------

def _docker(*args, check=True, input=None, text=True):
    r = subprocess.run(["docker", *args], capture_output=True, input=input, text=text,
                       **({"encoding": "utf-8", "errors": "replace"} if text else {}))
    if check and r.returncode != 0:
        detail = (r.stderr or r.stdout or "")
        detail = detail if isinstance(detail, str) else detail.decode("utf-8", "replace")
        raise TestEnvError(f"docker {' '.join(args[:3])} failed: {detail.strip()[-800:]}")
    return r


def inspect(name):
    """`docker inspect` of one container, or None if it doesn't exist."""
    r = _docker("inspect", "--type", "container", name, check=False)
    if r.returncode != 0:
        return None
    data = json.loads(r.stdout)
    return data[0] if data else None


def host_path(source):
    """A bind-mount source as reported by docker, as a host path usable in `-v` (Docker Desktop reports
    some Windows binds as /run/desktop/mnt/host/<drive>/...)."""
    s = str(source).replace("\\", "/")
    m = re.match(r"^/run/desktop/mnt/host/([a-zA-Z])/(.*)$", s)
    if m:
        return f"{m.group(1).upper()}:/{m.group(2)}"
    return s


def norm(path):
    """A path in comparable form (forward slashes, no trailing slash, case-insensitive on Windows)."""
    s = host_path(path).rstrip("/")
    return s.lower() if IS_WINDOWS else s


def mount_source(info, destination):
    """Host source of the bind mount at `destination` in a container, or None."""
    for m in (info or {}).get("Mounts", []):
        if m.get("Destination") == destination and m.get("Type") == "bind":
            return host_path(m["Source"])
    return None


def _key(path):
    return hashlib.sha1(norm(path).encode("utf-8")).hexdigest()[:8]


def _running(info):
    return bool(info) and info.get("State", {}).get("Running")


# --- bind-mounted services (gaia-test, hermes) ---------------------------------------------------

class BindTestContainer:
    """A container that runs `base`'s image with its source mount pointed at `checkout`."""

    def __init__(self, base, checkout, dest, wait_port=None):
        self.base = base
        self.checkout = Path(checkout).resolve()
        self.dest = dest
        self.wait_port = wait_port

    def resolve(self):
        """(container name, note): the shared container if it already serves `checkout`, else a
        throwaway one (created or reused)."""
        shared = inspect(self.base)
        if not shared:
            raise TestEnvError(f"{self.base} does not exist - `p4m up` first (its image and settings are reused)")
        source = mount_source(shared, self.dest)
        if _running(shared) and source and norm(source) == norm(self.checkout):
            return self.base, ""
        name = f"p4m-test-{self.base}-{_key(self.checkout)}"
        own = inspect(name)
        if own and _running(own) and norm(mount_source(own, self.dest) or "") == norm(self.checkout) \
                and own["Config"]["Image"] == shared["Config"]["Image"]:
            return name, f"{self.base} serves {source or 'nothing'}; using {name} for {self.checkout}"
        if own:
            _docker("rm", "-f", name)
        self._create(name, shared, source)
        return name, f"{self.base} serves {source or 'nothing'}; started {name} for {self.checkout}"

    def _create(self, name, shared, old_source):
        args = ["run", "-d", "--name", name, "--label", f"{LABEL}={norm(self.checkout)}"]
        networks = list((shared.get("NetworkSettings") or {}).get("Networks") or {})
        if networks:
            args += ["--network", networks[0]]
        for m in shared.get("Mounts", []):
            dest = m["Destination"]
            mode = "" if m.get("RW", True) else ":ro"
            if m["Type"] == "volume":
                args += ["-v", f"{m['Name']}:{dest}{mode}"]
            elif m["Type"] == "bind":
                src = host_path(m["Source"])
                if dest == self.dest:
                    src = self.checkout.as_posix()
                elif old_source and norm(src).startswith(norm(old_source) + "/"):
                    src = (self.checkout / src[len(old_source.rstrip("/")) + 1:]).as_posix()
                args += ["-v", f"{src}:{dest}{mode}"]
        # Same process as the shared container, including compose-level command/entrypoint overrides.
        cfg = shared["Config"]
        entrypoint, command = list(cfg.get("Entrypoint") or []), list(cfg.get("Cmd") or [])
        if entrypoint:
            args += ["--entrypoint", entrypoint[0]]
            command = entrypoint[1:] + command
        if cfg.get("WorkingDir"):
            args += ["-w", cfg["WorkingDir"]]
        if cfg.get("User"):
            args += ["-u", cfg["User"]]
        env_file = Path(os.environ.get("TEMP", "/tmp")) / f"{name}.env"
        env_file.write_text("\n".join(cfg.get("Env") or []) + "\n", encoding="utf-8")
        try:
            _docker(*args, "--env-file", str(env_file), cfg["Image"], *command)
        finally:
            env_file.unlink(missing_ok=True)
        for extra in networks[1:]:
            _docker("network", "connect", extra, name, check=False)
        if self.wait_port:
            probe = (f"for i in $(seq 1 60); do (exec 3<>/dev/tcp/127.0.0.1/{self.wait_port}) 2>/dev/null "
                     f"&& exit 0; sleep 0.5; done; exit 1")
            if _docker("exec", name, "bash", "-c", probe, check=False).returncode != 0:
                raise TestEnvError(f"{name} did not open port {self.wait_port} within 30s "
                                   f"(`docker logs {name}`)")


# --- copied-source containers (gaia, prometheus) -------------------------------------------------

def _tracked_files(checkout, exclude=SYNC_EXCLUDE):
    """Tracked plus untracked-but-not-ignored files of a checkout, minus the `exclude` prefixes."""
    out = subprocess.run(["git", "-C", str(checkout), "ls-files", "-co", "--exclude-standard", "-z"],
                         capture_output=True, check=True).stdout.decode("utf-8", "replace")
    return [f for f in out.split("\0") if f and not f.startswith(exclude)]


def _safe_member(name):
    """A relative tar member name that stays inside the folder it is extracted to."""
    parts = name.replace("\\", "/").split("/")
    return bool(name) and not name.startswith("/") and ":" not in parts[0] and ".." not in parts


class CopiedSourceContainer:
    """A container holding a copy of a checkout at `root`, kept current by an incremental sync."""

    root = "/"
    exclude = SYNC_EXCLUDE
    owner = None            # user the copied files belong to (None: the container's default user)

    def __init__(self, checkout, state_dir, prefix):
        self.checkout = Path(checkout).resolve()
        self.name = f"{prefix}-{_key(self.checkout)}"
        self.manifest_file = Path(state_dir) / f"{self.name}.manifest.json"

    def sync(self):
        """Copy new/changed files in and delete removed ones. Returns the paths changed."""
        old = json.loads(self.manifest_file.read_text(encoding="utf-8")) if self.manifest_file.exists() else {}
        new, changed = {}, []
        for rel in _tracked_files(self.checkout, self.exclude):
            p = self.checkout / rel
            try:
                st = p.stat()
            except FileNotFoundError:          # tracked but deleted in the working tree
                continue
            stamp = [st.st_mtime_ns, st.st_size]
            new[rel] = stamp
            if old.get(rel) != stamp:
                changed.append(rel)
        removed = sorted(set(old) - set(new))
        user = ["-u", self.owner] if self.owner else []
        if changed:
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tar:
                for rel in changed:
                    tar.add(str(self.checkout / rel), arcname=rel, recursive=False)
            keep_owner = ["--no-same-owner"] if self.owner else []
            _docker("exec", "-i", *user, self.name, "tar", "-x", *keep_owner, "-C", self.root,
                    input=buf.getvalue(), text=False)
        for i in range(0, len(removed), 200):
            _docker("exec", *user, self.name, "rm", "-f", "--", *[f"{self.root}/{r}" for r in removed[i:i + 200]])
        self.manifest_file.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_file.write_text(json.dumps(new), encoding="utf-8")
        return changed + removed

    def fetch(self, pattern):
        """Copy the container's files matching a shell glob relative to `root` (e.g. `output/*.json`) back
        into the checkout. Returns the relative paths copied. `pattern` goes into a shell unquoted: pass
        only trusted constants."""
        script = f'cd {self.root} && set -- {pattern} && [ -e "$1" ] || exit 0; tar -cf - -- "$@"'
        data = _docker("exec", self.name, "sh", "-c", script, text=False).stdout
        copied = []
        if not data:
            return copied
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
            for m in tar.getmembers():
                if not m.isfile() or not _safe_member(m.name):
                    continue
                target = self.checkout / m.name
                if not target.resolve().is_relative_to(self.checkout):   # a symlinked folder in the checkout
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(tar.extractfile(m).read())
                copied.append(m.name)
        return copied


# --- gaia ----------------------------------------------------------------------------------------

GAIA_ROOT = "/var/www/html"
# vendor/ is gaia-test's volume; the rest isn't needed to run Gaia or its tests.
GAIA_SYNC_EXCLUDE = (".git/", ".idea/", ".cursor/", "vendor/")
# Folders Gaia writes at runtime, mostly git-ignored (so never copied): created and kept writable.
GAIA_WRITABLE = ("logs", "logs/tests", "output", "filesystem", "filesystem/uploads", "filesystem/img",
                 "filesystem/img/user", "filesystem/translations", "filesystem/tmp")
# Written into the test container only (gaia-dev keeps its settings). Timestamps are checked on every
# request, so the next request after a sync runs the new code.
GAIA_TEST_INI = ("zend_extension=opcache", "opcache.enable=1", "opcache.validate_timestamps=1",
                 "opcache.revalidate_freq=0", "opcache.memory_consumption=128",
                 "opcache.max_accelerated_files=20000")
GAIA_TEST_INI_FILE = "/usr/local/etc/php/conf.d/zz-p4m-test.ini"
REPORTS = "output/api-tester-*-report.json"


class GaiaTestContainer(CopiedSourceContainer):
    """gaia-test's image, network, env and volumes, with the checkout copied in instead of bind-mounted."""

    root = GAIA_ROOT
    exclude = GAIA_SYNC_EXCLUDE
    owner = "www-data"      # as in the image, so Apache can write where Gaia expects to

    def __init__(self, checkout, state_dir, base="gaia-test", slot=None, db_name=None):
        super().__init__(checkout, state_dir, "p4m-test-gaia")
        self.base = base
        self.slot = slot
        self.db_name = db_name          # overrides gaia-test's DB_NAME (a slot's database)
        if slot:
            self.name = f"p4m-test-gaia-slot{slot}"
            self.manifest_file = Path(state_dir) / f"{self.name}.manifest.json"
        self.checkout_file = Path(state_dir) / f"{self.name}.checkout"

    @property
    def label(self):
        """`p4m.test` label value: the checkout, or `slot<k>` for a slot (it serves any checkout)."""
        return f"slot{self.slot}" if self.slot else norm(self.checkout)

    def ensure(self, fresh=False):
        """Create the container unless one is running from gaia-test's current image. Returns True if it
        was (re)created (the next sync copies everything)."""
        shared = inspect(self.base)
        if not shared:
            raise TestEnvError(f"{self.base} does not exist - `p4m up` first (its image and settings are reused)")
        own = inspect(self.name)
        if own and not fresh and _running(own) and own.get("Image") == shared.get("Image"):
            if self._holds_checkout():
                return False
            self._empty()           # a slot that last ran another checkout: copy this one in from scratch
            return True
        if own:
            _docker("rm", "-f", self.name)
        self._create(shared)
        self._empty()               # the image holds an old copy of the source
        return True

    def _holds_checkout(self):
        """Whether the container's tree is this checkout's (a slot's is whichever checkout ran there last)."""
        if not self.slot:
            return True
        try:
            return self.checkout_file.read_text(encoding="utf-8").strip() == norm(self.checkout)
        except OSError:
            return False

    def _empty(self):
        """Start from an empty tree (vendor is a volume); the next sync copies everything."""
        _docker("exec", self.name, "sh", "-c",
                f"find {self.root} -mindepth 1 -maxdepth 1 ! -name vendor -exec rm -rf {{}} +")
        self.manifest_file.unlink(missing_ok=True)
        self.checkout_file.parent.mkdir(parents=True, exist_ok=True)
        self.checkout_file.write_text(norm(self.checkout), encoding="utf-8")

    def run_args(self, shared):
        """(`docker run` arguments before the image, image and command) for a container like `shared`,
        without its source mount."""
        args = ["run", "-d", "--name", self.name, "--label", f"{LABEL}={self.label}"]
        networks = list((shared.get("NetworkSettings") or {}).get("Networks") or {})
        if networks:
            args += ["--network", networks[0]]
        old_source = mount_source(shared, self.root)
        for m in shared.get("Mounts", []):
            dest = m["Destination"]
            mode = "" if m.get("RW", True) else ":ro"
            if m["Type"] == "volume":
                args += ["-v", f"{m['Name']}:{dest}{mode}"]
            elif m["Type"] == "bind" and dest != self.root:
                src = host_path(m["Source"])
                if old_source and norm(src).startswith(norm(old_source) + "/"):
                    src = (self.checkout / src[len(old_source.rstrip("/")) + 1:]).as_posix()
                args += ["-v", f"{src}:{dest}{mode}"]
        cfg = shared["Config"]
        if cfg.get("User"):
            args += ["-u", cfg["User"]]
        args += ["-w", cfg.get("WorkingDir") or self.root, "--entrypoint", "sh"]
        # gaia-test's process, after writing the test-only PHP ini
        ini = "\\n".join(GAIA_TEST_INI)
        command = ["-c", f"printf '{ini}\\n' > {GAIA_TEST_INI_FILE} && exec \"$@\"", "sh",
                   *(cfg.get("Entrypoint") or []), *(cfg.get("Cmd") or [])]
        return args, cfg["Image"], command

    def env(self, shared):
        """gaia-test's environment with Xdebug off (it only slows the suite down), on the slot's database."""
        dropped = ("XDEBUG_MODE=",) + (("DB_NAME=",) if self.db_name else ())
        return [e for e in shared["Config"].get("Env") or [] if not e.startswith(dropped)] \
            + ["XDEBUG_MODE=off"] + ([f"DB_NAME={self.db_name}"] if self.db_name else [])

    def _create(self, shared):
        args, image, command = self.run_args(shared)
        env_file = Path(os.environ.get("TEMP", "/tmp")) / f"{self.name}.env"
        env_file.write_text("\n".join(self.env(shared)) + "\n", encoding="utf-8")
        try:
            _docker(*args, "--env-file", str(env_file), image, *command)
        finally:
            env_file.unlink(missing_ok=True)
        for extra in list((shared.get("NetworkSettings") or {}).get("Networks") or {})[1:]:
            _docker("network", "connect", extra, self.name, check=False)
        probe = ("for i in $(seq 1 60); do (exec 3<>/dev/tcp/127.0.0.1/80) 2>/dev/null "
                 "&& exit 0; sleep 0.5; done; exit 1")
        if _docker("exec", self.name, "bash", "-c", probe, check=False).returncode != 0:
            raise TestEnvError(f"{self.name} did not open port 80 within 30s (`docker logs {self.name}`)")

    def make_writable(self):
        """Create the runtime folders git ignores; Apache (www-data) and root-run tools both write them."""
        dirs = " ".join(GAIA_WRITABLE)
        _docker("exec", "-w", self.root, self.name, "sh", "-c",
                f"mkdir -p {dirs} && chmod 777 {dirs} && chmod -R a+rwX logs output", check=False)

    def clear_reports(self):
        """Remove earlier runs' api-tester reports, so only this run's are copied back."""
        _docker("exec", "-w", self.root, self.name, "sh", "-c", f"rm -f {REPORTS}", check=False)

    def fetch_reports(self):
        return self.fetch(REPORTS)


# --- prometheus ----------------------------------------------------------------------------------

class PrometheusTestContainer(CopiedSourceContainer):
    """Source copied in, incremental test build kept warm, `ember test --path` per run."""

    root = "/prometheus"

    def __init__(self, checkout, state_dir, base="prometheus-dev"):
        super().__init__(checkout, state_dir, "p4m-test-prometheus")
        self.base = base

    # container lifecycle
    def ensure(self, fresh=False):
        own = inspect(self.name)
        if own and (fresh or not _running(own)):
            _docker("rm", "-f", self.name)
            own = None
        if own:
            return False
        shared = inspect(self.base)
        image = shared["Config"]["Image"] if shared else "prometheus:latest"
        volume = next((m["Name"] for m in (shared or {}).get("Mounts", [])
                       if m["Type"] == "volume" and m["Destination"] == "/prometheus/node_modules"),
                      "prometheus_prometheus_node_modules")
        _docker("run", "-d", "--name", self.name, "--label", f"{LABEL}={norm(self.checkout)}",
                "-v", f"{volume}:/prometheus/node_modules", "-e", "CI=true", "-e", "TRAVIS=true",
                "-w", "/prometheus", "--entrypoint", "sh", image, "-c", "sleep infinity")
        # The image holds an old copy of the source: start from an empty tree (node_modules is a volume).
        _docker("exec", self.name, "sh", "-c",
                "find /prometheus -mindepth 1 -maxdepth 1 ! -name node_modules -exec rm -rf {} +")
        self.manifest_file.unlink(missing_ok=True)
        return True

    # incremental build
    def _build_log(self):
        r = _docker("exec", self.name, "sh", "-c", f"cat {BUILD_LOG} 2>/dev/null", check=False)
        return r.stdout or ""

    def build_count(self):
        """Builds finished so far. Read it BEFORE sync(): a small change rebuilds faster than the copy returns."""
        return len(BUILD_DONE.findall(self._build_log()))

    def _builder_running(self):
        r = _docker("exec", self.name, "sh", "-c", BUILDER_RUNNING_SCRIPT, check=False)
        return r.returncode == 0

    def _stop_builder(self):
        _docker("exec", self.name, "sh", "-c", STOP_BUILDER_SCRIPT, check=False)

    def stale_builders(self):
        """Pids of processes holding the build log open that are not p4m's own watch build (its process group)."""
        r = _docker("exec", self.name, "sh", "-c", STALE_SCRIPT, check=False)
        return [int(p) for p in (r.stdout or "").split() if p.isdigit()]

    def kill_stale_builders(self):
        """Kill orphaned watchers (e.g. renamed to plain `ember`, so pgrep -f 'ember build' misses them)."""
        pids = self.stale_builders()
        if pids:
            _docker("exec", self.name, "kill", "-9", *map(str, pids), check=False)
        return pids

    def build(self, changed, before=None, timeout=900, progress=30):
        """Make sure the test build is current after syncing `changed` paths. Returns (ok, message).

        `before` is build_count() taken before the sync; without it the count is read now (racy after a copy).
        """
        self.kill_stale_builders()
        if any(c in RESTART_ON for c in changed) and self._builder_running():
            self._stop_builder()
        if before is None:
            before = self.build_count()
        if not self._builder_running():
            _docker("exec", "-d", self.name, "sh", "-c",
                    f"echo $$ > {BUILD_PID}; cd /prometheus && exec ./node_modules/.bin/ember build "
                    f"--environment=test --output-path={BUILD_DIR} --watch > {BUILD_LOG} 2>&1")
            before, changed = 0, ["(new build)"]
        elif changed and not any(c.startswith(WATCHED) for c in changed):
            return True, "build current (changed files are outside the build)"
        if not changed and before:
            return True, "build current"
        deadline = time.time() + timeout
        last, stable_since = None, time.time()
        next_note = time.time() + progress
        while time.time() < deadline:
            log = self._build_log()
            done = len(BUILD_DONE.findall(log))
            if log != last:
                last, stable_since = log, time.time()
            if progress and time.time() >= next_note:
                next_note = time.time() + progress
                print(f"waiting for the test build, {done} builds seen, last log write "
                      f"{int(time.time() - stable_since)}s ago", file=sys.stderr, flush=True)
            # a new build result, and nothing written for 2s (a sync can trigger two rebuilds)
            if done > before and time.time() - stable_since >= 2:
                tail = log[log.rfind("\n", 0, max(0, len(log) - 2000)) + 1:]
                failed = list(BUILD_FAILED.finditer(log))
                ok_marks = list(re.finditer(r"Build successful|Built project successfully", log, re.I))
                if failed and (not ok_marks or failed[-1].start() > ok_marks[-1].start()):
                    return False, "test build failed:\n" + tail.strip()[-1500:]
                return True, "build updated"
            if not self._builder_running():
                return False, "ember build stopped:\n" + log.strip()[-1500:]
            time.sleep(1)
        return False, f"test build did not finish within {timeout}s ({BUILD_LOG} in {self.name})"

    def test_command(self, filter_text=None):
        cmd = ["docker", "exec", "-w", "/prometheus", self.name, "./node_modules/.bin/ember", "test",
               "--path", BUILD_DIR]
        if filter_text:
            cmd += ["--filter", filter_text]
        return cmd

    def kill_tests(self):
        """Stop any `ember test` and its Chrome in the container (a killed caller leaves them running)."""
        # [e]mber / [c]hrome: the patterns must not match the sh -c command line that runs pkill
        _docker("exec", self.name, "sh", "-c",
                "pkill -f '[e]mber test'; pkill -f '[c]hrome'; pkill -f '[t]estem'; sleep 1", check=False)

    def run_tests(self, filter_text=None, timeout=TEST_TIMEOUT, log=None):
        """Run `ember test`, echoing its output as it arrives. Returns the exit code.

        A run that can never finish is stopped: nothing matched the filter, a QUnit global failure /
        TestLoader failure (testem waits forever after those), or `timeout` seconds. Whatever ends the run,
        the in-container ember test and Chrome are killed.
        """
        self.kill_tests()
        proc = subprocess.Popen(self.test_command(filter_text), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", bufsize=1)
        lines = queue.Queue()

        def pump():
            for l in proc.stdout:
                lines.put(l)
            lines.put(None)

        threading.Thread(target=pump, daemon=True).start()
        logf = open(log, "w", encoding="utf-8") if log else None
        started, stuck_since, reason = time.time(), None, None
        try:
            while True:
                try:
                    line = lines.get(timeout=1)
                except queue.Empty:
                    line = ""
                else:
                    if line is None:
                        break
                    print(line, end="", flush=True)
                    if logf:
                        logf.write(line)
                        logf.flush()
                    if HANG_MARKER.search(line) and stuck_since is None:
                        stuck_since = time.time()
                if stuck_since and time.time() - stuck_since > STUCK_GRACE:
                    reason = "a QUnit global failure / test load failure or an unmatched filter; testem would wait forever"
                    break
                if time.time() - started > timeout:
                    reason = f"ember test did not finish within {timeout}s"
                    break
        except KeyboardInterrupt:
            reason = "interrupted"
        finally:
            if reason or proc.poll() is None:
                proc.kill()
            self.kill_tests()
            if logf:
                if reason:
                    logf.write(f"\n# p4m: stopped ember test: {reason}\n")
                logf.close()
        if reason:
            print(f"\np4m: stopped ember test: {reason}", flush=True)
            return 1
        return proc.wait()


# --- cleanup -------------------------------------------------------------------------------------

def clean(checkout=None):
    """Remove the p4m test containers (only those for `checkout` if given). Returns their names."""
    # a container is labelled with the path it was started with, so match the given and the resolved form
    labels = ([f"{LABEL}={norm(p)}" for p in dict.fromkeys([Path(checkout), Path(checkout).resolve()])]
              if checkout else [LABEL])
    names = []
    for label in labels:
        names += [n for n in _docker("ps", "-a", "--filter", f"label={label}", "--format", "{{.Names}}").stdout.split()
                  if n not in names]
    for n in names:
        _docker("rm", "-f", n, check=False)
    return names


def containers(under):
    """[(name, checkout)] of the p4m test containers started for a checkout in the folder `under` (or `under` itself),
    e.g. a Talos issue workspace; checkout is the label's comparable path relative to `under` ("" for `under`). Slot
    containers are labelled by slot, so they never match. Raises TestEnvError when docker can't be asked."""
    prefixes = list(dict.fromkeys(norm(p) for p in (Path(under), Path(under).resolve())))
    out = _docker("ps", "-a", "--filter", f"label={LABEL}", "--format", f'{{{{.Names}}}}\t{{{{.Label "{LABEL}"}}}}')
    found = []
    for line in out.stdout.splitlines():
        name, _, checkout = line.partition("\t")
        for p in prefixes:
            if checkout == p or checkout.startswith(p + "/"):
                found.append((name, checkout[len(p) + 1:]))
                break
    return found


def remove(names):
    """Remove the named test containers (`containers` finds them). Raises TestEnvError when one can't be removed."""
    for n in names:
        _docker("rm", "-f", n)
