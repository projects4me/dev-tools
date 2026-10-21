"""The Nestor UI server: the static page, and /sim/* forwarded to the stand-ins as the engineer.

It plays Prometheus' part: the page talks to "Gaia" (the idea resources) and listens to "Hermes" (the event
stream); the server adds who the engineer is, as Prometheus' OAuth token would. Nothing else happens here: the
agent runs in Talos.

    GET  /api/info                  who the engineer and the agent are
    *    /sim/api/v1/idea…          sim Gaia's idea resources (GET, POST, PATCH; ideascreen/<id> is a PNG)
    GET  /sim/hermes/events?…       sim Hermes' event stream (SSE), passed through as it arrives
    GET  /sim/hermes/presence?…     whether the agent is listening (Talos running)

Local only: bound to loopback, other Host headers refused, and writes need `X-Nestor: 1` (a cross-site page can't
send it without a preflight this server never answers).
"""

import json
import mimetypes
import re
import urllib.error
import urllib.request
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .config import WEB_DIR

FORWARD = re.compile(r"^/sim(/api/v1/(idea|ideamessage|ideaplan|ideascreen)(/\d+)?|/hermes/(events|presence))$")
MAX_BODY = 1_000_000
LOOPBACK = ("127.0.0.1", "localhost", "::1")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
       "font-src 'self' https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:; object-src 'none'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
for _type, _ext in (("font/woff2", ".woff2"), ("font/woff", ".woff"), ("image/png", ".png"),
                    ("text/javascript", ".js"), ("text/css", ".css")):
    mimetypes.add_type(_type, _ext)   # the Windows registry may not know them


def make_handler(cfg, allowed_hosts):
    sim = cfg["simUrl"].rstrip("/")
    info = {"engineer": {"id": cfg["engineerUserId"], "name": cfg["engineerName"] or "You"},
            "agent": {"id": cfg["agentUserId"], "name": cfg.get("agentName") or "Agent Nestor"}}

    class Handler(BaseHTTPRequestHandler):
        server_version = "Nestor"

        def log_message(self, fmt, *args):
            if self.command != "GET":
                super().log_message(fmt, *args)

        def _dispatch(self):
            if self.headers.get("Host", "").lower() not in allowed_hosts:
                return self._json(421, {"error": "unexpected Host"})
            path = urlparse(self.path).path
            if path == "/api/info" and self.command == "GET":
                return self._json(200, info)
            if FORWARD.match(path):
                if self.command != "GET" and self.headers.get("X-Nestor") != "1":
                    return self._json(403, {"error": "missing X-Nestor header"})
                return self._forward(path)
            if self.command == "GET":
                return self._static(path)
            return self._json(404, {"error": "no such route"})

        do_GET = do_POST = do_PATCH = _dispatch

        def _json(self, status, payload):
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _forward(self, path):
            length = 0
            if self.command != "GET":
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if length < 0 or length > MAX_BODY:
                    return self._json(400, {"error": "bad or too large Content-Length"})
            body = self.rfile.read(length) if length else None
            query = urlparse(self.path).query
            req = urllib.request.Request(f"{sim}{path[len('/sim'):]}" + (f"?{query}" if query else ""), data=body,
                                         method=self.command,
                                         headers={"Content-Type": "application/json",
                                                  "X-Sim-Actor": cfg["engineerUserId"]})
            stream = path.endswith("/hermes/events")
            try:
                resp = urllib.request.urlopen(req, timeout=None if stream else 30)
            except urllib.error.HTTPError as e:
                data = e.read()
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            except (urllib.error.URLError, OSError) as e:
                return self._json(502, {"error": f"the sim stand-ins are not running at {sim} ({e}); "
                                                 f"start them with dev-tools/sim/sim serve"})
            with resp:
                self.send_response(resp.status)
                self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                # screen PNGs never change (a revision is a new id): let the browser keep them
                self.send_header("Cache-Control", resp.headers.get("Cache-Control") if "/ideascreen/" in path
                                 else "no-store")
                if stream:
                    self.end_headers()
                    try:
                        for line in resp:            # SSE, passed through line by line as it arrives
                            self.wfile.write(line)
                            self.wfile.flush()
                    except OSError:
                        pass                         # the page or the stand-ins went away
                    self.close_connection = True
                else:
                    data = resp.read()
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

        def _static(self, path):
            name = "index.html" if path in ("", "/") else path.lstrip("/")
            target = (WEB_DIR / name).resolve()
            if WEB_DIR.resolve() not in target.parents or not target.is_file():
                return self._json(404, {"error": "not found"})
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", (mimetypes.guess_type(target.name)[0] or "application/octet-stream")
                             + ("; charset=utf-8" if target.suffix in (".html", ".js", ".css") else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            self.wfile.write(data)

    return Handler


class Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second process bind a port that is in use, so an old instance would keep
    # answering; fail instead ("address already in use").
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def serve(cfg, port):
    host = cfg.get("host", "127.0.0.1")
    if host not in LOOPBACK:
        raise ValueError(f"Nestor only listens on this machine (127.0.0.1), not on '{host}'")
    allowed = set()
    httpd = Server((host, port), make_handler(cfg, allowed))
    bound = httpd.server_address[1]          # the real port, also when asked for 0
    allowed.update(f"{h}:{bound}" for h in ("127.0.0.1", "localhost"))
    return httpd
