"""Nestor: the UI for brainstorming ideas with Agent Nestor. Only the UI: the agent runs in Talos, and the idea
data and events go through the dev-tools/sim stand-ins for Gaia and Hermes.

    dev-tools/nestor/nestor serve [--open]   http://127.0.0.1:4790 (until Ctrl+C)

See README.md.
"""

import argparse
import sys
import webbrowser

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from lib.config import NestorError, settings  # noqa: E402
from lib.server import serve  # noqa: E402


def cmd_serve(args):
    cfg = settings()
    if args.sim_url:
        cfg["simUrl"] = args.sim_url
    port = int(args.port or cfg.get("port", 4790))
    try:
        httpd = serve(cfg, port)
    except ValueError as e:
        raise NestorError(str(e))
    url = f"http://{cfg.get('host', '127.0.0.1')}:{port}/"
    print(f"Nestor UI at {url} for {cfg['engineerName'] or cfg['engineerUserId']}; stand-ins at {cfg['simUrl']}; "
          f"the agent runs in Talos (`talos run`)  (Ctrl+C to stop)", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="nestor", description="The Nestor UI (brainstorm ideas with Agent Nestor)")
    sub = p.add_subparsers(dest="command", required=True)
    sp = sub.add_parser("serve", help="run the UI")
    sp.add_argument("--port", type=int)
    sp.add_argument("--sim-url", help="where the sim stand-ins are (default: simUrl in config.json)")
    sp.add_argument("--open", action="store_true", help="open it in the browser")
    sp.set_defaults(func=cmd_serve)
    args = p.parse_args(argv)
    try:
        return args.func(args)
    except NestorError as e:
        print(f"nestor: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
