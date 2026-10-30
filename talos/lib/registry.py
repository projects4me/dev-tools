"""Agent registry: one folder per agent under agents/, each with agent.json and its role prompt."""

import json
import os
import sys
from dataclasses import dataclass, field

from .config import AGENTS_DIR, DEV_TOOLS, LOCAL_DIR, TalosError, log

sys.path.insert(0, str(DEV_TOOLS / "installer"))
import testslots  # noqa: E402  (stdlib only: the slot count p4m uses)


def installed_test_slots():
    """Number of test slots p4m provides: TEST_SLOTS from p4m.env (the real environment wins)."""
    cfg = {}
    try:
        lines = (LOCAL_DIR / "p4m.env").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if sep and not key.startswith("#"):
            cfg[key.strip()] = value.strip().strip('"').strip("'")
    if "TEST_SLOTS" in os.environ:
        cfg["TEST_SLOTS"] = os.environ["TEST_SLOTS"]
    return testslots.slot_count(cfg)


@dataclass
class Action:
    """What an agent does when handed an issue in a given status."""
    prompt: str
    resume: bool = False


AUTO_RETRY_REASONS = ("turn_budget", "cost_budget", "interrupted", "branch_checked_out", "talos_error")


@dataclass
class Orchestration:
    """What an orchestrator agent (kind orchestrator) feeds its worker agent; no Claude runs of its own."""
    worker: str                         # the issues agent it feeds (e.g. "odysseus")
    milestone_prefix: str = "aga-"      # only milestones whose name starts with this (any case)
    owners: list = field(default_factory=list)   # Gaia user ids whose issues it may assign (empty = worker's handoffTo)
    max_in_flight: int = 0              # issues assigned to the worker at once (0 = the worker's maxParallel)
    sweep_minutes: float = 5            # periodic sweep, besides the event-driven ones
    debounce_seconds: float = 5         # wait after a Hermes event before sweeping, so a burst gives one sweep
    auto_retry_reasons: tuple = AUTO_RETRY_REASONS   # handback reason codes that are restarted automatically
    max_auto_retries: int = 2           # automatic restarts per issue (cleared by `talos reset`)
    fail_streak_limit: int = 3          # consecutive failed or blocked handbacks before it pauses itself
    auto_merge_milestones: tuple = ()   # milestones (exact name, any case) whose CodeRabbit-clean PRs it merges itself
    max_auto_merges_per_day: int = 5    # automatic merges per day; past it, PRs go to the reviewer
    auto_merge_wait_minutes: float = 60  # how long a still-pending merge gate (CI, CodeRabbit) is waited for


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
    dependency_done_statuses: tuple = ("done", "closed")   # statuses a "Depends on" issue must be in (any case)
    max_turns: int = 0                  # turns per run before Claude stops and the issue goes back (0 = no limit)
    limit_comment_hours: float = 1.0    # comment on the issue when a Claude usage-limit pause lasts longer than this
    kind: str = "issues"                # "issues": works assigned issues; "ideas": brainstorms ideas (Gaia, or the sim);
                                        # "orchestrator": assigns issues to another agent (no Claude runs)
    events: list = field(default_factory=list)       # event names to subscribe to (kind ideas)
    builtin_tools: str = ""             # `claude --tools`: the built-in tools loaded at all (empty = all)
    denied_tools: list = field(default_factory=list)  # `--disallowedTools`
    limits_by_model: dict = field(default_factory=dict)  # model -> {"maxTurns", "maxBudgetUsd"} for that model's runs
    ship_prompt: str = ""               # fresh session that ships after a ready_to_ship outcome ("" = not offered)
    bot_round_prompt: str = ""          # run that answers CodeRabbit's findings ("" = hand off without bot rounds)
    bot_review_rounds: int = 3          # bot rounds per issue before it is handed off with open bot threads
    bot_review_wait_minutes: float = 20     # per round: how long a pending CodeRabbit review is waited for
    bot_review_poll_seconds: int = 60   # how often waiting issues are checked with `ai rabbit`
    env: dict = field(default_factory=dict)          # extra environment for the Claude process
    max_parallel: int = 1               # worker threads: issues run at once (1 = one at a time)
    max_parallel_by_repo: dict = field(default_factory=dict)   # repo -> runs at once
    max_parallel_by_model: dict = field(default_factory=lambda: {"opus": 1})   # model -> runs at once
    test_slots: int = 0                 # test slots to lease per run (0 = max_parallel; at most TEST_SLOTS in p4m.env)
    runner: dict = field(default_factory=dict)       # where runs execute: {} / {"type": "local"} or a container (container.py)
    extra: dict = field(default_factory=dict)
    orchestration: Orchestration = None     # kind orchestrator only

    def action_for(self, status):
        return self.actions.get(status)

    def repo_for(self, project_name):
        return self.projects.get(project_name)


