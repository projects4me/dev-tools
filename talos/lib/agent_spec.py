"""An agent's Gaia agentspec as Talos settings: for an agent.json with gaiaAgent, Gaia owns the role prompt, model
and limits (GET agentspec/{id}, read through gaia-mcp's Gaia.agent_spec)."""

import threading

from .config import TalosError, log

SPEC_VERSION = 1
PERMISSION_MODES = ("default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions")   # claude --permission-mode
FALLBACKS = ("max_turns", "max_iterations", "permission_mode")    # agent.json's values when the spec leaves them unset


def settings_from(spec, fallback):
    """The Agent fields the spec sets: role prompt, models, turn cap, runs per issue and permission mode.
    `fallback` holds agent.json's values for the limits Gaia leaves null. Raises TalosError for a spec Talos can't
    run (another specVersion, no role prompt or model, a permission mode Claude Code doesn't have)."""
    if not isinstance(spec, dict) or spec.get("specVersion") != SPEC_VERSION:
        version = spec.get("specVersion") if isinstance(spec, dict) else None
        raise TalosError(f"agentspec version {version} is not supported (Talos reads version {SPEC_VERSION})")
    config = spec.get("config") or {}
    role_prompt = (config.get("rolePrompt") or "").strip()
    if not role_prompt:
        raise TalosError("agentspec has no rolePrompt")
    model_config = config.get("modelConfig") or {}
    model = str(model_config.get("model") or "").strip()
    if not model:
        raise TalosError("agentspec modelConfig has no model")
    by_complexity = model_config.get("byComplexity") or {}
    if not isinstance(by_complexity, dict):
        raise TalosError("agentspec modelConfig.byComplexity must map issue complexities to models")
    mode = config.get("permissionMode") or fallback["permission_mode"]
    if mode not in PERMISSION_MODES:
        raise TalosError(f"agentspec permissionMode '{mode}' is not a Claude Code mode "
                         f"({', '.join(PERMISSION_MODES)})")
    return {
        "role_prompt": role_prompt,
        "model_default": model,
        "model_escalate": str(model_config.get("escalate") or model),
        "model_by_complexity": {str(k): str(v) for k, v in by_complexity.items()},
        "max_turns": _limit(config, "maxTurns", fallback["max_turns"]),
        "max_iterations": _limit(config, "maxRunsPerIssue", fallback["max_iterations"]),
        "permission_mode": mode,
    }


def _limit(config, key, fallback):
    """A positive whole-number limit from the spec, or agent.json's value when Gaia leaves it null."""
    value = config.get(key)
    if value is None:
        return fallback
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TalosError(f"agentspec {key} must be a positive whole number, not {value!r}")
    return value


class SpecSource:
    """Keeps one agent's settings in step with its Gaia spec. A fetch that fails (Gaia unreachable, a spec Talos
    can't run) keeps the last good spec; with none yet, the agent can't run."""

    def __init__(self, agent, fetch):
        self.agent = agent
        self.fetch = fetch          # fetch(gaia_agent) -> the spec, as the agent's own Gaia user
        self.fallback = {k: getattr(agent, k) for k in FALLBACKS}
        self._good = None
        self._ignored_noted = False
        self._lock = threading.Lock()

    def refresh(self):
        """Fetch the spec and apply it to the agent (shared by its worker threads)."""
        with self._lock:
            try:
                spec = self.fetch(self.agent.gaia_agent)
                values = settings_from(spec, self.fallback)
            except TalosError as e:
                if self._good is None:
                    raise TalosError(f"no usable agentspec for Gaia agent '{self.agent.gaia_agent}': {e}")
                log(f"agentspec for '{self.agent.gaia_agent}' unavailable, keeping the last good one: {e}",
                    self.agent.name)
                return
            self._note_ignored(spec)
            for name, value in values.items():
                setattr(self.agent, name, value)
            self._good = values

    def _note_ignored(self, spec):
        if self._ignored_noted or not (spec.get("toolGrants") or spec.get("skills")):
            return
        self._ignored_noted = True
        log(f"agentspec for '{self.agent.gaia_agent}': tool grants and skills are not applied yet; "
            f"tools come from agent.json", self.agent.name)


def follow(agent, fetch):
    """For an agent with gaiaAgent: applies its spec now (TalosError when there is none) and returns the refresh to
    call before each run. None for an agent configured in agent.json alone."""
    if not agent.gaia_agent:
        return None
    source = SpecSource(agent, fetch)
    source.refresh()
    return source.refresh
