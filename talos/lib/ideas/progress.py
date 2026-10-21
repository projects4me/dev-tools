"""Live progress of an agent's run (text as written, tool steps) for the engineer watching.

Published as `idea.agent.progress` on sim Hermes' /publish. Real Hermes only accepts events from Gaia, so once
ideas move there this needs a path of its own (e.g. Gaia relaying, or an agent publish channel in Hermes); the
idea client (client.py) is unaffected by that.
"""

import json
import urllib.request

from ..simbus import envelope


class ProgressPublisher:
    def __init__(self, url, secret, actor_id):
        self.url, self.secret, self.actor_id = url.rstrip("/"), secret, str(actor_id)

    def publish(self, idea, payload):
        """Best effort: a lost progress event only means a less lively UI; the stored reply is what counts."""
        env = envelope("idea.agent.progress", f"user:{idea['owner']}", "idea", idea["id"], self.actor_id, payload,
                       {"ideaId": idea["id"]})
        req = urllib.request.Request(f"{self.url}/publish", data=json.dumps(env).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "X-Hermes-Secret": self.secret})
        try:
            with urllib.request.urlopen(req, timeout=10):
                pass
        except OSError:
            pass
