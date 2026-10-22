"""The stand-ins over HTTP, on 127.0.0.1 only.

Sim Gaia (CRUD, JSON; the caller is the Gaia user id in X-Sim-Actor, which Gaia would take from the OAuth token):
    GET   /api/v1/idea                 ideas the caller owns or is the agent of
    POST  /api/v1/idea                 {title, agent}
    GET   /api/v1/idea/<id>            idea + messages + plans (each with its issues) + agent_online
    PATCH /api/v1/idea/<id>            {title} | {status} | {agent_state}
    POST  /api/v1/ideamessage          {idea_id, role, body, steps?, plan_id?, cost_usd?}
    POST  /api/v1/ideaplan             {idea_id, body}
    PATCH /api/v1/ideaplan/<id>        {status, error?}
    POST  /api/v1/ideaissue            {plan_id, key, number, project_code, subject, url}
    POST  /api/v1/ideascreen           {idea_id, key, title, width, height, png_base64} (the agent)
    GET   /api/v1/ideascreen/<id>      the PNG (owner or agent)
Sim Hermes:
    POST  /publish                     an envelope, with X-Hermes-Secret (as hermes/src/http/routes/publish.js)
    GET   /hermes/events?intents=JSON  SSE: `ready` {accepted, rejected}, then `domain:event` envelopes
    GET   /hermes/presence?user=<id>   {online}: is anyone listening as that user
"""

import base64
import binascii
import hmac
import json
import queue
import re
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .bus import InvalidEnvelope
from .ideas import Forbidden, NotFound

