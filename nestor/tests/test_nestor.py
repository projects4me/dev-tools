"""The Nestor UI server: static files, /api/info, and /sim/* forwarded to the stand-ins as the engineer.
Run: python -m unittest discover -s tests (from dev-tools/nestor)"""

import json
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.server import serve  # noqa: E402


def wait_accepting(port, budget=10):
    """Block until the server's socket accepts a connection (the thread may not be scheduled yet)."""
    deadline = time.monotonic() + budget
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


class Client:
    """urlopen without the system proxy lookup; the first request gets a bounded retry on a timeout."""
    def __init__(self):
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.warm = False

    def open(self, req, timeout=5):
        attempts = 1 if self.warm else 3
        for attempt in range(attempts):
            try:
                resp = self.opener.open(req, timeout=timeout)
                self.warm = True
                return resp
            except urllib.error.HTTPError:
                self.warm = True
                raise
            except (socket.timeout, TimeoutError, ConnectionError):
                if attempt == attempts - 1:
                    raise


class StubSim(BaseHTTPRequestHandler):
    """Records what reached it; answers JSON, or two SSE events for /hermes/events."""
    seen = []

    def log_message(self, *args):
        pass

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        StubSim.seen.append((self.command, self.path, self.headers.get("X-Sim-Actor"),
                             self.rfile.read(length) if length else b""))
        if self.path.startswith("/api/v1/ideascreen/"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", "4")
            self.end_headers()
            self.wfile.write(b"\x89PNG")
            return
        if self.path.startswith("/hermes/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'event: ready\ndata: {"accepted": []}\n\nevent: domain:event\ndata: {"eventName": "x"}\n\n')
            return
        data = json.dumps({"data": {"ok": True}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PATCH = _handle


class NestorServerTest(unittest.TestCase):
    def setUp(self):
        StubSim.seen = []
        self.sim = ThreadingHTTPServer(("127.0.0.1", 0), StubSim)
        threading.Thread(target=self.sim.serve_forever, daemon=True).start()
        cfg = {"host": "127.0.0.1", "simUrl": f"http://127.0.0.1:{self.sim.server_address[1]}",
               "engineerUserId": "1", "engineerName": "Hammad Hassan", "agentUserId": "nestor-id",
               "agentName": "Agent Nestor"}
        self.httpd = serve(cfg, 0)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        wait_accepting(self.port)
        self.client = Client()

    def tearDown(self):
        for s in (self.httpd, self.sim):
            s.shutdown()
            s.server_close()

    def call(self, method, path, body=None, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=None if body is None else json.dumps(body).encode(), headers=headers or {})
        try:
            with self.client.open(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read()

    def test_info_and_static(self):
        status, body = self.call("GET", "/api/info")
        self.assertEqual(json.loads(body)["agent"], {"id": "nestor-id", "name": "Agent Nestor"})
        self.assertEqual(self.call("GET", "/")[0], 200)
        self.assertEqual(self.call("GET", "/../config.json")[0], 404)

    def test_forwards_idea_resources_as_the_engineer(self):
        status, body = self.call("POST", "/sim/api/v1/ideamessage", {"idea_id": 1, "role": "user", "body": "hi"},
                                 {"X-Nestor": "1", "Content-Type": "application/json"})
        self.assertEqual((status, json.loads(body)["data"]), (200, {"ok": True}))
        method, path, actor, sent = StubSim.seen[-1]
        self.assertEqual((method, path, actor), ("POST", "/api/v1/ideamessage", "1"))
        self.assertEqual(json.loads(sent)["body"], "hi")

    def test_screen_images_are_forwarded(self):
        status, body = self.call("GET", "/sim/api/v1/ideascreen/3")
        self.assertEqual((status, body), (200, b"\x89PNG"))
        self.assertEqual(StubSim.seen[-1][1:3], ("/api/v1/ideascreen/3", "1"))

    def test_refuses_what_it_should(self):
        self.assertEqual(self.call("PATCH", "/sim/api/v1/idea/1", {"title": "x"})[0], 403)        # no X-Nestor
        self.assertEqual(self.call("POST", "/sim/publish", {}, {"X-Nestor": "1"})[0], 404)          # not forwarded
        self.assertEqual(self.call("POST", "/sim/api/v1/ideaissue", {}, {"X-Nestor": "1"})[0], 404)  # agent-only
        self.assertEqual(self.call("GET", "/api/info", headers={"Host": "evil.example"})[0], 421)
        self.assertEqual(StubSim.seen, [])

    def test_events_are_passed_through(self):
        status, body = self.call("GET", "/sim/hermes/events?intents=%5B%5D")
        self.assertIn(b"event: domain:event", body)
        self.assertEqual(StubSim.seen[-1][1:3], ("/hermes/events?intents=%5B%5D", "1"))


if __name__ == "__main__":
    unittest.main()
