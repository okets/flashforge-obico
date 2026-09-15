"""The Obico side of the agent.

A websocket carries status out and commands in; REST carries pictures and printer events. This
mirrors moonraker-obico's ServerConn and, like it, is synchronous and thread-based.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Any, Callable, Protocol

import requests
import websocket

_logger = logging.getLogger(__name__)

QUEUE_MAX = 50
BACKOFF_START_S, BACKOFF_MAX_S = 1.0, 300.0
SHARED_TOKEN_CLOSE_CODE = 4321
SOCKET_TIMEOUT_S = 1.0
CONNECT_TIMEOUT_S = 10
REST_TIMEOUT_S = 60


class ObicoError(Exception):
    pass


class SharedTokenClosed(Exception):
    """Obico closed the socket with code 4321: another agent is using this printer's token."""


class WsLike(Protocol):
    def send(self, data: str) -> None: ...
    def recv(self) -> str | bytes: ...
    def close(self) -> None: ...
    def settimeout(self, timeout: float) -> None: ...


Connector = Callable[[str, list[str]], WsLike]
"""(websocket url, request headers) -> a connected socket."""


class Poster(Protocol):
    def __call__(self, method: str, url: str, *, headers: dict, timeout: float, data=None, files=None) -> Any: ...


class _RealWs:
    """Adapts websocket-client so a close frame surfaces its code: 4321 becomes SharedTokenClosed."""

    def __init__(self, raw):
        self._raw = raw

    def send(self, data: str) -> None:
        self._raw.send(data)

    def close(self) -> None:
        self._raw.close()

    def settimeout(self, timeout: float) -> None:
        self._raw.settimeout(timeout)

    def recv(self) -> str | bytes:
        while True:
            opcode, data = self._raw.recv_data(control_frame=True)
            if opcode == websocket.ABNF.OPCODE_TEXT:
                return data.decode("utf-8", errors="replace")
            if opcode == websocket.ABNF.OPCODE_BINARY:
                return data
            if opcode == websocket.ABNF.OPCODE_CLOSE:
                code = int.from_bytes(data[:2], "big") if len(data) >= 2 else 0
                if code == SHARED_TOKEN_CLOSE_CODE:
                    raise SharedTokenClosed()
                raise websocket.WebSocketConnectionClosedException(f"closed with code {code}")
            # ping/pong: websocket-client has already answered a ping


def _default_connector(url: str, headers: list[str]) -> WsLike:
    return _RealWs(websocket.create_connection(url, header=headers, timeout=CONNECT_TIMEOUT_S))


def _default_poster(method, url, *, headers, timeout, data=None, files=None):
    return requests.request(method, url, headers=headers, timeout=timeout, data=data, files=files)


