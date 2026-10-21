"""Nestor's tool calls in a few words for the chat ("Reading gaia/app/models/Issue.php")."""

import os
import re
from pathlib import Path

from ..config import WORKSPACE

REGEX = re.compile(r"[\\\[\](){}|*+?^$]")
COMMANDS = (("dev-tools/ai/ai kb search", "Searching issues:"), ("dev-tools/ai/ai kb show", "Reading issue"),
            ("dev-tools/ai/ai issue", "Reading issue"), ("dev-tools/ai/ai context", "Context for issue"))


def _short(path):
    try:
        return Path(os.path.relpath(path, WORKSPACE)).as_posix()
    except ValueError:
        return str(path)


def describe(name, args):
    args = args or {}
    if name == "Read":
        return f"Reading {_short(args.get('file_path', ''))}"
    if name == "Grep":
        where = f" in {_short(args['path'])}" if args.get("path") else ""
        pattern = str(args.get("pattern", ""))
        if len(pattern) > 40 or REGEX.search(pattern):   # a regex means nothing to someone watching
            return f"Searching the code{where}"
        return f"Searching for \"{pattern}\"{where}"
    if name == "Glob":
        where = f" in {_short(args['path'])}" if args.get("path") else ""
        return f"Listing {args.get('pattern', '')}{where}"
    if name == "Bash":
        command = " ".join(str(args.get("command", "")).split())
        for prefix, label in COMMANDS:
            if command.startswith(prefix):
                return f"{label} {command[len(prefix):].strip()}"[:140]
        return f"Running {command[:120]}"
    if name == "Edit":
        return "Updating the knowledge map"
    if name.startswith("mcp__gaia__"):
        tool = name[len("mcp__gaia__gaia_"):].replace("_", " ")
        detail = args.get("issue_number") or args.get("project") or args.get("search") or ""
        return f"Gaia: {tool} {detail}".strip()
    return name