MAX_BODY = 1_000_000
MAX_SCREEN_BODY = 12_000_000   # a base64 PNG screen (ideas.MAX_PNG) and its fields
KEEPALIVE_SECONDS = 15
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def make_handler(ideas, bus, secret, allowed_hosts):
    routes = [
        ("GET", r"/api/v1/idea", "list_ideas"),
        ("POST", r"/api/v1/idea", "create_idea"),
        ("GET", r"/api/v1/idea/(\d+)", "get_idea"),
        ("PATCH", r"/api/v1/idea/(\d+)", "update_idea"),
        ("POST", r"/api/v1/ideamessage", "create_message"),
        ("POST", r"/api/v1/ideaplan", "create_plan"),
        ("PATCH", r"/api/v1/ideaplan/(\d+)", "update_plan"),
        ("POST", r"/api/v1/ideaissue", "create_issue"),
        ("POST", r"/api/v1/ideascreen", "create_screen"),
        ("GET", r"/api/v1/ideascreen/(\d+)", "get_screen"),
        ("POST", r"/publish", "publish"),
        ("GET", r"/hermes/events", "events"),
        ("GET", r"/hermes/presence", "presence"),
        ("GET", r"/health", "health"),
    ]

    class Handler(BaseHTTPRequestHandler):
        server_version = "SimGaiaHermes"

        def log_request(self, code="-", size="-"):
            # reads and Talos' live progress (a /publish about every second while an agent writes) are routine;
            # print writes and anything that failed
            routine = self.command == "GET" or urlparse(self.path).path == "/publish"
            if not routine or (isinstance(code, int) and code >= 400):
                super().log_request(code, size)

        def _dispatch(self):
            if self.headers.get("Host", "").lower() not in allowed_hosts:
                return self._json(421, {"error": "unexpected Host"})
            url = urlparse(self.path)
            self.query = parse_qs(url.query)
            for method, pattern, name in routes:
                m = re.fullmatch(pattern, url.path)
                if m and method == self.command:
                    try:
                        return getattr(self, name)(*(int(g) for g in m.groups()))
                    except NotFound as e:
                        return self._json(404, {"error": str(e)})
                    except Forbidden as e:
                        return self._json(403, {"error": str(e)})
                    except (ValueError, InvalidEnvelope) as e:
                        return self._json(400, {"error": str(e)})
                    except Exception as e:
                        self.log_error("%s %s failed: %r", self.command, url.path, e)
                        return self._json(500, {"error": f"{type(e).__name__}: {e}"})
            return self._json(404, {"error": "no such route"})

        do_GET = do_POST = do_PATCH = _dispatch

        # -- plumbing ---------------------------------------------------------------------------------------------

        def _actor(self):
            actor = (self.headers.get("X-Sim-Actor") or "").strip()
            if not actor:
                raise Forbidden("missing X-Sim-Actor (the caller's Gaia user id)")
            return actor

        def _body(self, limit=MAX_BODY):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ValueError("bad Content-Length")
            if length < 0 or length > limit:
                raise ValueError("bad or too large Content-Length")
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                raise ValueError("body is not JSON")
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
            return body

        def _json(self, status, payload):
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        # -- sim Gaia ---------------------------------------------------------------------------------------------

        def health(self):
            self._json(200, {"ok": True})

        def list_ideas(self):
            self._json(200, {"data": ideas.list(self._actor())})

        def create_idea(self):
            b = self._body()
            self._json(201, {"data": ideas.create(self._actor(), b.get("title"), b.get("agent"))})

        def get_idea(self, idea_id):
            self._json(200, {"data": ideas.get(idea_id, self._actor())})

        def update_idea(self, idea_id):
            self._json(200, {"data": ideas.update(idea_id, self._actor(), self._body())})

        def create_message(self, *_):
            b = self._body()
            mid = ideas.add_message(self._actor(), int(b.get("idea_id") or 0), b.get("role"), b.get("body"),
                                    steps=b.get("steps"), plan_id=b.get("plan_id"), cost=b.get("cost_usd"),
                                    screens=b.get("screens"))
            self._json(201, {"data": {"id": mid}})

        def create_plan(self, *_):
            b = self._body()
            pid, version = ideas.add_plan(self._actor(), int(b.get("idea_id") or 0), b.get("body"))
            self._json(201, {"data": {"id": pid, "version": version}})

        def update_plan(self, plan_id):
            b = self._body()
            self._json(200, {"data": ideas.set_plan_status(self._actor(), plan_id, b.get("status"), b.get("error"))})

        def create_issue(self, *_):
            b = self._body()
            iid = ideas.add_issue(self._actor(), int(b.get("plan_id") or 0), b.get("key"), b.get("number"),
                                  b.get("project_code"), b.get("subject"), b.get("url"))
            self._json(201, {"data": {"id": iid}})

        def create_screen(self, *_):
            b = self._body(MAX_SCREEN_BODY)
            try:
                png = base64.b64decode(b.get("png_base64") or "", validate=True)
            except (binascii.Error, ValueError):
                raise ValueError("png_base64 is not base64")
            sid, version = ideas.add_screen(self._actor(), int(b.get("idea_id") or 0), b.get("key"), b.get("title"),
                                            b.get("width"), b.get("height"), png)
            self._json(201, {"data": {"id": sid, "version": version}})

        def get_screen(self, screen_id):
            data = ideas.screen_png(screen_id, self._actor())
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()
            self.wfile.write(data)

        # -- sim Hermes -------------------------------------------------------------------------------------------

        def publish(self):
            given = self.headers.get("X-Hermes-Secret") or (self.headers.get("Authorization") or "").removeprefix("Bearer ")
            if not hmac.compare_digest(given.encode(), secret.encode()):
                return self._json(401, {"error": "bad secret"})
            env = self._body()
            count = bus.publish(env)
            self._json(200, {"ok": True, "eventId": env["eventId"], "subscribers": count,
                             "room": f"v1:intent:{env['projectId']}:{env['eventName']}"})

        def presence(self):
            user = (self.query.get("user") or [""])[0]
            self._json(200, {"online": bool(user) and bus.online(user)})

        def events(self):
            actor = self._actor()
            try:
                intents = json.loads((self.query.get("intents") or ["[]"])[0])
            except ValueError:
                raise ValueError("intents is not JSON")
            sub, accepted, rejected = bus.subscribe(actor, intents)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self._sse("ready", {"accepted": accepted, "rejected": rejected})
                while True:
                    try:
                        env = sub.queue.get(timeout=KEEPALIVE_SECONDS)
                        self._sse("domain:event", env)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except OSError:
                pass  # the subscriber went away
            finally:
                bus.unsubscribe(sub)
                self.close_connection = True

        def _sse(self, name, payload):
            self.wfile.write(f"event: {name}\ndata: {json.dumps(payload)}\n\n".encode("utf-8"))
            self.wfile.flush()

    return Handler


class Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second process bind a port that is in use, so an old instance would keep
    # answering; fail instead ("address already in use").
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def serve(ideas, bus, host, port, secret):
    if host not in LOOPBACK:
        raise ValueError(f"the stand-ins only listen on this machine (127.0.0.1), not on '{host}'")
    allowed = set()
    httpd = Server((host, port), make_handler(ideas, bus, secret, allowed))
    bound = httpd.server_address[1]          # the real port, also when asked for 0
    allowed.update(f"{h}:{bound}" for h in ("127.0.0.1", "localhost"))
    return httpd
