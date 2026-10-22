"""Idea resources as the agent on sim Gaia's REST API (dev-tools/sim), until the sim is retired; client.py is the
same interface on real Gaia. (Live progress goes through progress.py.)"""

import base64
import json
import urllib.error
import urllib.request

from ..config import TalosError


class SimIdeasClient:
    def __init__(self, url, actor_id):
        self.url, self.actor_id = url.rstrip("/"), str(actor_id)

    def _call(self, method, path, body=None, raw=False):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(f"{self.url}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json", "X-Sim-Actor": self.actor_id})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                content = resp.read()
                return content if raw else json.loads(content or b"{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("error", detail)
            except ValueError:
                pass
            raise TalosError(f"sim Gaia {method} {path}: {e.code} {detail}")
        except (urllib.error.URLError, OSError) as e:
            raise TalosError(f"sim Gaia not reachable at {self.url}: {e}")

    # -- reads --------------------------------------------------------------------------------------------------

    def ideas(self):
        return self._call("GET", "/api/v1/idea")["data"]

    def idea(self, idea_id):
        """{idea, messages, plans (each with issues)}."""
        return self._call("GET", f"/api/v1/idea/{int(idea_id)}")["data"]

    # -- writes -------------------------------------------------------------------------------------------------

    def set_state(self, idea_id, agent_state):
        return self._call("PATCH", f"/api/v1/idea/{int(idea_id)}", {"agent_state": agent_state})

    def mark_created(self, idea_id):
        return self._call("PATCH", f"/api/v1/idea/{int(idea_id)}", {"status": "created"})

    def post(self, idea_id, role, body, steps=None, plan_id=None, cost=None, screens=None):
        return self._call("POST", "/api/v1/ideamessage", {"idea_id": int(idea_id), "role": role, "body": body,
                                                          "steps": steps, "plan_id": plan_id, "cost_usd": cost,
                                                          "screens": screens or None})["data"]

    def add_screen(self, idea_id, screen, png):
        """A rendered screen (screens.py dict + its PNG bytes); returns its id, which post() takes in screens."""
        d = self._call("POST", "/api/v1/ideascreen", {
            "idea_id": int(idea_id), "key": screen["key"], "title": screen["title"], "width": screen["width"],
            "height": screen["height"], "png_base64": base64.b64encode(png).decode("ascii")})["data"]
        return d["id"]

    def screen_png(self, screen_id):
        return self._call("GET", f"/api/v1/ideascreen/{int(screen_id)}", raw=True)

    def add_plan(self, idea_id, body):
        """(plan id, version)."""
        d = self._call("POST", "/api/v1/ideaplan", {"idea_id": int(idea_id), "body": body})["data"]
        return d["id"], d["version"]

    def set_plan_status(self, plan_id, status, error=None):
        return self._call("PATCH", f"/api/v1/ideaplan/{int(plan_id)}", {"status": status, "error": error})["data"]

    def add_issue(self, plan_id, key, issue):
        return self._call("POST", "/api/v1/ideaissue", {
            "plan_id": int(plan_id), "key": key, "number": issue["issueNumber"],
            "project_code": issue.get("projectShortcode"), "subject": issue.get("subject"), "url": issue.get("url")})
