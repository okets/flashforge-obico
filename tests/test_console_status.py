from flashforge_obico.console.status import build_console_status
from flashforge_obico.flashforge.snapshot import parse_snapshot


def test_offline_status_has_no_printer_block():
    st = build_console_status(snapshot=None, state_text="Offline", obico_connected=True, pending_command=None,
                              viewing=False, camera_count=1, version="0.2.0", now=1.0)
    assert st["connected"] is False and "printer" not in st and st["camera"] == "/cameras/0/stream"
    assert st["controls"] == ["pause", "resume", "light"]


def test_printing_status(detail):
    detail.update(status="printing", printFileName="a.gcode", printProgress=0.25, printDuration=100,
                  estimatedTime=300, printLayer=10, targetPrintLayer=40, lightStatus="open")
    detail["matlStationInfo"]["slotInfos"] = [{"slotId": 2, "hasFilament": True, "materialName": "ABS", "materialColor": "#8C8C89"}]
    st = build_console_status(snapshot=parse_snapshot(detail), state_text="Printing", obico_connected=True,
                              pending_command="pause", viewing=True, camera_count=1, version="0.2.0", now=5.0)
    p = st["printer"]
    assert p["state"] == "printing" and p["has_job"] and not p["warming_up"] and p["file"] == "a.gcode"
    assert p["progress"] == 0.25 and p["layer"] == 10 and p["total_layers"] == 40 and p["light_on"]
    assert p["temperatures"]["nozzles"][0] == {"tool": 0, "current": 28.0, "target": 0.0}
    assert p["slots"] == [{"slot_id": 2, "has_filament": True, "material": "ABS", "color": "#8C8C89"}]
    assert p["can_pause"] and not p["can_resume"]
    assert st["obico"] == {"connected": True, "viewing": True, "pending_command": "pause"}


def test_paused_status_offers_resume(detail):
    detail.update(status="pause", printFileName="a.gcode", printDuration=100, printLayer=3)
    p = build_console_status(snapshot=parse_snapshot(detail), state_text="Paused", obico_connected=False,
                             pending_command=None, viewing=False, camera_count=0, version="x", now=1.0)["printer"]
    assert p["paused"] and p["can_resume"] and not p["can_pause"]
