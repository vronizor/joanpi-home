import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .collector import Collector
from .store import Store

STATIC = Path(__file__).parent / "static"
FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


def make_handler(collector: Collector, store: Store) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/api/now":
                self._send_json(collector.latest or {"starting": True})
            elif path == "/api/history":
                self._send_json(store.history(time.time()))
            elif path in FILES:
                name, content_type = FILES[path]
                self._send(200, content_type, (STATIC / name).read_bytes())
            else:
                self._send(404, "text/plain; charset=utf-8", b"not found")

        def _send_json(self, data: dict) -> None:
            self._send(200, "application/json", json.dumps(data, ensure_ascii=False).encode())

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    return Handler


def serve(host: str, port: int, collector: Collector, store: Store) -> None:
    ThreadingHTTPServer((host, port), make_handler(collector, store)).serve_forever()
