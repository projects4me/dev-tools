"""aga: the only tool an Agamemnon session acts with. Run `dev-tools/agamemnon/aga -h`; see talos/README.md.

The incident comes from AGA_INCIDENT (the id Talos opened it with), never from the command line: the issue and the
incident type, and so what the premise allows, are read from talos.db. Every verb prints one JSON object; a refusal
exits 3 with {"refused": "..."}, and nothing was done."""

import argparse
import json
import os
import sys

from lib.agamemnon import tool, verbs, wiring
from lib.config import TalosError

INCIDENT_ENV = "AGA_INCIDENT"
REFUSED_EXIT = 3


def build_parser():
    p = argparse.ArgumentParser(prog="aga", description="Agamemnon's guarded actions on its incident's issue.")
    sub = p.add_subparsers(dest="verb", required=True)
    for verb in verbs.VERBS.values():
        sp = sub.add_parser(verb.name, help=f"[{verb.tier}, level {verb.level}] {verb.help}")
        if verb.acts:
            sp.add_argument("--why", required=True, help="the reasoning for this action (kept in the journal)")
        if verb.name == "logs":
            sp.add_argument("--lines", type=int, default=tool.LOG_LINES)
        elif verb.name == "restore":
            sp.add_argument("paths", nargs="+", help="known generated files (premise generatedFiles)")
        elif verb.name == "wait-for":
            sp.add_argument("what", help="an issue number, or 'slot' for a free Gaia test slot")
            sp.add_argument("--minutes", type=float, help="the longest wait (capped by agaWaitMaxMinutes)")
        elif verb.name == "send":
            sp.add_argument("--notes", required=True, help="what the worker should do (posted as a comment)")
    return p


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    verb_args = {k: v for k, v in vars(args).items() if k not in ("verb", "why") and v is not None}
    try:
        settings, ports = wiring.build()
        result = tool.Aga(settings, ports, os.environ.get(INCIDENT_ENV)).run(args.verb, verb_args,
                                                                            getattr(args, "why", None))
    except verbs.Refused as e:
        print(json.dumps({"refused": str(e)}))
        return REFUSED_EXIT
    except TalosError as e:
        print(json.dumps({"error": str(e)}))
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
