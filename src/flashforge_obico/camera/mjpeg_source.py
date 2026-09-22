"""One camera stream, one connection.

`iter_mjpeg_frames` is the pure multipart parser. `MjpegSource` runs it on a thread, keeps the
newest JPEG and reconnects when the stream drops. The printer's MJPG-Streamer allows exactly one
client, so a source is the only thing that may ever open its URL.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import BinaryIO, Callable, Iterator

_logger = logging.getLogger(__name__)

Opener = Callable[[str], BinaryIO]
MAX_FRAME_BYTES = 8_000_000
BACKOFF_START_S, BACKOFF_MAX_S = 1.0, 30.0
OPEN_TIMEOUT_S = 10


def _default_opener(url: str) -> BinaryIO:
    return urllib.request.urlopen(url, timeout=OPEN_TIMEOUT_S)


def _underlying_socket(stream: object) -> socket.socket | None:
    """The socket a urllib response is layered on, found by unwrapping its buffers."""
    for _ in range(4):
        if isinstance(stream, socket.socket):
            return stream
        stream = getattr(stream, "fp", None) or getattr(stream, "raw", None) or getattr(stream, "_sock", None)
    return None


def _read_headers(stream: BinaryIO) -> dict[str, str] | None:
    """Reads part headers up to the blank line. None at end of stream."""
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if line == b"":
            return None
        if line in (b"\r\n", b"\n"):
            return headers
        key, sep, value = line.decode("latin-1").partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()


def _read_exact(stream: BinaryIO, length: int) -> bytes | None:
    data = stream.read(length)
    return data if len(data) == length else None


def _read_until_boundary(stream: BinaryIO, boundary: bytes) -> bytes | None:
    """Body of a part that declared no Content-Length. Consumes the terminating boundary line."""
    chunks: list[bytes] = []
    total = 0
    while True:
        line = stream.readline()
        if line == b"":
            return None
        if line.strip().startswith(boundary):
            body = b"".join(chunks)
            return body[:-2] if body.endswith(b"\r\n") else body
        chunks.append(line)
        total += len(line)
        if total > MAX_FRAME_BYTES:
            return None


def iter_mjpeg_frames(stream: BinaryIO) -> Iterator[bytes]:
    """Yields the raw JPEG of each part. Returns at end of stream or on a truncated part."""
    boundary: bytes | None = None
    at_boundary = False
    while True:
        if not at_boundary:
            line = stream.readline()
            if line == b"":
                return
            stripped = line.strip()
            if not stripped.startswith(b"--"):
                continue
            boundary = boundary or stripped
            if stripped == boundary + b"--":
                return
        at_boundary = False
        headers = _read_headers(stream)
        if headers is None:
            return
        length = headers.get("content-length", "")
        if length.isdigit():
            if int(length) > MAX_FRAME_BYTES:
                return
            frame = _read_exact(stream, int(length))
        else:
            frame = _read_until_boundary(stream, boundary)
            at_boundary = frame is not None
        if frame is None:
            return
        yield frame


@dataclass(frozen=True)
class CameraHealth:
    """What a viewer needs to explain a missing picture, rather than showing a broken image."""
    name: str
    streaming: bool
    """A connection to the camera is open right now."""
    frame_age_s: float | None
    """Seconds since the last frame, or None before the first one ever arrives."""
    last_error: str
    """The last connection failure; empty once frames are arriving again."""


class MjpegSource:
    def __init__(self, url: str, *, name: str = "", opener: Opener | None = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.url, self.name = url, name
        self._opener = opener or _default_opener
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Condition()
        self._latest: tuple[bytes, float] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stream_lock = threading.Lock()
        self._stream: BinaryIO | None = None
        self._last_error = ""

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"mjpeg:{self.name or self.url}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Closes the open connection too, rather than only asking the reader to finish.

        The printer frees its single viewer slot when the peer goes away, and a reader blocked on a
        stream that has gone quiet never gets there: the firmware then holds a dead client and
        refuses every new connection until its own timeout expires, which takes hours.
        """
        self._stop.set()
        self._close_stream()

    def _track_stream(self, stream: BinaryIO | None) -> None:
        with self._stream_lock:
            self._stream = stream

    def _close_stream(self) -> None:
        """Hangs up at once.

        Closing the response would not: the reader thread holds the buffer's lock until its own
        read returns, which is a socket timeout away, and a stop that slow is one the container
        runtime kills first -- leaving the printer holding the viewer slot all over again. A socket
        shutdown takes no lock, sends the FIN immediately and ends that read, after which the
        reader closes the response itself. A stream with no socket behind it can only be closed.
        """
        with self._stream_lock:
            stream, self._stream = self._stream, None
        if stream is None:
            return
        sock = _underlying_socket(stream)
        try:
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
            else:
                stream.close()
        except OSError:
            pass

    def latest(self) -> tuple[bytes, float] | None:
        """(jpeg, clock time it arrived), or None before the first frame."""
        with self._lock:
            return self._latest

    def health(self) -> CameraHealth:
        with self._stream_lock:
            streaming = self._stream is not None
        return CameraHealth(name=self._label, streaming=streaming,
                            frame_age_s=self.age(), last_error=self._last_error)

    def age(self) -> float | None:
        latest = self.latest()
        return None if latest is None else self._clock() - latest[1]

    def wait_for_new_frame(self, after: float, timeout: float) -> tuple[bytes, float] | None:
        with self._lock:
            self._lock.wait_for(lambda: self._latest is not None and self._latest[1] > after, timeout)
            if self._latest is not None and self._latest[1] > after:
                return self._latest
            return None

    def _store(self, jpeg: bytes) -> None:
        with self._lock:
            self._latest = (jpeg, self._clock())
            self._last_error = ""
            self._lock.notify_all()

    def _run(self) -> None:
        backoff = BACKOFF_START_S
        while not self._stop.is_set():
            try:
                backoff = self._consume_stream(backoff)
                _logger.info("camera %s: stream ended, reconnecting", self._label)
            except Exception as exc:
                if self._stop.is_set():
                    return  # the stream was closed by our own stop()
                self._last_error = f"{type(exc).__name__}: {exc}"
                _logger.warning("camera %s: %s", self._label, self._last_error)
            if self._stop.is_set():
                return
            self._sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_S)

    def _consume_stream(self, backoff: float) -> float:
        """Reads frames until the stream ends. Returns the backoff to use next (reset once a frame arrived)."""
        stream = self._opener(self.url)
        self._track_stream(stream)
        try:
            with stream:
                for frame in iter_mjpeg_frames(stream):
                    self._store(frame)
                    backoff = BACKOFF_START_S
                    if self._stop.is_set():
                        break
        finally:
            self._track_stream(None)
        return backoff

    @property
    def _label(self) -> str:
        return self.name or self.url
