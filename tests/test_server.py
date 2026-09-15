import json
import queue
import threading
import time

import pytest
import websocket

from flashforge_obico.obico.server import ObicoError, ObicoServer, SharedTokenClosed, _RealWs, verify_link_code, ws_url


class FakeWs:
    def __init__(self, incoming):
        self.incoming = queue.Queue()
        for message in incoming:
            self.incoming.put(message)
        self.sent = []
        self.closed = threading.Event()

    def settimeout(self, timeout):
        pass

    def send(self, data):
        self.sent.append(json.loads(data))

    def recv(self):
        try:
            item = self.incoming.get(timeout=0.05)
        except queue.Empty:
            raise TimeoutError()
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed.set()


class FakeResponse:
    def __init__(self, ok=True, status_code=200, body=None):
        self.ok, self.status_code, self._body = ok, status_code, body or {}

    def json(self):
        return self._body


def wait_until(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while not predicate() and time.time() < deadline:
        time.sleep(0.01)
    return predicate()


def test_ws_url():
    assert ws_url("http://web:3334") == "ws://web:3334/ws/dev/"
    assert ws_url("https://obico.example/") == "wss://obico.example/ws/dev/"


def test_sends_queued_messages_with_bearer_header_and_dispatches_incoming():
    received, seen = [], []
    ws = FakeWs([json.dumps({"commands": [{"cmd": "pause"}]}), "not json", json.dumps([1, 2])])

    def connector(url, headers):
        seen.append((url, headers))
        return ws

    server = ObicoServer("http://web:3334", "TOKEN", on_message=received.append, connector=connector,
                         sleep=lambda s: None)
    server.start()
    try:
        server.send({"hello": 1})
        assert wait_until(lambda: received and ws.sent)
        assert seen[0] == ("ws://web:3334/ws/dev/", ["authorization: bearer TOKEN"])
        assert received == [{"commands": [{"cmd": "pause"}]}]
        assert ws.sent == [{"hello": 1}]
        assert server.connected.is_set()
    finally:
        server.stop()
        assert wait_until(ws.closed.is_set)


def test_reconnects_after_socket_error_with_backoff():
    sockets = [FakeWs([ConnectionResetError()]), FakeWs([])]
    sleeps = []
    server = ObicoServer("http://web:3334", "T", on_message=lambda m: None,
                         connector=lambda url, headers: sockets.pop(0), sleep=sleeps.append)
    server.start()
    try:
        assert wait_until(lambda: not sockets)
        assert sleeps and sleeps[0] == 1.0
    finally:
        server.stop()


def test_connect_failure_backs_off_exponentially():
    attempts, sleeps = [], []

    def connector(url, headers):
        attempts.append(1)
        raise OSError("refused")

    server = ObicoServer("http://web:3334", "T", on_message=lambda m: None, connector=connector, sleep=sleeps.append)
    server.start()
    try:
        assert wait_until(lambda: len(attempts) >= 4)
        assert sleeps[:3] == [1.0, 2.0, 4.0]
    finally:
        server.stop()


def test_shared_token_close_stops_reconnecting():
    ws = FakeWs([SharedTokenClosed()])
    attempts = []

    def connector(url, headers):
        attempts.append(1)
        return ws

    server = ObicoServer("http://web:3334", "T", on_message=lambda m: None, connector=connector, sleep=lambda s: None)
    server.start()
    try:
        assert server.shared_token_detected.wait(2.0)
        time.sleep(0.1)
        assert attempts == [1]
    finally:
        server.stop()


def test_queue_drops_oldest_when_full():
    server = ObicoServer("http://web:3334", "T", on_message=lambda m: None, connector=lambda u, h: FakeWs([]))
    for i in range(60):
        server.send({"n": i})
    assert server._queue.qsize() == 50 and server._queue.get_nowait() == {"n": 10}


def test_handler_exception_does_not_kill_the_reader():
    ws = FakeWs([json.dumps({"a": 1}), json.dumps({"b": 2})])
    received = []

    def on_message(message):
        received.append(message)
        raise RuntimeError("handler bug")

    server = ObicoServer("http://web:3334", "T", on_message=on_message, connector=lambda u, h: ws, sleep=lambda s: None)
    server.start()
    try:
        assert wait_until(lambda: len(received) == 2)
    finally:
        server.stop()


def test_post_pic_and_event_use_token_auth():
    posted = []

    def poster(method, url, *, headers, timeout, data=None, files=None):
        posted.append((method, url, headers, data, files))
        return FakeResponse()

    server = ObicoServer("http://web:3334/", "T", on_message=lambda m: None, poster=poster)
    assert server.post_pic(b"jpg", camera_name="Printer", is_primary=True, viewing_boost=False) is True
    method, url, headers, data, files = posted[0]
    assert (method, url) == ("POST", "http://web:3334/api/v1/octo/pic/")
    assert headers == {"Authorization": "Token T"}
    assert data == {"is_primary_camera": True, "is_nozzle_camera": False, "camera_name": "Printer",
                    "viewing_boost": False}
    assert files == {"pic": b"jpg"}
    assert server.post_printer_event(title="Pause failed", text="details", snapshot=b"jpg") is True
    method, url, headers, data, files = posted[1]
    assert url == "http://web:3334/api/v1/octo/printer_events/"
    assert data == {"event_title": "Pause failed", "event_text": "details", "event_type": "PRINTER_ERROR",
                    "event_class": "ERROR"}
    assert files == {"snapshot": b"jpg"}
    assert server._queue.get_nowait() == {"passthru": {"printer_event": data}}


def test_post_failure_returns_false():
    server = ObicoServer("http://web:3334", "T", on_message=lambda m: None,
                         poster=lambda *a, **k: FakeResponse(ok=False, status_code=500))
    assert server.post_pic(b"x", camera_name="c", is_primary=True, viewing_boost=False) is False

    def exploding(*a, **k):
        raise ConnectionError("down")

    server = ObicoServer("http://web:3334", "T", on_message=lambda m: None, poster=exploding)
    assert server.post_printer_event(title="t", text="x") is False


def test_verify_link_code():
    calls = []

    def poster(method, url, *, headers, timeout, data=None, files=None):
        calls.append((method, url, data))
        return FakeResponse(body={"printer": {"auth_token": "NEW"}})

    assert verify_link_code("http://web:3334", " 123456 ", poster=poster) == "NEW"
    assert calls[0] == ("POST", "http://web:3334/api/v1/octo/verify/?code=123456", None)
    with pytest.raises(ObicoError, match="rejected"):
        verify_link_code("http://web:3334", "0", poster=lambda *a, **k: FakeResponse(ok=False, status_code=400))
    with pytest.raises(ObicoError, match="auth token"):
        verify_link_code("http://web:3334", "1", poster=lambda *a, **k: FakeResponse(body={"printer": {}}))


class FakeRaw:
    def __init__(self, frames):
        self.frames = list(frames)

    def recv_data(self, control_frame=False):
        return self.frames.pop(0)


def test_real_ws_adapter_maps_frames():
    close_4321 = (4321).to_bytes(2, "big") + b"shared"
    raw = FakeRaw([(websocket.ABNF.OPCODE_PING, b""), (websocket.ABNF.OPCODE_TEXT, b'{"x":1}'),
                   (websocket.ABNF.OPCODE_BINARY, b"\x00"), (websocket.ABNF.OPCODE_CLOSE, (1000).to_bytes(2, "big")),
                   (websocket.ABNF.OPCODE_CLOSE, close_4321)])
    adapter = _RealWs(raw)
    assert adapter.recv() == '{"x":1}'
    assert adapter.recv() == b"\x00"
    with pytest.raises(websocket.WebSocketConnectionClosedException):
        adapter.recv()
    with pytest.raises(SharedTokenClosed):
        adapter.recv()
