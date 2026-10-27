"""PreToolUse guard (Bash and Read): redirect, never truncate.

Reads the hook JSON on stdin. Blocks (permissionDecision "deny" with a one-line reason) reads of Gaia framework and
api-tester internals that a gaia-dev command answers, and `docker exec|run` of the test suites (use `ai test`).
Escape hatches: the file is in the branch's changed set (genuine framework work), or the command carries a `--raw`
marker / `# raw` comment. Blocks and --raw uses are appended to state/hooks.jsonl for `ai cost`.
Stdlib only and no subprocess unless a protected file matched: it runs on every tool call (budget 100 ms).
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

LOG = Path(os.environ.get("AI_HOOK_LOG") or Path(__file__).resolve().parent.parent / "state" / "hooks.jsonl")

PROTECTED = (   # (path regex, the gaia-dev command that answers the question)
    (re.compile(r"gaia/core/mvc/controllers/RestController\.php$"), "gaia-dev route <path> (routes) or gaia-dev model <Model>"),
    (re.compile(r"gaia/core/libs/security/Acl\.php$"), "gaia-dev acl <model>"),
    (re.compile(r"gaia/core/mvc/models/Model\.php$"), "gaia-dev model <Model>"),
    (re.compile(r"gaia/tools/api-tester/(src|apis|harness)/"), "gaia-dev cases <model>, gaia-dev assertions or gaia-dev profiles"),
    (re.compile(r"gaia/(tools/api-tester/)?fixtures/default\.json$"), "gaia-dev profiles or gaia-dev model <Model>"),
)
READ_VERB = re.compile(r"(^|[\s;&|(])(cat|head|tail|sed|awk|grep|egrep|rg|less|more|bat|wc|nl|type|Get-Content|sls)\s")
RAW = re.compile(r"(^|\s)--raw(\s|$)|#\s*raw\b")
DOCKER_SUITE = re.compile(r"\bdocker\s+(exec|run)\b.*\b(phpunit|api-tester|pytest|unittest|ember\s+test|npm\s+(run\s+)?test|vendor/bin)\b", re.S)
PATH_TOKEN = re.compile(r"[\w./\:~-]*(?:RestController\.php|Acl\.php|Model\.php|api-tester[\w./\-]*|default\.json)")


def norm(p):
    return str(p).replace("\\", "/")


def protected(path):
    p = norm(path)
    for rx, answer in PROTECTED:
        if rx.search(p):
            return p, answer
    return None


def changed_set(root):
    """Files (relative to the gaia root) changed on the current branch there; empty on any failure."""
    names = set()
    try:
        for args in (["diff", "--name-only", "origin/dev...HEAD"], ["diff", "--name-only", "HEAD"]):
            out = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=5).stdout
            names |= set(out.split())
    except (OSError, subprocess.SubprocessError):
        pass
    return names


def gaia_root(path):
    p = norm(path)
    return p.split("/gaia/")[0] + "/gaia" if "/gaia/" in p else str(Path(__file__).resolve().parents[3] / "gaia")


def in_changed_set(path):
    root = gaia_root(path)
    rel = norm(path).split("/gaia/", 1)[-1]
    return rel in changed_set(root)


def log(kind, detail):
    try:
        LOG.parent.mkdir(exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, "detail": detail[:200]}) + "\n")
    except OSError:
        pass


def decide(tool, tool_input):
    """None to allow, else (kind, reason)."""
    if tool == "Read":
        hit = protected(tool_input.get("file_path", ""))
        if hit and not in_changed_set(hit[0]):
            return "read", f"{Path(hit[0]).name} is framework internals: use {hit[1]} (it answers this in a few lines)."
        return None
    if tool != "Bash":
        return None
    cmd = tool_input.get("command", "")
    if DOCKER_SUITE.search(cmd):
        return "docker", "Don't run suites with docker exec/run (shared containers may serve another checkout): use `dev-tools/ai/ai test <repo>`."
    if not READ_VERB.search(cmd):
        return None
    for tok in PATH_TOKEN.findall(cmd):
        hit = protected(tok) or protected("gaia/" + tok.lstrip("./"))
        if not hit:
            continue
        if RAW.search(cmd):
            log("raw", cmd)
            return None
        if not in_changed_set(hit[0]):
            return "read", f"{Path(hit[0]).name} is framework internals: use {hit[1]}; add --raw or `# raw` to read it anyway."
    return None


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    verdict = decide(data.get("tool_name", ""), data.get("tool_input") or {})
    if verdict:
        log("block-" + verdict[0], verdict[1])
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                  "permissionDecisionReason": verdict[1]}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
