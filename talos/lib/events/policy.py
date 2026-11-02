"""Events agents' decisions as pure functions: which events start a run, and the prompt a run gets."""

import re

PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def key_for(envelope, agent, project_ids):
    """(event name, resource id, project id) when the envelope is one of the agent's triggers in one of its
    projects, else None. The key is what a run is deduplicated on: Hermes may deliver the same event again
    (reconnects, the same resource seen from several projects)."""
    if not isinstance(envelope, dict):
        return None
    name = str(envelope.get("eventName") or "")
    project_id = str(envelope.get("projectId") or "")
    if name not in agent.triggers or project_id not in set(project_ids.values()):
        return None
    resource_id = _resource_id(envelope)
    if not resource_id:
        return None
    return name, resource_id, project_id


def variables(envelope, project_ids, project_codes):
    """The values a trigger prompt can use: the event's changes (e.g. {{name}}) and, winning over them,
    {{eventName}}, {{resourceId}}, {{projectId}}, {{projectName}}, {{projectShortcode}} and {{actorId}}."""
    project_id = str(envelope.get("projectId") or "")
    project_name = next((n for n, pid in project_ids.items() if pid == project_id), "")
    changes = envelope.get("changes")
    values = {str(k): v for k, v in (changes if isinstance(changes, dict) else {}).items()
              if isinstance(v, (str, int, float, bool)) or v is None}
    values.update({
        "eventName": envelope.get("eventName") or "",
        "resourceId": _resource_id(envelope),
        "projectId": project_id,
        "projectName": project_name,
        "projectShortcode": project_codes.get(project_name, ""),
        "actorId": str(envelope.get("actorId") or ""),
    })
    return values


def _resource_id(envelope):
    resource = envelope.get("resource")
    return str(resource.get("id") or "") if isinstance(resource, dict) else ""


def render(template, values):
    """The template with each {{key}} replaced by its value (None as empty). An unknown {{key}} is left as it is,
    so a misspelt placeholder shows in the prompt and the run log instead of silently vanishing."""
    def sub(match):
        key = match.group(1)
        if key not in values:
            return match.group(0)
        value = values[key]
        return "" if value is None else str(value)
    return PLACEHOLDER.sub(sub, template)
