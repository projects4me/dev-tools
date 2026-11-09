"""An agent's Gaia agentspec as Talos settings: for a Gaia-declared agent (gaia_agents.py), Gaia owns the role
prompt, model and limits (GET agentspec/{id}, read through gaia-mcp's Gaia.agent_spec_by_id)."""

from .config import TalosError

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


def skills_prompt(spec):
    """The skills granted in Gaia, appended to the role prompt so the agent works by them."""
    parts = []
    for skill in spec.get("skills") or []:
        text = "\n\n".join(t for t in ((skill.get("description") or "").strip(), (skill.get("content") or "").strip())
                           if t)
        if text:
            parts.append(f"## Skill: {skill.get('name') or skill.get('id')}\n\n{text}")
    return "\n\n# Skills granted in Gaia\n\n" + "\n\n".join(parts) if parts else ""
