"""Sim Hermes as an event source (dev-tools/sim): the same envelopes and intents as Hermes, over SSE.

The counterpart of hermes.Listener for events real Hermes doesn't carry yet (ideas). On every (re)connect it
reports `ready` so the owner can reconcile, because like Hermes it keeps no history.
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
import uuid

from .config import log


def envelope(event_name, project_id, resource_type, resource_id, actor_id, changes=None, meta=None):
    """An envelope in Hermes' contract (schemaVersion 1)."""
    return {"schemaVersion": 1, "eventId": str(uuid.uuid4()), "eventName": event_name,
            "occurredAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "projectId": project_id,
            "resource": {"type": resource_type, "id": str(resource_id)}, "actorId": str(actor_id),
            "changes": changes or {}, "meta": meta or {}}


def parse_sse(lines):
    """(event name, data) pairs from SSE lines; comments and keepalives are skipped."""
    name, data = None, []
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            if data:
                yield name or "message", "\n".join(data)
            name, data = None, []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())


class SimSource:
    """Listens as `actor_id` with `intents`; calls on_ready() after each connect and on_event(envelope)."""

    def __init__(self, url, actor_id, intents, on_event, on_ready, name, retry_seconds=5):
        self.url, self.actor_id, self.intents = url.rstrip("/"), str(actor_id), intents
        self.on_event, self.on_ready, self.name, self.retry_seconds = on_event, on_ready, name, retry_seconds

    def start(self):
        threading.Thread(target=self._loop, name=f"sim-{self.name}", daemon=True).start()

    def _loop(self):
        while True:
            try:
                self._listen_once()
            except (urllib.error.URLError, OSError) as e:
                log(f"sim Hermes not reachable at {self.url} ({e}); retrying in {self.retry_seconds}s", self.name)
            except Exception as e:
                log(f"sim listener error: {e}", self.name)
            time.sleep(self.retry_seconds)

    def _listen_once(self):
        query = urllib.parse.urlencode({"intents": json.dumps(self.intents)})
        req = urllib.request.Request(f"{self.url}/hermes/events?{query}", headers={"X-Sim-Actor": self.actor_id})
        with urllib.request.urlopen(req, timeout=60) as resp:
            lines = (line.decode("utf-8", "replace") for line in resp)
            for name, data in parse_sse(lines):
                payload = json.loads(data)
                if name == "ready":
                    log(f"listening to sim Hermes ({len(payload.get('accepted') or [])} intents, "
                        f"{len(payload.get('rejected') or [])} rejected)", self.name)
                    self.on_ready()
                elif name == "domain:event":
                    self.on_event(payload)
        log("sim Hermes closed the stream; reconnecting", self.name)