REQUIRED = ("name", "gaiaEnv", "projects", "acceptAssignmentsFrom", "handoffTo", "actions", "model", "rolePrompt")
REQUIRED_ORCHESTRATOR = ("name", "gaiaEnv", "worker")
KINDS = ("issues", "ideas", "orchestrator")
RUNNERS = ("local", "container")


def load_agent(folder):
    """agent.json, overlaid by the engineer's agent.local.json (git-ignored, written by `p4m configure`):
    who may hand this agent work, who gets hand-offs, and the reviewer's name."""
    path = folder / "agent.json"
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    local = LOCAL_DIR / "talos" / "agents" / folder.name / "agent.local.json"
    if local.exists():
        with open(local, encoding="utf-8") as f:
            raw.update(json.load(f))
    if raw.get("kind") == "orchestrator":
        return _load_orchestrator(path, raw)
    missing = [k for k in REQUIRED if k not in raw]
    if missing:
        raise TalosError(f"{path}: missing {', '.join(missing)}")
    if not raw["acceptAssignmentsFrom"] or not raw["handoffTo"]:
        raise TalosError(f"{folder.name}: no reviewer configured (acceptAssignmentsFrom/handoffTo); "
                         f"run `dev-tools/installer/p4m configure`, which writes {local.name}")
    role_file = folder / raw["rolePrompt"]
    if not role_file.exists():
        raise TalosError(f"{path}: rolePrompt {role_file.name} not found")
    env_file = LOCAL_DIR / raw["gaiaEnv"]
    if not env_file.exists():
        raise TalosError(f"{path}: gaiaEnv {env_file} not found")
    reserved = [k for k in raw.get("env", {}) if str(k).upper().startswith("GAIA_")]
    if reserved:
        raise TalosError(f"{path}: env may not set {', '.join(reserved)} (the agent's credentials come from gaiaEnv)")
    runner = raw.get("runner") or {}
    if runner.get("type", "local") not in RUNNERS:
        raise TalosError(f"{path}: runner.type must be one of {', '.join(RUNNERS)}")
    if runner.get("type") == "container" and not runner.get("image"):
        raise TalosError(f"{path}: a container runner needs runner.image")
    kind = raw.get("kind", "issues")
    if kind not in KINDS:
        raise TalosError(f"{path}: kind must be one of {', '.join(KINDS)}")
    test_slots = int(raw.get("testSlots", 0))
    if test_slots > 0:
        try:
            available = installed_test_slots()
        except testslots.SlotError as e:
            raise TalosError(f"{path}: {e}")
        if test_slots > available:
            raise TalosError(f"{path}: testSlots is {test_slots} but TEST_SLOTS in p4m.env is {available}: "
                             f"lower testSlots or raise TEST_SLOTS")
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
        dependency_done_statuses=tuple(raw.get("dependencyDoneStatuses") or ("done", "closed")),
        max_turns=int(raw.get("maxTurns", 0)),
        limit_comment_hours=float(raw.get("limitCommentHours", 1.0)),
        kind=kind,
        events=list(raw.get("events", [])),
        builtin_tools=raw.get("builtinTools", ""),
        denied_tools=list(raw.get("deniedTools", [])),
        limits_by_model=dict(raw.get("limitsByModel", {})),
        ship_prompt=raw.get("shipPrompt", ""),
        bot_round_prompt=raw.get("botRoundPrompt", ""),
        bot_review_rounds=max(0, int(raw.get("botReviewRounds", 3))),
        bot_review_wait_minutes=float(raw.get("botReviewWaitMinutes", 20)),
        bot_review_poll_seconds=max(5, int(raw.get("botReviewPollSeconds", 60))),
        env={str(k): str(v) for k, v in raw.get("env", {}).items()},
        max_parallel=max(1, int(raw.get("maxParallel", 1))),
        max_parallel_by_repo={str(k): int(v) for k, v in raw.get("maxParallelByRepo", {}).items()},
        max_parallel_by_model={str(k): int(v) for k, v in raw.get("maxParallelByModel", {"opus": 1}).items()},
        test_slots=test_slots,
        runner=dict(runner),
        extra={k: raw[k] for k in ("knowledgeMap", "ideaSource") if k in raw},
    )


