import io
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
