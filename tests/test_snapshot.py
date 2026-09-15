from flashforge_obico.flashforge.snapshot import MachineState, parse_snapshot


def test_idle_machine(detail):
    s = parse_snapshot(detail)
    assert s.state is MachineState.READY
    assert s.file_name == "" and not s.has_job
    assert s.nozzle_temps == (28.0, 29.0, 29.0, 29.0)
    assert s.nozzle_targets == (0.0,) * 4
    assert s.bed_temp == 27.0 and s.chamber_target == 0.0
    assert s.camera_stream_url == "http://10.0.0.10:8080/?action=stream"
    assert s.model == "Creator 5 Pro" and s.firmware == "1.9.9"


def test_printing_machine(detail):
    detail.update(status="Printing", printFileName="benchy.gcode", printProgress=0.42,
                  printDuration=600, estimatedTime=830.0, printLayer=57, targetPrintLayer=137)
    s = parse_snapshot(detail)
    assert s.state is MachineState.PRINTING and s.has_job
    assert s.progress == 0.42 and s.duration_s == 600 and s.remaining_s == 830
    assert s.layer == 57 and s.total_layers == 137


def test_cancel_maps_to_cancelled_and_unknown_is_unknown(detail):
    detail["status"] = "CANCEL"
    assert parse_snapshot(detail).state is MachineState.CANCELLED
    detail["status"] = "warming-up-3000"
    assert parse_snapshot(detail).state is MachineState.UNKNOWN


def test_mistyped_fields_cost_only_themselves(detail):
    detail.update(printLayer=" 12 ", targetPrintLayer=True, printProgress="0.5",
                  nozzleTemps=["x", 30, None, 31])
    s = parse_snapshot(detail)
    assert s.layer == 12 and s.total_layers == 1 and s.progress == 0.5
    assert s.nozzle_temps == (30.0, 31.0)


def test_missing_fields_default(detail):
    for key in ("printLayer", "nozzleTemps", "platTemp", "cameraStreamUrl", "errorCode"):
        detail.pop(key)
    s = parse_snapshot(detail)
    assert s.layer is None and s.nozzle_temps == () and s.bed_temp is None
    assert s.camera_stream_url == "" and s.error_code == ""


def test_progress_is_clamped(detail):
    detail["printProgress"] = 1.7
    assert parse_snapshot(detail).progress == 1.0