def _load_orchestrator(path, raw):
    """An orchestrator: a Gaia identity and the worker it feeds; it has no actions, model or role prompt."""
    missing = [k for k in REQUIRED_ORCHESTRATOR if k not in raw]
    if missing:
        raise TalosError(f"{path}: missing {', '.join(missing)}")
    env_file = LOCAL_DIR / raw["gaiaEnv"]
    if not env_file.exists():
        raise TalosError(f"{path}: gaiaEnv {env_file} not found")
    prefix = str(raw.get("milestonePrefix", "aga-")).strip()
    if not prefix:
        raise TalosError(f"{path}: milestonePrefix may not be empty (it limits the orchestrator to its milestones)")
    reasons = raw.get("autoRetryReasons", list(AUTO_RETRY_REASONS))
    if not isinstance(reasons, list) or not all(isinstance(r, str) and r for r in reasons):
        raise TalosError(f"{path}: autoRetryReasons must be a list of reason codes")
    merge_milestones = raw.get("autoMergeMilestones", [])
    if not isinstance(merge_milestones, list) or not all(isinstance(m, str) and m.strip() for m in merge_milestones):
        raise TalosError(f"{path}: autoMergeMilestones must be a list of milestone names")
    numbers = {}
    for key, default, least in (("maxInFlight", 0, 0), ("maxAutoRetries", 2, 0), ("failStreakLimit", 3, 1),
                                ("maxAutoMergesPerDay", 5, 0)):
        try:
            numbers[key] = int(raw.get(key, default))
        except (TypeError, ValueError):
            raise TalosError(f"{path}: {key} must be a whole number")
        if numbers[key] < least:
            raise TalosError(f"{path}: {key} must be at least {least}")
    try:
        sweep = float(raw.get("sweepMinutes", 5))
        debounce = float(raw.get("debounceSeconds", 5))
        merge_wait = float(raw.get("autoMergeWaitMinutes", 60))
    except (TypeError, ValueError):
        raise TalosError(f"{path}: sweepMinutes, debounceSeconds and autoMergeWaitMinutes must be numbers")
    if sweep <= 0 or debounce < 0 or merge_wait < 0:
        raise TalosError(f"{path}: sweepMinutes must be above 0, debounceSeconds and autoMergeWaitMinutes not below 0")
    orchestration = Orchestration(
        worker=str(raw["worker"]),
        milestone_prefix=prefix,
        owners=[str(u) for u in raw.get("owners", [])],
        max_in_flight=numbers["maxInFlight"],
        sweep_minutes=sweep,
        debounce_seconds=debounce,
        auto_retry_reasons=tuple(reasons),
        max_auto_retries=numbers["maxAutoRetries"],
        fail_streak_limit=numbers["failStreakLimit"],
        auto_merge_milestones=tuple(m.strip() for m in merge_milestones),
        max_auto_merges_per_day=numbers["maxAutoMergesPerDay"],
        auto_merge_wait_minutes=merge_wait,
    )
    return Agent(
        name=raw["name"], display_name=raw.get("displayName", raw["name"]), role=raw.get("role", ""),
        gaia_env=str(env_file), projects={}, accept_from=[], handoff_to="", milestones=[], actions={},
        statuses={}, model_default="", model_escalate="", model_by_complexity={}, permission_mode="",
        allowed_tools=[], max_iterations=0, role_prompt="", enabled=raw.get("enabled", True),
        kind="orchestrator", orchestration=orchestration)


def load_agents(only=None):
    """All agents in agents/ (enabled ones unless `only` names one explicitly)."""
    """An agent that can't load (e.g. not configured on this machine yet) is reported and skipped, so one agent's
    setup never stops another; naming it with `only` raises the error instead."""
    agents = []
    for folder in sorted(p for p in AGENTS_DIR.iterdir() if (p / "agent.json").exists()):
        if only and folder.name != only:
            continue
        try:
            agent = load_agent(folder)
        except TalosError as e:
            if only:
                raise
            log(f"skipping agent {folder.name}: {e}")
            continue
        if only:
            return [agent]
        if agent.enabled:
            agents.append(agent)
    if only:
        raise TalosError(f"no agent named '{only}' in {AGENTS_DIR}")
    return agents
