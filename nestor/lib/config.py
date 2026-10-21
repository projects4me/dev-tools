"""Nestor UI settings: where the stand-ins are, and who the engineer and the agent are."""

import json
from pathlib import Path

NESTOR_DIR = Path(__file__).resolve().parent.parent
DEV_TOOLS = NESTOR_DIR.parent
WEB_DIR = NESTOR_DIR / "web"
CONFIG_FILE = NESTOR_DIR / "config.json"
WORKSPACE_LOCAL = DEV_TOOLS / "workspace.local.json"


class NestorError(Exception):
    pass


def settings():
    """config.json, overlaid by the git-ignored config.local.json; the engineer defaults to the one
    `p4m configure` wrote in workspace.local.json."""
    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    local = CONFIG_FILE.with_name("config.local.json")
    if local.exists():
        with open(local, encoding="utf-8") as f:
            cfg.update(json.load(f))
    if WORKSPACE_LOCAL.exists():
        with open(WORKSPACE_LOCAL, encoding="utf-8") as f:
            ws = json.load(f)
        cfg["engineerUserId"] = cfg.get("engineerUserId") or str(ws.get("gaiaUserId") or "")
        cfg["engineerName"] = cfg.get("engineerName") or ws.get("name") or ""
    missing = [k for k in ("engineerUserId", "agentUserId") if not cfg.get(k)]
    if missing:
        raise NestorError(f"set {', '.join(missing)} in dev-tools/nestor/config.local.json (Gaia user ids; "
                          f"`dev-tools/talos/talos agents --agent nestor` shows the agent's)")
    return cfg
