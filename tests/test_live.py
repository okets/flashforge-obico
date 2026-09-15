"""Opt-in test against the real printer. Skips unless FF_HOST, FF_SERIAL and FF_CHECK_CODE are set.
Reads status and one camera frame; never sends a job command."""
import os
import urllib.request

import pytest

from flashforge_obico.camera.mjpeg_source import MjpegSource
from flashforge_obico.camera.reserve import CameraReserver
from flashforge_obico.flashforge.client import FlashforgeClient
from flashforge_obico.flashforge.snapshot import parse_snapshot

pytestmark = pytest.mark.live
NEEDED = ("FF_HOST", "FF_SERIAL", "FF_CHECK_CODE")


@pytest.fixture
def client():
    if not all(os.environ.get(key) for key in NEEDED):
        pytest.skip("set FF_HOST, FF_SERIAL and FF_CHECK_CODE to run the live test")
    return FlashforgeClient(os.environ["FF_HOST"], os.environ["FF_SERIAL"], os.environ["FF_CHECK_CODE"])


def test_detail_parses(client):
    snapshot = parse_snapshot(client.detail())
    assert snapshot.model and snapshot.firmware
    assert snapshot.camera_stream_url.startswith("http")
    assert len(snapshot.nozzle_temps) == 4


def test_camera_frame_and_reserver_serve_two_clients(client):
    url = parse_snapshot(client.detail()).camera_stream_url
    source = MjpegSource(url, name="Printer")
    source.start()
    server = CameraReserver([source], port=0, stale_after_s=5, host="127.0.0.1")
    server.start()
    try:
        assert source.wait_for_new_frame(after=0.0, timeout=10.0) is not None
        base = f"http://127.0.0.1:{server.port}"
        a = urllib.request.urlopen(base + "/cameras/0/stream", timeout=5)
        b = urllib.request.urlopen(base + "/cameras/0/stream", timeout=5)
        assert a.readline().startswith(b"--") and b.readline().startswith(b"--")
        jpeg = urllib.request.urlopen(base + "/cameras/0/snapshot", timeout=5).read()
        assert jpeg[:2] == b"\xff\xd8"
    finally:
        server.stop()
        source.stop()
