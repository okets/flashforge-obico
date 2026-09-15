import io
import urllib.error
import urllib.request

import pytest

from flashforge_obico.camera.mjpeg_source import MjpegSource
from flashforge_obico.camera.reserve import CameraReserver

JPEG = b"\xff\xd8hello\xff\xd9"


def make_source():
    return MjpegSource("http://unused", name="Printer", opener=lambda url: io.BytesIO(b""))  # never started


@pytest.fixture
def served():
    source = make_source()
    server = CameraReserver([source], port=0, stale_after_s=0.3, host="127.0.0.1")
    server.start()
    try:
        yield source, f"http://127.0.0.1:{server.port}"
    finally:
        server.stop()


def status_of(url):
    try:
        return urllib.request.urlopen(url, timeout=2).status
    except urllib.error.HTTPError as error:
        return error.code


def test_snapshot_and_health(served):
    source, base = served
    assert urllib.request.urlopen(base + "/healthz", timeout=2).read() == b"ok"
    assert status_of(base + "/cameras/0/snapshot") == 503
    source._store(JPEG)
    response = urllib.request.urlopen(base + "/cameras/0/snapshot", timeout=2)
    assert response.headers["Content-Type"] == "image/jpeg" and response.read() == JPEG
    assert status_of(base + "/cameras/7/snapshot") == 404
    assert status_of(base + "/nothing") == 404


def read_part(client):
    assert client.readline().strip() == b"--flashforgeobico"
    headers = {}
    while (line := client.readline()) not in (b"\r\n", b""):
        key, _, value = line.decode().partition(":")
        headers[key.lower()] = value.strip()
    return headers, client.read(int(headers["content-length"]))


def test_stream_serves_two_clients_and_closes_when_stale(served):
    source, base = served
    a = urllib.request.urlopen(base + "/cameras/0/stream", timeout=2)
    b = urllib.request.urlopen(base + "/cameras/0/stream", timeout=2)
    assert a.headers["Content-Type"].startswith("multipart/x-mixed-replace")
    source._store(JPEG)
    for client in (a, b):
        headers, body = read_part(client)
        assert headers["content-type"] == "image/jpeg" and body == JPEG
    assert a.read() in (b"", b"\r\n")  # no new frames: the server ends the response after stale_after_s
