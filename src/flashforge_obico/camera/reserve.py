"""The agent's HTTP server: fans camera frames out to any number of clients, and hosts the small
phone console next to them.

The printer's own MJPEG server allows exactly one viewer, so this is what every local viewer
(OrcaSlicer, a browser) should point at.

Built-in routes:
  GET /cameras/<i>/stream    multipart/x-mixed-replace, boundary "flashforgeobico"
  GET /cameras/<i>/snapshot  image/jpeg (503 before the first frame, 404 for an unknown camera)
  GET /healthz               "ok"
Further routes are added with `add_route`; a handler gets the regex match and the request body and
returns (status, content type, body bytes).
"""
from __future__ import annotations

import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Sequence

from .mjpeg_source import MjpegSource

_logger = logging.getLogger(__name__)

BOUNDARY = b"flashforgeobico"
_ROUTE = re.compile(r"^/cameras/(\d+)/(stream|snapshot)$")
DEFAULT_STALE_AFTER_S = 15.0
RouteHandler = Callable[[re.Match, bytes], tuple[int, str, bytes]]


class CameraReserver:
    def __init__(self, sources: Sequence[MjpegSource], port: int, *,
                 stale_after_s: float = DEFAULT_STALE_AFTER_S, host: str = "0.0.0.0"):
        self._sources = list(sources)
        self._stale_after = stale_after_s
        self._routes: list[tuple[str, re.Pattern, RouteHandler]] = []
        self._stopping = threading.Event()
        self._server = ThreadingHTTPServer((host, port), self._handler_class())
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def add_source(self, source: MjpegSource) -> None:
        """Registers a camera discovered after start-up; it is served at the next free index."""
        self._sources.append(source)
        _logger.info("camera re-server now serves %d camera(s)", len(self._sources))

    def add_route(self, method: str, pattern: str, handler: RouteHandler) -> None:
        self._routes.append((method.upper(), re.compile(pattern), handler))

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, name="reserver", daemon=True)
        self._thread.start()
        _logger.info("camera re-server listening on port %d for %d camera(s)", self.port, len(self._sources))

    def stop(self) -> None:
        self._stopping.set()
        self._server.shutdown()
        self._server.server_close()

    def _handler_class(self):
        reserver = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, fmt, *args):  # access logs for a stream are noise
                _logger.debug("reserver: " + fmt, *args)

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/healthz":
                    return self._send(200, b"ok", "text/plain")
                match = _ROUTE.match(path)
                if match:
                    if int(match.group(1)) >= len(reserver._sources):
                        return self._send(404, b"no such camera", "text/plain")
                    source = reserver._sources[int(match.group(1))]
                    return self._snapshot(source) if match.group(2) == "snapshot" else self._stream(source)
                self._dispatch("GET", path)

            def do_POST(self):
                self._dispatch("POST", self.path.split("?", 1)[0])

            def _dispatch(self, method: str, path: str):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                for route_method, pattern, handler in reserver._routes:
                    match = pattern.match(path)
                    if route_method == method and match:
                        try:
                            status, content_type, payload = handler(match, body)
                        except Exception:
                            _logger.exception("route %s %s failed", method, path)
                            status, content_type, payload = 500, "text/plain", b"internal error"
                        return self._send(status, payload, content_type)
                self._send(404, b"not found", "text/plain")

            def _send(self, code: int, body: bytes, content_type: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")  # viewers read frames from their own pages
                self.end_headers()
                self.wfile.write(body)

            def _snapshot(self, source: MjpegSource) -> None:
                latest = source.latest()
                if latest is None:
                    return self._send(503, b"no frame yet", "text/plain")
                self._send(200, latest[0], "image/jpeg")

            def _stream(self, source: MjpegSource) -> None:
                self.send_response(200)
                self.send_header("Content-Type", f"multipart/x-mixed-replace;boundary={BOUNDARY.decode()}")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Connection", "close")
                self.end_headers()
                last = 0.0
                try:
                    while not reserver._stopping.is_set():
                        frame = source.wait_for_new_frame(after=last, timeout=reserver._stale_after)
                        if frame is None:
                            return  # stale camera: end the response so viewers notice
                        jpeg, last = frame
                        self.wfile.write(b"--" + BOUNDARY + b"\r\nContent-Type: image/jpeg\r\n"
                                         b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return

        return Handler
