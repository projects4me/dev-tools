"""The post-run check of the runtime copy: did a run change the tooling it ran on, and putting it back.

Talos snapshots the copy (<workspace>/.dev-tools-runtime) before each run and compares after it: HEAD, the branch
and `git status`. ai/state/ is left out, because runs write their ship files and knowledge base there. A change the
run made is saved to its log, undone (HEAD back to the recorded commit, the changed paths back to it, new files
removed) and the issue handed off as blocked. Changes that were already there before the run are left alone.

Runs in parallel share the copy, so a change lands on whichever run ends first after it; the hand-off comment says
so. The PreToolUse hook (runtime_hook.py) stops most changes before they happen.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path

ALLOWED_PREFIX = "ai/state/"


class RuntimeCheckError(Exception):
    """A git step on the runtime copy failed; the message says which and why."""


@dataclass(frozen=True)
class Snapshot:
    head: str
    branch: str            # "" when detached, as `p4m runtime` keeps the copy
    entries: frozenset     # (status code, path) from `git status`, outside ai/state/
    digests: frozenset = frozenset()   # (path, blob hash) of each changed file on disk, to see a second edit


def _git(cwd, *args, check=True):
    cp = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", stdin=subprocess.DEVNULL)
    if check and cp.returncode != 0:
        raise RuntimeCheckError(f"git {' '.join(args)} failed in {cwd}: {(cp.stderr or cp.stdout).strip()}")
    return cp.stdout


def _entries(porcelain):
    """(code, path) pairs from `git status --porcelain -z`; a rename gives its source path too."""
    parts, out, i = porcelain.split("\0"), [], 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        out.append((code, path))
        if code[0] in "RC" and i < len(parts):
            out.append((code, parts[i]))
            i += 1
    return frozenset(e for e in out if not e[1].startswith(ALLOWED_PREFIX))


def _digests(runtime, entries):
    """(path, blob hash) for the changed paths that are files on disk: a file already changed keeps its status code
    when it is changed again, its hash doesn't."""
    paths = sorted({path for _, path in entries if (runtime / path).is_file()})
    if not paths:
        return frozenset()
    return frozenset(zip(paths, _git(runtime, "hash-object", "--", *paths).split()))


def snapshot(runtime):
    runtime = Path(runtime)
    entries = _entries(_git(runtime, "status", "--porcelain", "-z", "--untracked-files=all"))
    return Snapshot(head=_git(runtime, "rev-parse", "HEAD").strip(),
                    branch=_git(runtime, "symbolic-ref", "-q", "--short", "HEAD", check=False).strip(),
                    entries=entries, digests=_digests(runtime, entries))


def changes(before, after):
    """What changed between two snapshots, one line each; empty when nothing did."""
    found = []
    if after.head != before.head:
        found.append(f"HEAD moved from {before.head[:10]} to {after.head[:10]}")
    if after.branch != before.branch:
        found.append(f"checked-out branch changed from {before.branch or '(detached)'} to "
                     f"{after.branch or '(detached)'}")
    found += [f"{code.strip() or code} {path}" for code, path in sorted(after.entries - before.entries, key=lambda e: (e[1], e[0]))]
    was = dict(before.digests)
    found += [f"changed again {path} (already changed before the run)"
              for path, digest in sorted(after.digests) if path in was and was[path] != digest]
    return found


def report(runtime, before, found):
    """The text saved to the run log: what changed, the commits since the recorded HEAD and the full diff."""
    runtime = Path(runtime)
    lines = ["--- runtime copy changed during the run ---", f"copy: {runtime}", *found, ""]
    lines.append(_git(runtime, "log", "--oneline", f"{before.head}..HEAD", check=False))
    lines.append(_git(runtime, "diff", before.head, "--", ".", f":(exclude){ALLOWED_PREFIX}", check=False))
    untracked = sorted(path for code, path in snapshot(runtime).entries - before.entries if code == "??")
    if untracked:
        lines += ["untracked files:", *untracked]
    return "\n".join(lines) + "\n"


def restore(runtime, before):
    """Put the copy back to `before`: HEAD to the recorded commit (detached, like `p4m runtime`), every path the run
    changed back to it, new files removed. Returns what is still different afterwards (empty when restored)."""
    runtime = Path(runtime)
    now = snapshot(runtime)
    if now.head != before.head or now.branch != before.branch:
        target = before.branch or before.head
        # --force only when the copy was clean before the run: it would also throw away changes from before it
        clean = all(code == "??" for code, _ in before.entries)
        _git(runtime, "checkout", *(["--force"] if clean else []), *([] if before.branch else ["--detach"]), target)
        now = snapshot(runtime)
    for _, path in sorted(now.entries - before.entries):
        if any(p == path for _, p in before.entries):
            continue                      # changed before the run too: not this run's to undo
        _git(runtime, "reset", "-q", before.head, "--", path, check=False)
        if _tracked(runtime, before.head, path):
            _git(runtime, "checkout", before.head, "--", path)
        else:
            target = runtime / path
            if target.is_file() or target.is_symlink():
                target.unlink()
    return changes(before, snapshot(runtime))


def after_run(runtime, before, log_path):
    """The check after a run: what it changed in the copy (empty for nothing) and what is still different after
    restoring. A change is appended to the run log (report) and undone (restore). Raises RuntimeCheckError."""
    found = changes(before, snapshot(runtime))
    if not found:
        return [], []
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("\n" + report(runtime, before, found))
    return found, restore(runtime, before)


def _tracked(runtime, head, path):
    """Whether the path is in the commit (a file the run changed or deleted, not one it added)."""
    return subprocess.run(["git", "cat-file", "-e", f"{head}:{path}"], cwd=runtime, capture_output=True,
                          stdin=subprocess.DEVNULL).returncode == 0
