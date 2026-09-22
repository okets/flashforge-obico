import io
import socket
import threading
import time

from flashforge_obico.camera.mjpeg_source import MjpegSource, iter_mjpeg_frames

JPEG_A = b"\xff\xd8AAAA\xff\xd9"
JPEG_B = b"\xff\xd8BB\xff\xd9"


def mjpg_streamer_body(*frames, with_length=True):
    out = b""
    for frame in frames:
        out += b"--boundarydonotcross\r\nContent-Type: image/jpeg\r\n"
        if with_length:
            out += b"Content-Length: %d\r\n" % len(frame)
        out += b"X-Timestamp: 1.000\r\n\r\n" + frame + b"\r\n"
    return out


def test_parses_frames_with_content_length():
    assert list(iter_mjpeg_frames(io.BytesIO(mjpg_streamer_body(JPEG_A, JPEG_B)))) == [JPEG_A, JPEG_B]


def test_parses_frames_without_content_length_by_boundary():
    body = mjpg_streamer_body(JPEG_A, JPEG_B, with_length=False) + b"--boundarydonotcross--\r\n"
    assert list(iter_mjpeg_frames(io.BytesIO(body))) == [JPEG_A, JPEG_B]


def test_frame_containing_newlines_survives_boundary_scan():
    tricky = b"\xff\xd8\r\nline\r\n--not-the-boundary\r\n\xff\xd9"
    body = mjpg_streamer_body(tricky, JPEG_A, with_length=False) + b"--boundarydonotcross--\r\n"
    assert list(iter_mjpeg_frames(io.BytesIO(body))) == [tricky, JPEG_A]


def test_truncated_final_frame_is_dropped():
    body = mjpg_streamer_body(JPEG_A) + b"--boundarydonotcross\r\nContent-Length: 99\r\n\r\n\xff\xd8partial"
    assert list(iter_mjpeg_frames(io.BytesIO(body))) == [JPEG_A]


def test_source_keeps_latest_and_reconnects():
    bodies = [mjpg_streamer_body(JPEG_A), mjpg_streamer_body(JPEG_B)]
    exhausted = threading.Event()

    def opener(url):
        if not bodies:
            exhausted.set()
            time.sleep(0.05)
            raise OSError("down")
        return io.BytesIO(bodies.pop(0))

    src = MjpegSource("http://cam/?action=stream", name="Printer", opener=opener,
                      sleep=lambda s: time.sleep(min(s, 0.01)))
    src.start()
    try:
        assert exhausted.wait(2.0)
        jpeg, _ = src.latest()
        assert jpeg == JPEG_B and src.age() < 2.0
    finally:
        src.stop()


def test_wait_for_new_frame_times_out_and_returns_new():
    src = MjpegSource("http://cam", opener=lambda url: io.BytesIO(b""))
    assert src.age() is None
    assert src.wait_for_new_frame(after=0.0, timeout=0.05) is None
    src._store(JPEG_A)
    assert src.wait_for_new_frame(after=0.0, timeout=0.05)[0] == JPEG_A
    assert src.wait_for_new_frame(after=src.latest()[1], timeout=0.05) is None


class StalledStream:
    """A connection that is open but sending nothing, like the camera slot we are queued behind.
    `readline` only returns once the stream is closed."""

    def __init__(self):
        self.closed = threading.Event()

    def readline(self):
        self.closed.wait(5.0)
        return b""

    def read(self, length=-1):
        return b""

    def close(self):
        self.closed.set()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def test_stop_closes_the_open_connection():
    """The printer allows one viewer, so the slot has to be free the moment we let go of it;
    waiting for the reader to notice leaves the firmware holding a dead peer."""
    stream, opened = StalledStream(), threading.Event()

    def opener(url):
        opened.set()
        return stream

    src = MjpegSource("http://cam/?action=stream", name="Printer", opener=opener)
    src.start()
    assert opened.wait(2.0)
    src.stop()
    assert stream.closed.wait(0.5), "stop() left the camera connection open"


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not predicate():
        time.sleep(0.01)
    return predicate()


def test_health_reports_why_the_printer_will_not_connect():
    def opener(url):
        raise ConnectionResetError(104, "Connection reset by peer")

    src = MjpegSource("http://cam", name="Printer", opener=opener, sleep=lambda s: time.sleep(0.01))
    src.start()
    try:
        assert wait_until(lambda: src.health().last_error)
        health = src.health()
    finally:
        src.stop()
    assert health.name == "Printer" and not health.streaming and health.frame_age_s is None
    assert "Connection reset by peer" in health.last_error


def test_health_shows_a_connection_that_is_open_but_silent():
    stream = StalledStream()
    src = MjpegSource("http://cam", name="Printer", opener=lambda url: stream)
    src.start()
    try:
        assert wait_until(lambda: src.health().streaming)
        assert src.health().frame_age_s is None
    finally:
        src.stop()


def test_health_clears_the_error_once_a_frame_arrives():
    attempts, blocked = [], threading.Event()

    def opener(url):
        attempts.append(url)
        if len(attempts) == 1:
            raise ConnectionResetError(104, "Connection reset by peer")
        if len(attempts) == 2:
            return io.BytesIO(mjpg_streamer_body(JPEG_A))
        blocked.wait(5.0)   # no further failure, so nothing races the assertion below
        raise OSError("down")

    src = MjpegSource("http://cam", name="Printer", opener=opener, sleep=lambda s: time.sleep(0.01))
    src.start()
    try:
        assert wait_until(lambda: src.latest() is not None)
        health = src.health()
        assert health.last_error == "" and health.frame_age_s < 1.0
    finally:
        blocked.set()
        src.stop()


def test_stop_hangs_up_on_a_real_socket():
    """The outage this guards against was a printer left holding a client that never said goodbye,
    so this asserts on a real connection rather than on a stub's close()."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    src = MjpegSource(f"http://127.0.0.1:{listener.getsockname()[1]}/?action=stream", name="Printer")
    src.start()
    conn, _ = listener.accept()
    try:
        conn.recv(4096)                       # the GET
        conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: multipart/x-mixed-replace;boundary=b\r\n\r\n")
        assert wait_until(lambda: src.health().streaming)   # connected, and sending nothing
        started = time.monotonic()
        src.stop()
        # Promptness is the point: a stop that waits for the blocked reader to time out is a stop
        # that docker kills first, which leaves the printer holding the viewer slot all over again.
        assert time.monotonic() - started < 2.0, "stop() waited for the reader instead of hanging up"
        conn.settimeout(2.0)
        assert conn.recv(4096) == b"", "the printer would still be holding a live viewer"
    finally:
        conn.close()
        listener.close()
        src.stop()
