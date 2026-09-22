from flashforge_obico.camera.mjpeg_source import CameraHealth
from flashforge_obico.console.status import build_console_status
from flashforge_obico.flashforge.snapshot import parse_snapshot

HEALTHY = CameraHealth(name="Printer", streaming=True, frame_age_s=0.4, last_error="")
REFUSED = CameraHealth(name="Printer", streaming=False, frame_age_s=None,
                       last_error="ConnectionResetError: [Errno 104] Connection reset by peer")


def test_offline_status_has_no_printer_block():
    st = build_console_status(snapshot=None, state_text="Offline", obico_connected=True, pending_command=None,
                              viewing=False, cameras=[HEALTHY], version="0.2.0", now=1.0)
    assert st["connected"] is False and "printer" not in st and st["camera"] == "/cameras/0/stream"
    assert st["controls"] == ["pause", "resume", "light"]


def test_printing_status(detail):
    detail.update(status="printing", printFileName="a.gcode", printProgress=0.25, printDuration=100,
                  estimatedTime=300, printLayer=10, targetPrintLayer=40, lightStatus="open")
    detail["matlStationInfo"]["slotInfos"] = [{"slotId": 2, "hasFilament": True, "materialName": "ABS", "materialColor": "#8C8C89"}]
    st = build_console_status(snapshot=parse_snapshot(detail), state_text="Printing", obico_connected=True,
                              pending_command="pause", viewing=True, cameras=[HEALTHY], version="0.2.0", now=5.0)
    p = st["printer"]
    assert p["state"] == "printing" and p["has_job"] and not p["warming_up"] and p["file"] == "a.gcode"
    assert p["progress"] == 0.25 and p["layer"] == 10 and p["total_layers"] == 40 and p["light_on"]
    assert p["remaining_est_s"] == 300 and p["firmware_estimated_s"] == 300   # 100 s at 25 % -> 300 s left
    assert p["temperatures"]["nozzles"][0] == {"tool": 0, "current": 28.0, "target": 0.0}
    assert p["slots"] == [{"slot_id": 2, "has_filament": True, "material": "ABS", "color": "#8C8C89"}]
    assert p["can_pause"] and not p["can_resume"]
    assert st["obico"] == {"connected": True, "viewing": True, "pending_command": "pause"}


def test_paused_status_offers_resume(detail):
    detail.update(status="pause", printFileName="a.gcode", printDuration=100, printLayer=3)
    p = build_console_status(snapshot=parse_snapshot(detail), state_text="Paused", obico_connected=False,
                             pending_command=None, viewing=False, cameras=[], version="x", now=1.0)["printer"]
    assert p["paused"] and p["can_resume"] and not p["can_pause"]


def test_camera_health_says_why_there_is_no_picture():
    """A blank camera used to look the same as a working one; the page needs the reason."""
    st = build_console_status(snapshot=None, state_text="Offline", obico_connected=True, pending_command=None,
                              viewing=False, cameras=[REFUSED], version="0.2.0", now=1.0)
    assert st["cameras"] == [{"name": "Printer", "streaming": False, "frame_age_s": None,
                              "last_error": "ConnectionResetError: [Errno 104] Connection reset by peer"}]


def test_a_healthy_camera_reports_its_frame_age():
    st = build_console_status(snapshot=None, state_text="Offline", obico_connected=True, pending_command=None,
                              viewing=False, cameras=[HEALTHY], version="0.2.0", now=1.0)
    assert st["cameras"] == [{"name": "Printer", "streaming": True, "frame_age_s": 0.4, "last_error": ""}]


def test_no_camera_means_no_stream_url():
    st = build_console_status(snapshot=None, state_text="Offline", obico_connected=True, pending_command=None,
                              viewing=False, cameras=[], version="0.2.0", now=1.0)
    assert st["camera"] is None and st["cameras"] == []
