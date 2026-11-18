"""Tiny HTTP server so the engine can run as a FREE Render Web Service.
  GET /health                                   → status JSON (also what the keep-alive ping hits)
  GET /run/capture-test?token=ADMIN&url=<live>  → runs the capture test in the background (results in dashboard feed)
  GET /run/heartbeat?token=ADMIN                → runs one heartbeat now
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .core import config

STARTED = time.time()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path in ("/", "/health"):
            return self._send(200, {"ok": True, "node": config.NODE, "light_mode": config.LIGHT,
                                    "uptime_min": round((time.time() - STARTED) / 60, 1)})
        if u.path.startswith("/run/"):
            if not config.ADMIN_TOKEN or q.get("token") != config.ADMIN_TOKEN:
                return self._send(403, {"error": "bad token"})
            if u.path == "/run/capture-test":
                if not q.get("url"):
                    return self._send(400, {"error": "add &url=<live YouTube URL>"})
                from .tools import capture_test
                threading.Thread(target=capture_test.main, args=(q["url"],), daemon=True).start()
                return self._send(202, {"started": "capture test — watch the dashboard feed"})
            if u.path == "/run/heartbeat":
                from . import heartbeat
                threading.Thread(target=heartbeat.main, daemon=True).start()
                return self._send(202, {"started": "heartbeat"})
        return self._send(404, {"error": "not found"})

    def log_message(self, *a):  # keep logs quiet
        pass


def serve():
    srv = ThreadingHTTPServer(("0.0.0.0", config.PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True, name="http").start()
    print(f"[http] listening on :{config.PORT}", flush=True)
