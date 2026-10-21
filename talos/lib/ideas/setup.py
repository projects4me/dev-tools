"""Wires one ideas agent: its Gaia identity, the idea client, the streamed runner, the worker and its event source."""

from ..config import STATE_DIR, TalosError, WORKSPACE
from ..simbus import SimSource
from ..tracker import Tracker
from . import policy
from .approval import Approval
from .client import IdeasClient
from .conversation import Conversation
from .kb import KbSync, sync_as
from .progress import ProgressPublisher
from .render import Renderer
from ..mcp import mcp_config
from .stream import Launcher
from .worker import IdeaWorker

MAP_TEMPLATE = """# Knowledge map

Areas of the product -> where the code lives, the key issues and decisions, open threads.
Kept short; the agent updates it as it learns. Local only (git-ignored).
"""


def sim_settings(cfg):
    """sim.url, and the publish secret: sim.secret (config.local.json) or the file the sim keeps it in."""
    sim = dict(cfg.get("sim") or {})
    if not sim.get("url"):
        raise TalosError("config.json needs sim.url (the dev-tools/sim stand-ins for Gaia ideas and Hermes events)")
    if not sim.get("secret"):
        path = WORKSPACE / (sim.get("secretFile") or "")
        if not sim.get("secretFile") or not path.is_file():
            raise TalosError(f"no sim publish secret: start dev-tools/sim/sim serve once (it writes {path}), or set "
                             f"sim.secret in config.local.json")
        sim["secret"] = path.read_text(encoding="utf-8").strip()
    return sim


def knowledge_map(agent):
    """The agent's map file (agent.json `knowledgeMap`, relative to the workspace), created if missing."""
    rel = agent.extra.get("knowledgeMap")
    if not rel:
        raise TalosError(f"{agent.name}: agent.json needs knowledgeMap")
    path = WORKSPACE / rel
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(MAP_TEMPLATE, encoding="utf-8")
    return path


def start(agent, cfg, claude):
    """Start the agent's worker and sim Hermes listener; returns the worker."""
    sim = sim_settings(cfg)
    tracker = Tracker(agent)
    user_id = tracker.user_id()
    client = IdeasClient(sim["url"], user_id)
    progress = ProgressPublisher(sim["url"], sim["secret"], user_id)
    env = tracker.child_env()
    codes = {}

    def project_codes():
        if not codes:
            codes.update(tracker.project_codes())
        return codes

    launcher = Launcher(claude, agent, env, mcp=mcp_config(STATE_DIR / agent.name / "mcp.json"))
    kb = KbSync(sync_as(env), int(cfg.get("kbMaxAgeMinutes", 10)))
    conversation = Conversation(agent, client, progress, launcher, kb, project_codes, knowledge_map(agent),
                                milestones=tracker.milestones, renderer=Renderer(cfg.get("browserBin"), agent.reviewer_name,
                                                  cfg.get("prometheusUrl")))
    worker = IdeaWorker(agent, client, progress, conversation, Approval(agent, client, tracker), user_id)
    worker.start()
    SimSource(sim["url"], user_id, policy.intents(agent, user_id), worker.on_event, worker.reconcile,
              agent.name, int(cfg.get("reconnectSeconds", 10))).start()
    return worker
