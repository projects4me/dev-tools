"""Test containers that run a suite against a given checkout (the workspace's by default).

The shared containers (gaia-test, hermes, prometheus-dev) serve whichever folder `p4m up` was run from.
Several workspaces can exist on one machine, so `p4m test` checks what the shared container really runs:

- gaia / hermes bind-mount their source. If the shared container mounts another folder, a throwaway
  container is started from the same image, network, env and mounts, with the source mount (and any bind
  under it) pointed at the checkout under test. It is kept for reuse and keyed by the checkout path.
- prometheus has no source mount (a Windows bind mount makes the Ember build crawl). Its test container
  gets the source copied in (only changed files on later runs), keeps an incremental
  `ember build --environment=test --watch` running, and runs `ember test --path` on the result.

Containers are labelled `p4m.test` and removed by `p4m test clean`.
"""

import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import time
from pathlib import Path

IS_WINDOWS = os.name == "nt"
LABEL = "p4m.test"
BUILD_DIR = "/tmp/p4m-test-dist"
BUILD_LOG = "/tmp/p4m-build.log"
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


# --- prometheus ----------------------------------------------------------------------------------

def _tracked_files(checkout):
    """Tracked plus untracked-but-not-ignored files of a checkout, minus SYNC_EXCLUDE."""
    out = subprocess.run(["git", "-C", str(checkout), "ls-files", "-co", "--exclude-standard", "-z"],
                         capture_output=True, check=True).stdout.decode("utf-8", "replace")
    return [f for f in out.split("\0") if f and not f.startswith(SYNC_EXCLUDE)]


class PrometheusTestContainer:
    """Source copied in, incremental test build kept warm, `ember test --path` per run."""

    def __init__(self, checkout, state_dir, base="prometheus-dev"):
        self.checkout = Path(checkout).resolve()
        self.base = base
        self.name = f"p4m-test-prometheus-{_key(self.checkout)}"
        self.manifest_file = Path(state_dir) / f"{self.name}.manifest.json"

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

    # source sync
    def sync(self):
        """Copy new/changed files in and delete removed ones. Returns the paths changed."""
        old = json.loads(self.manifest_file.read_text(encoding="utf-8")) if self.manifest_file.exists() else {}
        new, changed = {}, []
        for rel in _tracked_files(self.checkout):
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
        if changed:
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tar:
                for rel in changed:
                    tar.add(str(self.checkout / rel), arcname=rel, recursive=False)
            _docker("exec", "-i", self.name, "tar", "-x", "-C", "/prometheus", input=buf.getvalue(), text=False)
        for i in range(0, len(removed), 200):
            _docker("exec", self.name, "rm", "-f", "--", *[f"/prometheus/{r}" for r in removed[i:i + 200]])
        self.manifest_file.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_file.write_text(json.dumps(new), encoding="utf-8")
        return changed + removed

    # incremental build
    def _build_log(self):
        r = _docker("exec", self.name, "sh", "-c", f"cat {BUILD_LOG} 2>/dev/null", check=False)
        return r.stdout or ""

    def _builder_running(self):
        # [e]mber: the pattern must not match this sh -c command line itself
        r = _docker("exec", self.name, "sh", "-c", "pgrep -f '[e]mber build' >/dev/null", check=False)
        return r.returncode == 0

    def build(self, changed, timeout=900):
        """Make sure the test build is current after syncing `changed` paths. Returns (ok, message)."""
        if any(c in RESTART_ON for c in changed) and self._builder_running():
            _docker("exec", self.name, "sh", "-c", "pkill -f '[e]mber build'; sleep 1", check=False)
        before = len(BUILD_DONE.findall(self._build_log()))
        if not self._builder_running():
            _docker("exec", "-d", self.name, "sh", "-c",
                    f"cd /prometheus && ./node_modules/.bin/ember build --environment=test "
                    f"--output-path={BUILD_DIR} --watch > {BUILD_LOG} 2>&1")
            before, changed = 0, ["(new build)"]
        elif changed and not any(c.startswith(WATCHED) for c in changed):
            return True, "build current (changed files are outside the build)"
        if not changed and before:
            return True, "build current"
        deadline = time.time() + timeout
        last, stable_since = None, time.time()
        while time.time() < deadline:
            log = self._build_log()
            done = len(BUILD_DONE.findall(log))
            if log != last:
                last, stable_since = log, time.time()
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


# --- cleanup -------------------------------------------------------------------------------------

def clean(checkout=None):
    """Remove the p4m test containers (only those for `checkout` if given). Returns their names."""
    label = f"{LABEL}={norm(Path(checkout).resolve())}" if checkout else LABEL
    names = _docker("ps", "-a", "--filter", f"label={label}", "--format", "{{.Names}}").stdout.split()
    for n in names:
        _docker("rm", "-f", n, check=False)
    return names