def ws_url(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    scheme, _, rest = base.partition("://")
    return f"{'wss' if scheme == 'https' else 'ws'}://{rest}/ws/dev/"


def verify_link_code(base_url: str, code: str, *, poster: Poster | None = None) -> str:
    """Exchanges the 6-digit code shown by Obico's "link printer" flow for the printer's auth token."""
    post = poster or _default_poster
    url = f"{base_url.strip().rstrip('/')}/api/v1/octo/verify/?code={code.strip()}"
    response = post("POST", url, headers={}, timeout=15)
    if not response.ok:
        raise ObicoError(f"Obico rejected the code (HTTP {response.status_code})")
    try:
        return response.json()["printer"]["auth_token"]
    except (KeyError, TypeError, ValueError):
        raise ObicoError("Obico's reply did not contain an auth token") from None


class ObicoServer:
    def __init__(self, base_url: str, auth_token: str, *, on_message: Callable[[dict], None],
                 connector: Connector | None = None, poster: Poster | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self._base = base_url.strip().rstrip("/")
        self._token = auth_token
        self._on_message = on_message
        self._connect = connector or _default_connector
        self._post = poster or _default_poster
        self._sleep = sleep
        self._queue: queue.Queue[dict] = queue.Queue(maxsize=QUEUE_MAX)
        self._stop = threading.Event()
        self.connected = threading.Event()
        self.shared_token_detected = threading.Event()
        self._thread: threading.Thread | None = None

    # ── websocket ──────────────────────────────────────────────────────────────────────────
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="obico-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def send(self, message: dict) -> None:
        """Queues a message; when the queue is full the oldest is dropped, never the newest."""
        while True:
            try:
                self._queue.put_nowait(message)
                return
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    _logger.warning("Obico message queue full; dropped the oldest message")
                except queue.Empty:
                    pass

    def _run(self) -> None:
        backoff = BACKOFF_START_S
        while self._should_run():
            connected = self._session()
            if connected:
                backoff = BACKOFF_START_S
            if self._should_run():
                self._sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_S)

    def _should_run(self) -> bool:
        return not self._stop.is_set() and not self.shared_token_detected.is_set()

    def _session(self) -> bool:
        """One connection, from connect to failure. Returns whether it connected at all."""
        ws = None
        try:
            ws = self._connect(ws_url(self._base), [f"authorization: bearer {self._token}"])
            ws.settimeout(SOCKET_TIMEOUT_S)
            self.connected.set()
            _logger.info("connected to Obico at %s", self._base)
            self._pump(ws)
            return True
        except SharedTokenClosed:
            _logger.error("Obico closed the connection: this auth token is in use by another agent. Stopping.")
            self.shared_token_detected.set()
            return True
        except Exception as exc:
            _logger.warning("Obico connection: %s: %s", type(exc).__name__, exc)
            return ws is not None
        finally:
            self.connected.clear()
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass

    def _pump(self, ws: WsLike) -> None:
        """Sends queued messages while a reader thread receives, until either side fails."""
        failure: list[BaseException] = []
        reader = threading.Thread(target=self._reader, args=(ws, failure), name="obico-ws-reader", daemon=True)
        reader.start()
        while not self._stop.is_set():
            if failure:
                raise failure[0]
            try:
                message = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            ws.send(json.dumps(message, default=str))

    def _reader(self, ws: WsLike, failure: list[BaseException]) -> None:
        while not self._stop.is_set():
            try:
                raw = ws.recv()
            except (TimeoutError, websocket.WebSocketTimeoutException):
                continue
            except Exception as exc:
                failure.append(exc)
                return
            self._dispatch(raw)

    def _dispatch(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            return  # a BSON frame; this agent never asks for one
        try:
            message = json.loads(raw)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        try:
            self._on_message(message)
        except Exception:
            _logger.exception("error while handling a message from Obico")

    # ── REST ───────────────────────────────────────────────────────────────────────────────
    def _rest(self, path: str, *, data: dict, files: dict | None) -> bool:
        try:
            response = self._post("POST", f"{self._base}{path}", headers={"Authorization": f"Token {self._token}"},
                                  timeout=REST_TIMEOUT_S, data=data, files=files)
        except Exception as exc:
            _logger.warning("POST %s failed: %s: %s", path, type(exc).__name__, exc)
            return False
        if not response.ok:
            _logger.warning("POST %s -> HTTP %s", path, response.status_code)
        return bool(response.ok)

    def post_pic(self, jpeg: bytes, *, camera_name: str, is_primary: bool, viewing_boost: bool) -> bool:
        return self._rest("/api/v1/octo/pic/", files={"pic": jpeg},
                          data={"is_primary_camera": is_primary, "is_nozzle_camera": False,
                                "camera_name": camera_name, "viewing_boost": viewing_boost})

    def post_printer_event(self, *, title: str, text: str, event_type: str = "PRINTER_ERROR",
                           event_class: str = "ERROR", snapshot: bytes | None = None) -> bool:
        data = {"event_title": title, "event_text": text, "event_type": event_type, "event_class": event_class}
        self.send({"passthru": {"printer_event": data}})
        return self._rest("/api/v1/octo/printer_events/", data=data, files={"snapshot": snapshot} if snapshot else None)
