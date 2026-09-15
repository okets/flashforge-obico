"""One camera stream, one connection.

`iter_mjpeg_frames` is the pure multipart parser. `MjpegSource` runs it on a thread, keeps the
newest JPEG and reconnects when the stream drops. The printer's MJPG-Streamer allows exactly one
client, so a source is the only thing that may ever open its URL.
"""
from __future__ import annotations

import logging
import threading
import time
import urllib.request
from typing import BinaryIO, Callable, Iterator

_logger = logging.getLogger(__name__)

Opener = Callable[[str], BinaryIO]
MAX_FRAME_BYTES = 8_000_000
BACKOFF_START_S, BACKOFF_MAX_S = 1.0, 30.0
OPEN_TIMEOUT_S = 10


def _default_opener(url: str) -> BinaryIO:
    return urllib.request.urlopen(url, timeout=OPEN_TIMEOUT_S)


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

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"mjpeg:{self.name or self.url}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def latest(self) -> tuple[bytes, float] | None:
        """(jpeg, clock time it arrived), or None before the first frame."""
        with self._lock:
            return self._latest

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
            self._lock.notify_all()

    def _run(self) -> None:
        backoff = BACKOFF_START_S
        while not self._stop.is_set():
            try:
                backoff = self._consume_stream(backoff)
                _logger.info("camera %s: stream ended, reconnecting", self._label)
            except Exception as exc:
                _logger.warning("camera %s: %s: %s", self._label, type(exc).__name__, exc)
            if self._stop.is_set():
                return
            self._sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_S)

    def _consume_stream(self, backoff: float) -> float:
        """Reads frames until the stream ends. Returns the backoff to use next (reset once a frame arrived)."""
        with self._opener(self.url) as stream:
            for frame in iter_mjpeg_frames(stream):
                self._store(frame)
                backoff = BACKOFF_START_S
                if self._stop.is_set():
                    break
        return backoff

    @property
    def _label(self) -> str:
        return self.name or self.url
