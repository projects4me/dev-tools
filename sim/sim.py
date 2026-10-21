"""Stand-ins for what Gaia and Hermes don't have yet: Gaia's idea resources and Hermes' idea events.

    dev-tools/sim/sim serve [--import-nestor]   http://127.0.0.1:4791 (until Ctrl+C)

Temporary: delete this folder when Gaia has ideas and Hermes carries their events. See README.md.
"""

import argparse
import json
import secrets
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from lib.bus import Bus  # noqa: E402
from lib.ideas import Ideas  # noqa: E402
from lib.server import serve  # noqa: E402
from lib.store import Store  # noqa: E402

SIM_DIR = Path(__file__).resolve().parent
# This machine's settings and state live in the dev-tools checkout, also when the code runs from the runtime copy
# (<workspace>/.dev-tools-runtime, see `p4m runtime`), so their paths don't depend on which copy is running.
LOCAL_DIR = SIM_DIR.parent.parent / "dev-tools"
DB_FILE = LOCAL_DIR / "sim" / "state" / "sim.db"
SECRET_FILE = LOCAL_DIR / "sim" / "state" / "hermes-secret"   # the /publish secret; Talos reads it from here
OLD_NESTOR_DB = LOCAL_DIR / "nestor" / "state" / "nestor.db"


def settings():
    with open(SIM_DIR / "config.json", encoding="utf-8") as f:
        cfg = json.load(f)
    local = LOCAL_DIR / "sim" / "config.local.json"
    if local.exists():
        with open(local, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def publish_secret(cfg):
    """config `secret` if set (config.local.json), else a random one kept in state/ (git-ignored)."""
    if cfg.get("secret"):
        return cfg["secret"]
    if not SECRET_FILE.exists():
        SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        SECRET_FILE.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    return SECRET_FILE.read_text(encoding="utf-8").strip()


def import_nestor(store, owner, agent):
    """One-time: ideas from the first Nestor build (dev-tools/nestor/state/nestor.db) into sim Gaia."""
    if not OLD_NESTOR_DB.exists() or store.one("SELECT count(*) AS n FROM idea")["n"]:
        return 0
    old = sqlite3.connect(str(OLD_NESTOR_DB))
    old.row_factory = sqlite3.Row
    done = 0
    with store.transaction() as db:
        for i in old.execute("SELECT * FROM ideas WHERE status != 'archived' ORDER BY id"):
            iid = db.execute("INSERT INTO idea (title, status, owner, agent, created, updated) VALUES (?, ?, ?, ?, ?, ?)",
                             (i["title"], i["status"], owner, agent, i["created"], i["updated"])).lastrowid
            plan_ids = {}
            for p in old.execute("SELECT * FROM plans WHERE idea_id = ? ORDER BY version", (i["id"],)):
                plan_ids[p["version"]] = db.execute(
                    "INSERT INTO ideaplan (idea_id, version, body, status, error, created, updated) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (iid, p["version"], p["body"], p["status"], p["error"], p["created"], p["created"])).lastrowid
            for m in old.execute("SELECT * FROM messages WHERE idea_id = ? ORDER BY id", (i["id"],)):
                role = {"nestor": "agent"}.get(m["role"], m["role"])
                db.execute("INSERT INTO ideamessage (idea_id, role, author, body, steps, plan_id, cost_usd, created) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (iid, role, owner if role == "user" else agent, m["body"], m["steps"],
                            plan_ids.get(m["plan_version"]), m["cost_usd"], m["created"]))
            for c in old.execute("SELECT * FROM created_issues WHERE idea_id = ?", (i["id"],)):
                db.execute("INSERT INTO ideaissue (idea_id, plan_id, key, number, project_code, subject, url, created) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (iid, plan_ids.get(c["plan_version"]), c["key"], c["number"], c["project_code"],
                            c["subject"], c["url"], c["created"]))
            done += 1
    old.close()
    return done


def cmd_serve(args):
    cfg = settings()
    store = Store(Path(args.db) if args.db else DB_FILE)
    if args.import_nestor:
        owner, agent = args.import_nestor
        print(f"imported {import_nestor(store, owner, agent)} idea(s) from {OLD_NESTOR_DB}")
    bus = Bus()
    httpd = serve(Ideas(store, bus), bus, cfg["host"], int(args.port or cfg["port"]),
                  publish_secret(cfg))
    print(f"sim Gaia (ideas) + sim Hermes at http://{cfg['host']}:{args.port or cfg['port']}/  (Ctrl+C to stop)",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="sim", description="Stand-ins for Gaia idea resources and Hermes idea events")
    sub = p.add_subparsers(dest="command", required=True)
    sp = sub.add_parser("serve", help="run sim Gaia + sim Hermes")
    sp.add_argument("--port", type=int)
    sp.add_argument("--db", help="another database file (e.g. for a throwaway test run)")
    sp.add_argument("--import-nestor", nargs=2, metavar=("OWNER_ID", "AGENT_ID"),
                    help="first run only: import the ideas of the first Nestor build, owned by OWNER_ID with AGENT_ID")
    sp.set_defaults(func=cmd_serve)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
