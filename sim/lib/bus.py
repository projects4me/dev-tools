"""Sim Hermes: the same contract as hermes/ (src/contract/envelope.js, intents, rooms), delivered over SSE.

- Envelope: {schemaVersion: 1, eventId, eventName, occurredAt, projectId, resource: {type, id}, actorId, changes,
  meta?}; no other keys; eventName must be on the allowlist.
- A subscriber states intents [{projectId, eventName}]; it receives an envelope when the envelope's (projectId,
  eventName) is one of them. Ideas are user-scoped like notification.created: projectId "user:<id>", and a
  subscriber may only listen to its own user.
- Publishing is a server-side call with the shared secret (Gaia's role, here sim Gaia and Talos for progress).

When Gaia publishes idea events and Hermes allows them, subscribers move to real Hermes unchanged.
"""

import queue
import re
import threading
import uuid
from datetime import datetime, timezone

SCHEMA_VERSION = 1
ALLOWLIST = (
    "idea.created", "idea.updated", "idea.message.created", "idea.plan.created", "idea.plan.status.changed",
    "idea.issue.created", "idea.screen.created", "idea.agent.progress",
)
ENVELOPE_KEYS = {"schemaVersion", "eventId", "eventName", "occurredAt", "projectId", "resource", "actorId",
                 "changes", "meta"}
MAX_INTENTS = 100


class InvalidEnvelope(ValueError):
    pass


def envelope(event_name, project_id, resource_type, resource_id, actor_id, changes=None, meta=None):
    """A valid envelope, as Gaia's LiveEventEnvelope builds them."""
    return {"schemaVersion": SCHEMA_VERSION, "eventId": str(uuid.uuid4()), "eventName": event_name,
            "occurredAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "projectId": project_id,
            "resource": {"type": resource_type, "id": str(resource_id)},
            "actorId": None if actor_id is None else str(actor_id), "changes": changes or {}, "meta": meta or {}}


ISO_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")  # hermes errors.js


def _text(value, limit):
    return isinstance(value, str) and 0 < len(value) <= limit


def validate(env):
    """The checks of hermes/src/contract/envelope.js, with the same limits."""
    if not isinstance(env, dict):
        raise InvalidEnvelope("envelope must be an object")
    extra = set(env) - ENVELOPE_KEYS
    if extra:
        raise InvalidEnvelope(f"unexpected keys: {', '.join(sorted(extra))}")
    if env.get("schemaVersion") != SCHEMA_VERSION:
        raise InvalidEnvelope("schemaVersion must be 1")
    if env.get("eventName") not in ALLOWLIST:
        raise InvalidEnvelope(f"eventName not allowed: {env.get('eventName')}")
    for key in ("eventId", "projectId"):
        if not _text(env.get(key), 255):
            raise InvalidEnvelope(f"{key} must be a non-empty string of at most 255 characters")
    if not isinstance(env.get("occurredAt"), str) or not ISO_TIMESTAMP.match(env["occurredAt"]):
        raise InvalidEnvelope("occurredAt must be an ISO-8601 timestamp")
    resource = env.get("resource")
    if not isinstance(resource, dict) or set(resource) != {"type", "id"}:
        raise InvalidEnvelope("resource must be {type, id}")
    if not _text(resource["type"], 100) or not _text(resource["id"], 255):
        raise InvalidEnvelope("resource type (at most 100) and id (at most 255) must be non-empty strings")
    if not (env.get("actorId") is None or _text(env["actorId"], 255)):
        raise InvalidEnvelope("actorId must be null or a non-empty string of at most 255 characters")
    if not isinstance(env.get("changes"), dict):
        raise InvalidEnvelope("changes must be an object")
    if "meta" in env and not isinstance(env["meta"], dict):
        raise InvalidEnvelope("meta must be an object")
    return env


class Subscription:
    def __init__(self, user_id, intents):
        self.user_id = user_id
        self.intents = intents
        self.queue = queue.Queue(maxsize=5000)


class Bus:
    def __init__(self):
        self._subs = set()
        self._lock = threading.Lock()

    def subscribe(self, user_id, intents):
        """(subscription, accepted, rejected), checked the way Hermes' intents:set evaluates them."""
        if not isinstance(intents, list) or len(intents) > MAX_INTENTS:
            raise ValueError(f"intents must be a list of at most {MAX_INTENTS}")
        accepted, rejected, seen = [], [], set()
        for intent in intents:
            pid, name = (intent or {}).get("projectId"), (intent or {}).get("eventName")
            if name not in ALLOWLIST:
                rejected.append({**intent, "reason": "unknown_event"})
            elif pid != f"user:{user_id}":
                rejected.append({**intent, "reason": "not_member"})
            elif (pid, name) in seen:
                rejected.append({**intent, "reason": "duplicate"})
            else:
                seen.add((pid, name))
                accepted.append({"projectId": pid, "eventName": name})
        sub = Subscription(user_id, seen)
        with self._lock:
            self._subs.add(sub)
        return sub, accepted, rejected

    def unsubscribe(self, sub):
        with self._lock:
            self._subs.discard(sub)

    def publish(self, env):
        """Deliver to every subscriber with a matching intent; returns how many got it."""
        validate(env)
        key = (env["projectId"], env["eventName"])
        with self._lock:
            targets = [s for s in self._subs if key in s.intents]
        for s in targets:
            try:
                s.queue.put_nowait(env)
            except queue.Full:  # a stuck reader loses events, like a dropped socket; it reconciles on reconnect
                pass
        return len(targets)

    def online(self, user_id):
        """Whether anyone is listening as this user (e.g. Talos for an agent)."""
        with self._lock:
            return any(s.user_id == user_id for s in self._subs)
