from flashforge_obico import VERSION
from flashforge_obico.flashforge.snapshot import parse_snapshot
from flashforge_obico.job_tracker import Event, JobTracker
from flashforge_obico.obico.status import (OFFLINE, OPERATIONAL, PAUSED, PRINTING, build_message,
                                          build_settings, build_status, obico_state_text, webcam_entry)


def snap(detail, **over):
    merged = dict(detail)
    merged.update(over)
    return parse_snapshot(merged)


def test_state_text_mapping(detail):
    assert obico_state_text(snap(detail), PRINTING) == OPERATIONAL
    assert obico_state_text(snap(detail, status="heating", printFileName="a"), OPERATIONAL) == PRINTING
    assert obico_state_text(snap(detail, status="busy", printFileName="a"), OPERATIONAL) == PRINTING
    assert obico_state_text(snap(detail, status="busy"), PRINTING) == PRINTING  # busy, no file: keep
    assert obico_state_text(snap(detail, status="paused", printFileName="a"), PRINTING) == PAUSED
    assert obico_state_text(snap(detail, status="mystery"), PAUSED) == PAUSED  # unknown: keep
    assert obico_state_text(snap(detail, status="error"), PRINTING) == OPERATIONAL
    assert obico_state_text(None, PRINTING) == OFFLINE


def test_offline_status_is_empty(detail):
    assert build_status(None, JobTracker(), OFFLINE, 1.0) == {}
    assert build_status(snap(detail), JobTracker(), OFFLINE, 1.0) == {}


def test_printing_status_shape(detail):
    tracker = JobTracker()
    s = snap(detail, status="printing", printFileName="benchy.gcode", printProgress=0.42,
             printDuration=600, estimatedTime=830, printLayer=57, targetPrintLayer=137,
             nozzleTemps=[210, 30, 30, 30], nozzleTargetTemps=[210, 0, 0, 0], platTemp=60, platTargetTemp=60)
    tracker.observe(s, 1000.0)
    st = build_status(s, tracker, PRINTING, 1001.0)
    assert st["_ts"] == 1001.0
    assert st["state"]["text"] == "Printing"
    assert st["state"]["flags"] == {"operational": True, "paused": False, "printing": True, "cancelling": False,
                                    "pausing": False, "error": False, "ready": False, "closedOrError": False}
    assert st["state"]["error"] is None
    assert st["job"]["file"] == {"name": "benchy.gcode", "path": "benchy.gcode", "display": "benchy.gcode",
                                 "obico_g_code_file_id": None}
    assert st["progress"] == {"completion": 42.0, "filepos": 0, "printTime": 600, "printTimeLeft": 830,
                              "filamentUsed": None}
    assert st["temperatures"]["tool0"] == {"actual": 210.0, "offset": 0, "target": 210.0}
    assert st["temperatures"]["tool3"] == {"actual": 30.0, "offset": 0, "target": 0.0}
    assert st["temperatures"]["bed"] == {"actual": 60.0, "offset": 0, "target": 60.0}
    assert st["temperatures"]["chamber"] == {"actual": 27.0, "offset": 0, "target": 0.0}
    assert st["currentLayerHeight"] == 57
    assert st["file_metadata"]["obico"]["totalLayerCount"] == 137
    assert st["currentZ"] is None and st["currentFeedRate"] is None and st["currentFanSpeed"] is None


def test_idle_status_has_no_progress(detail):
    st = build_status(snap(detail), JobTracker(), OPERATIONAL, 1.0)
    assert st["progress"]["completion"] is None and st["currentLayerHeight"] is None
    assert st["job"]["file"]["name"] is None


def test_error_state(detail):
    s = snap(detail, status="error", errorCode="E42")
    st = build_status(s, JobTracker(), OPERATIONAL, 1.0)
    assert st["state"]["flags"]["error"] is True and st["state"]["error"] == "E42"
    assert st["state"]["flags"]["ready"] is True


def test_message_and_settings():
    cam = webcam_entry(name="Printer", is_primary=True, stream_url="http://h:8081/cameras/0/stream",
                       snapshot_url="http://h:8081/cameras/0/snapshot")
    assert cam["is_primary_camera"] is True and cam["stream_mode"] == "h264_transcode" and cam["stream_id"] is None
    assert cam["streamRatio"] == "16:9" and cam["rotation"] == 0 and cam["flipH"] is False
    assert cam["stream_url"] == "http://h:8081/cameras/0/stream"
    settings = build_settings([cam])
    assert settings["agent"] == {"name": "flashforge_obico", "version": VERSION}
    assert settings["webcams"] == [cam] and settings["temperature"] == {"profiles": []}
    msg = build_message(status={"x": 1}, current_print_ts=5, event=Event.STARTED, settings=settings)
    assert msg == {"current_print_ts": 5, "status": {"x": 1}, "event": {"event_type": "PrintStarted"},
                   "settings": settings}
    assert build_message(status={}, current_print_ts=-1) == {"current_print_ts": -1, "status": {}}
