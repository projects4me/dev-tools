"""Agent registry: one folder per agent under agents/, each with agent.json and its role prompt."""

import json
from dataclasses import dataclass, field

from .config import AGENTS_DIR, DEV_TOOLS, TalosError


@dataclass
class Action:
    """What an agent does when handed an issue in a given status."""
    prompt: str
    resume: bool = False


@dataclass
class Agent:
    name: str
    display_name: str
    role: str
    gaia_env: str
    projects: dict                      # project name -> repo folder
    accept_from: list                   # Gaia user ids allowed to hand this agent work
    handoff_to: str                     # Gaia user id that gets questions, blockers and failures
    milestones: list                    # empty = any milestone
    actions: dict                       # status name -> Action
    statuses: dict                      # outcome -> status the issue is moved to on hand-off
    model_default: str
    model_escalate: str
    model_by_complexity: dict           # issue complexity ('simple'/'complex') -> model
    permission_mode: str
    allowed_tools: list
    max_iterations: int                 # Claude runs per issue before it needs a human (0 = no limit)
    role_prompt: str
    reviewer_name: str = "your reviewer"   # the engineer this copy of Talos works for
    enabled: bool = True
    pickup_status: str = None           # status set when Talos starts work on an issue (None = leave it)
    resume_max_idle_minutes: int = 55   # resume a session only if its last run ended this recently (0 = always)
    max_turns: int = 0                  # turns per run before Claude stops and the issue goes back (0 = no limit)
    extra: dict = field(default_factory=dict)

    def action_for(self, status):
        return self.actions.get(status)

    def repo_for(self, project_name):
        return self.projects.get(project_name)


REQUIRED = ("name", "gaiaEnv", "projects", "acceptAssignmentsFrom", "handoffTo", "actions", "model", "rolePrompt")


def load_agent(folder):
    """agent.json, overlaid by the engineer's agent.local.json (git-ignored, written by `p4m configure`):
    who may hand this agent work, who gets hand-offs, and the reviewer's name."""
    path = folder / "agent.json"
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    local = folder / "agent.local.json"
    if local.exists():
        with open(local, encoding="utf-8") as f:
            raw.update(json.load(f))
    missing = [k for k in REQUIRED if k not in raw]
    if missing:
        raise TalosError(f"{path}: missing {', '.join(missing)}")
    if not raw["acceptAssignmentsFrom"] or not raw["handoffTo"]:
        raise TalosError(f"{folder.name}: no reviewer configured (acceptAssignmentsFrom/handoffTo); "
                         f"run `dev-tools/installer/p4m configure`, which writes {local.name}")
    role_file = folder / raw["rolePrompt"]
    if not role_file.exists():
        raise TalosError(f"{path}: rolePrompt {role_file.name} not found")
    env_file = DEV_TOOLS / raw["gaiaEnv"]
    if not env_file.exists():
        raise TalosError(f"{path}: gaiaEnv {env_file} not found")
    actions = {status: Action(prompt=a["prompt"], resume=bool(a.get("resume")))
               for status, a in raw["actions"].items()}
    return Agent(
        name=raw["name"],
        display_name=raw.get("displayName", raw["name"]),
        role=raw.get("role", ""),
        gaia_env=str(env_file),
        projects=raw["projects"],
        accept_from=[str(u) for u in raw["acceptAssignmentsFrom"]],
        handoff_to=str(raw["handoffTo"]),
        milestones=raw.get("milestones", []),
        actions=actions,
        statuses={"needs_input": "feedback", "blocked": "pending", "failed": "pending", "limit": "pending",
                  **raw.get("handoffStatuses", {})},
        model_default=raw["model"]["default"],
        model_escalate=raw["model"].get("escalate", raw["model"]["default"]),
        model_by_complexity=raw["model"].get("byComplexity", {}),
        permission_mode=raw.get("permissionMode", "dontAsk"),
        allowed_tools=raw.get("allowedTools", []),
        max_iterations=int(raw.get("maxIterations", 10)),
        role_prompt=role_file.read_text(encoding="utf-8"),
        reviewer_name=raw.get("reviewerName") or "your reviewer",
        enabled=raw.get("enabled", True),
        pickup_status=raw.get("pickupStatus"),
        resume_max_idle_minutes=int(raw.get("resumeMaxIdleMinutes", 55)),
        max_turns=int(raw.get("maxTurns", 0)),
    )


def load_agents(only=None):
    """All agents in agents/ (enabled ones unless `only` names one explicitly)."""
    agents = []
    for folder in sorted(p for p in AGENTS_DIR.iterdir() if (p / "agent.json").exists()):
        agent = load_agent(folder)
        if only:
            if agent.name == only:
                return [agent]
        elif agent.enabled:
            agents.append(agent)
    if only:
        raise TalosError(f"no agent named '{only}' in {AGENTS_DIR}")
    return agents
