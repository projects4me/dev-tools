"""Wires one ideas agent: its Gaia identity, the idea client, the streamed runner, the worker and its event source.

The source is Gaia (ideas in Gaia, their events through Hermes) unless agent.json says `"ideaSource": "sim"`
(dev-tools/sim's stand-ins, kept until the sim is retired)."""

from ..config import STATE_DIR, TalosError, WORKSPACE
from ..hermes import Listener
from ..simbus import SimSource
from ..tracker import Tracker
from . import policy
from .approval import Approval
from .client import GaiaIdeasClient
from .conversation import Conversation
from .kb import KbSync, sync_as
from .progress import NoProgress, ProgressPublisher
from .render import Renderer
from .sim_client import SimIdeasClient
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


def source(agent):
    """Where the agent's ideas live: "gaia" (default) or "sim"."""
    value = agent.extra.get("ideaSource") or "gaia"
    if value not in ("gaia", "sim"):
        raise TalosError(f"{agent.name}: ideaSource must be gaia or sim")
    return value


def _sim_parts(agent, cfg, user_id, retry):
    """The sim's idea client, progress publisher, event policy and event source."""
    sim = sim_settings(cfg)

    def listen(worker):
        SimSource(sim["url"], user_id, policy.intents(agent, user_id), worker.on_event, worker.reconcile,
                  agent.name, retry).start()
    return (SimIdeasClient(sim["url"], user_id), ProgressPublisher(sim["url"], sim["secret"], user_id),
            lambda envelope: policy.job_for(envelope, agent, user_id), listen)


def _gaia_parts(agent, cfg, tracker, user_id, retry):
    """Gaia's idea client, no progress, the Gaia event policy and the Hermes listener."""
    project_ids = tracker.project_ids()

    def listen(worker):
        Listener(agent, tracker, policy.GAIA_EVENTS, worker.on_event, lambda _project_ids: worker.reconcile(),
                 cfg["hermesUrl"], cfg.get("nodeBin", "node"), retry).start()
    return (GaiaIdeasClient(tracker, user_id, agent.display_name), NoProgress(),
            lambda envelope: policy.gaia_job_for(envelope, agent, user_id, project_ids), listen)


def start(agent, cfg, claude):
    """Start the agent's worker and its event source (Hermes for Gaia ideas, sim Hermes for the sim's); returns
    the worker."""
    tracker = Tracker(agent)
    user_id = tracker.user_id()
    retry = int(cfg.get("reconnectSeconds", 10))
    if source(agent) == "sim":
        client, progress, job_for, listen = _sim_parts(agent, cfg, user_id, retry)
    else:
        client, progress, job_for, listen = _gaia_parts(agent, cfg, tracker, user_id, retry)
    env = tracker.child_env()
    codes = {}

    def project_codes():
        if not codes:
            codes.update(tracker.project_codes())
        return codes

    launcher = Launcher(claude, agent, env, mcp=mcp_config(STATE_DIR / agent.name / "mcp.json"))
    kb = KbSync(sync_as(env), int(cfg.get("kbMaxAgeMinutes", 10)))
    conversation = Conversation(agent, client, progress, launcher, kb, project_codes, knowledge_map(agent),
                                milestones=tracker.milestones, project_ids=tracker.project_ids, renderer=Renderer(cfg.get("browserBin"), agent.reviewer_name,
                                                  cfg.get("prometheusUrl")))
    approval = Approval(agent, client, tracker)
    worker = IdeaWorker(agent, client, progress, conversation, approval, user_id, job_for=job_for)
    worker.start()
    listen(worker)
    return worker
