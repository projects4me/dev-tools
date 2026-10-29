"""Keeps a run from changing the tooling it runs on: a Claude Code PreToolUse hook that Talos adds to every local run.

The runtime copy (<workspace>/.dev-tools-runtime, `p4m runtime`) only changes through `p4m runtime update`. A run
reaches it through its workspace's `.dev-tools-runtime` link, and through `dev-tools/` when the issue is not a
dev-tools issue. The hook denies:
- Edit/Write/MultiEdit/NotebookEdit of a file that resolves into the copy (links followed), except under ai/state/,
  where runs keep their ship files and knowledge base;
- git commands that move the copy's HEAD or change its index or files (checkout, switch, rebase, reset, commit,
  stash, ...) when they run in it: the Bash command's cwd, its `cd`s and `git -C` are followed.

Other shell writes can't be told apart reliably; the post-run check (runtime_check.py) catches what gets through.
Run as a script by Claude Code (`python runtime_hook.py <runtime copy>`, the hook input on stdin); exit code 2 with
the reason on stderr denies the call. Standard library only, so it runs without Talos' package.
"""

import json
import os
import re
import shlex
import sys
from pathlib import Path

ALLOWED = ("ai", "state")                 # the one folder in the copy that runs write
FILE_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
GIT_WRITES = {"checkout", "switch", "rebase", "reset", "commit", "stash", "merge", "pull", "cherry-pick", "revert",
              "restore", "am", "apply", "clean", "rm", "mv",
              "update-ref", "symbolic-ref", "worktree"}
GIT_OPTIONS_WITH_VALUE = {"-c", "--exec-path", "--namespace", "--config-env"}
SEPARATORS = re.compile(r"&&|\|\||[;|&\n]")
MATCHER = "|".join([*FILE_TOOLS, "Bash"])

REASON = ("{what} is denied: it would change the runtime copy of dev-tools ({runtime}), the tooling this run uses, "
          "which only changes through `p4m runtime update`. Don't edit or check out branches in the tooling you run "
          "on: describe the fault as a proposed dev-tools issue in your handoff (outcome needs_input or blocked) "
          "instead, also when you are asked to fix it in dev-tools.")


def _real(path):
    """The path with links (symlinks and junctions) resolved, in the OS's case, for comparing."""
    return Path(os.path.normcase(os.path.realpath(path)))


def _inside(path, runtime):
    """The path relative to the runtime copy when it resolves into it, else None."""
    try:
        return _real(path).relative_to(_real(runtime))
    except ValueError:
        return None


def _native(path):
    """A Git Bash path (/d/work) as the OS path (d:/work) on Windows; unchanged elsewhere."""
    match = re.match(r"^/([a-zA-Z])(/.*)?$", path)
    if os.name == "nt" and match:
        return f"{match.group(1)}:{match.group(2) or '/'}"
    return path


def _move(folder, target):
    return folder / Path(os.path.expanduser(_native(target)))


def file_verdict(tool_input, key, cwd, runtime):
    """The reason to deny a file tool's call, or None."""
    target = tool_input.get(key)
    if not target:
        return None
    relative = _inside(_move(Path(cwd), str(target)), runtime)
    if relative is None or relative.parts[:2] == tuple(os.path.normcase(p) for p in ALLOWED):
        return None
    return REASON.format(what=f"Changing {target}", runtime=runtime)


def _tokens(segment):
    try:
        return shlex.split(segment)
    except ValueError:                    # an unclosed quote: the separators split inside it
        return segment.split()


def _git_call(tokens):
    """(the -C/--work-tree folders, the subcommand) when tokens run git (also through the odysseus wrapper)."""
    while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):    # FOO=bar git ...
        tokens = tokens[1:]
    if len(tokens) > 1 and Path(tokens[0]).name in ("odysseus", "odysseus.cmd") and tokens[1] == "git":
        tokens = tokens[1:]
    if not tokens or Path(tokens[0]).name not in ("git", "git.exe"):
        return None
    folders, rest = [], tokens[1:]
    while rest and rest[0].startswith("-"):
        option = rest.pop(0)
        if option == "-C" and rest:
            folders.append(rest.pop(0))
        elif option.startswith("--work-tree="):
            folders.append(option.split("=", 1)[1])
        elif option in GIT_OPTIONS_WITH_VALUE and rest:
            rest.pop(0)
    return folders, (rest[0] if rest else "")


def bash_verdict(command, cwd, runtime):
    """The reason to deny a Bash command, or None. Follows `cd`/`pushd` through the command's parts."""
    folder = Path(cwd)
    for segment in SEPARATORS.split(command or ""):
        tokens = _tokens(segment.strip())
        if not tokens:
            continue
        if tokens[0] in ("cd", "pushd"):
            folder = _move(folder, tokens[1]) if len(tokens) > 1 else Path.home()
            continue
        call = _git_call(tokens)
        if not call:
            continue
        folders, subcommand = call
        where = folder
        for target in folders:
            where = _move(where, target)
        if subcommand in GIT_WRITES and _inside(where, runtime) is not None:
            return REASON.format(what=f"`git {subcommand}` in {where}", runtime=runtime)
    return None


def verdict(tool_name, tool_input, cwd, runtime):
    """The reason to deny a tool call, or None to let it through."""
    if tool_name in FILE_TOOLS:
        return file_verdict(tool_input or {}, FILE_TOOLS[tool_name], cwd, runtime)
    if tool_name == "Bash":
        return bash_verdict((tool_input or {}).get("command"), cwd, runtime)
    return None


def settings(runtime, python=None):
    """The `claude --settings` part that runs this hook for the runtime copy at `runtime`."""
    script = Path(runtime) / "talos" / "lib" / "runtime_hook.py"
    command = " ".join(f'"{Path(p).as_posix()}"' for p in (python or sys.executable, script, runtime))
    return {"hooks": {"PreToolUse": [{"matcher": MATCHER, "hooks": [{"type": "command", "command": command}]}]}}


def main(argv):
    if len(argv) != 2:
        print("usage: runtime_hook.py <runtime copy>", file=sys.stderr)
        return 1
    data = json.load(sys.stdin)
    reason = verdict(data.get("tool_name"), data.get("tool_input"), data.get("cwd") or os.getcwd(), argv[1])
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
