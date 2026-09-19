"""The stored-file list, its thumbnails, and starting one of them.

The firmware returns two parallel things from `gcodeList`: a `gcodeList` of bare names and a
`gcodeListDetail` of objects carrying the print time, the filament weight and a per-tool material.
Neither is documented and either can be missing, so the parsing takes what it can read and drops
the rest rather than failing the call -- the same rule the snapshot parsing follows.
"""

import base64

import pytest

from flashforge_obico.flashforge.client import FlashforgeClient, FlashforgeError
from flashforge_obico.flashforge.parsing import parse_gcode_files

PNG = b"\x89PNG\r\n\x1a\n" + b"fake"


class FakeTransport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, url, body):
        self.calls.append((url, body))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def make(replies):
    transport = FakeTransport(replies)
    return FlashforgeClient("10.0.0.10", "SN123", "CODE", transport=transport), transport


def detail_entry(name, **over):
    entry = {
        "gcodeFileName": name,
        "printingTime": 3209,
        "totalFilamentWeight": 15.8,
        "gcodeToolCnt": 2,
        "useMatlStation": True,
        "gcodeToolDatas": [
            {"toolId": 0, "slotId": 1, "materialName": "PLA", "materialColor": "#FFFF0A", "filamentWeight": 3.95},
            {"toolId": 1, "slotId": 2, "materialName": "PLA", "materialColor": "#0080FF", "filamentWeight": 11.85},
        ],
    }
    entry.update(over)
    return entry


# ── parsing ────────────────────────────────────────────────────────────────────────────────


def test_detail_entries_carry_their_materials_and_totals():
    files = parse_gcode_files({
        "gcodeList": ["benchy.gcode.3mf"],
        "gcodeListDetail": [detail_entry("benchy.gcode.3mf")],
    })
    assert len(files) == 1
    assert files[0]["name"] == "benchy.gcode.3mf"
    assert files[0]["printing_time_s"] == 3209
    assert files[0]["filament_weight_g"] == pytest.approx(15.8)
    assert [tool["material"] for tool in files[0]["tools"]] == ["PLA", "PLA"]
    assert [tool["color"] for tool in files[0]["tools"]] == ["#FFFF0A", "#0080FF"]
    assert [tool["slot_id"] for tool in files[0]["tools"]] == [1, 2]


def test_a_name_with_no_detail_entry_still_appears():
    files = parse_gcode_files({"gcodeList": ["old.gcode", "benchy.gcode.3mf"],
                               "gcodeListDetail": [detail_entry("benchy.gcode.3mf")]})
    assert [f["name"] for f in files] == ["old.gcode", "benchy.gcode.3mf"]
    assert files[0]["printing_time_s"] is None
    assert files[0]["tools"] == []


def test_detail_only_firmware_still_yields_the_files():
    files = parse_gcode_files({"gcodeListDetail": [detail_entry("benchy.gcode.3mf")]})
    assert [f["name"] for f in files] == ["benchy.gcode.3mf"]


def test_unreadable_entries_are_dropped_not_fatal():
    files = parse_gcode_files({
        "gcodeList": ["good.gcode.3mf", "", None, 7],
        "gcodeListDetail": ["not a dict", {"noName": 1}, detail_entry("good.gcode.3mf", printingTime="oops")],
    })
    assert [f["name"] for f in files] == ["good.gcode.3mf"]
    assert files[0]["printing_time_s"] is None  # unreadable number, not a crash


def test_no_list_at_all_is_an_empty_list():
    assert parse_gcode_files({"code": 0}) == []


# ── client ─────────────────────────────────────────────────────────────────────────────────


def test_gcode_files_posts_bare_credentials():
    client, transport = make([{"code": 0, "gcodeList": ["benchy.gcode.3mf"],
                               "gcodeListDetail": [detail_entry("benchy.gcode.3mf")]}])
    files = client.gcode_files()
    url, body = transport.calls[0]
    assert url == "http://10.0.0.10:8898/gcodeList"
    assert body == {"serialNumber": "SN123", "checkCode": "CODE"}
    assert files[0]["name"] == "benchy.gcode.3mf"


def test_thumbnail_decodes_the_base64_image():
    client, transport = make([{"code": 0, "imageData": base64.b64encode(PNG).decode()}])
    assert client.gcode_thumbnail("benchy.gcode.3mf") == PNG
    url, body = transport.calls[0]
    assert url == "http://10.0.0.10:8898/gcodeThumb"
    assert body["fileName"] == "benchy.gcode.3mf"


def test_thumbnail_is_none_when_there_is_no_usable_image():
    client, _ = make([{"code": 0}, {"code": 0, "imageData": ""}, {"code": 0, "imageData": "!!not base64!!"}])
    assert client.gcode_thumbnail("a") is None
    assert client.gcode_thumbnail("b") is None
    assert client.gcode_thumbnail("c") is None


def test_thumbnail_rejects_a_reply_that_is_not_a_png():
    client, _ = make([{"code": 0, "imageData": base64.b64encode(b"<html>nope</html>").decode()}])
    assert client.gcode_thumbnail("a") is None


def test_print_gcode_sends_the_payload_the_firmware_expects():
    client, transport = make([{"code": 0}])
    client.print_gcode("benchy.gcode.3mf", leveling=True,
                       material_mappings=[{"toolId": 0, "slotId": 3}])
    _, body = transport.calls[0]
    assert body["fileName"] == "benchy.gcode.3mf"
    assert body["levelingBeforePrint"] is True
    assert body["flowCalibration"] is False
    assert body["useMatlStation"] is True
    assert body["gcodeToolCnt"] == 1
    assert body["materialMappings"] == [{"toolId": 0, "slotId": 3}]


def test_print_gcode_without_mappings_does_not_claim_the_material_station():
    client, transport = make([{"code": 0}])
    client.print_gcode("benchy.gcode.3mf")
    _, body = transport.calls[0]
    assert body["useMatlStation"] is False
    assert body["gcodeToolCnt"] == 0
    assert body["materialMappings"] == []


def test_a_refused_print_raises():
    client, _ = make([{"code": 5, "message": "busy"}])
    with pytest.raises(FlashforgeError, match="busy"):
        client.print_gcode("benchy.gcode.3mf")


# ── the agent's guard on starting a print ──────────────────────────────────────────────────
#
# The console has no login: the README's argument was that pausing is a nuisance if abused rather
# than a hazard, and that the page never touches heaters. Starting a print breaks both halves, so
# the guard carries the weight the absent login would have: never while the machine is busy, and
# never a name the printer did not itself list.

from flashforge_obico.agent import PrintRefused, may_start_print
from flashforge_obico.flashforge.snapshot import MachineState


def test_a_ready_printer_accepts_a_listed_file():
    assert may_start_print(MachineState.READY, "benchy.gcode.3mf", ["benchy.gcode.3mf"]) is None


@pytest.mark.parametrize("state", [MachineState.PRINTING, MachineState.PAUSED, MachineState.HEATING,
                                   MachineState.BUSY, MachineState.ERROR, MachineState.UNKNOWN])
def test_a_machine_that_is_not_ready_refuses(state):
    refusal = may_start_print(state, "benchy.gcode.3mf", ["benchy.gcode.3mf"])
    assert refusal is PrintRefused.NOT_READY


def test_a_finished_job_still_counts_as_ready_to_start_another():
    for state in (MachineState.COMPLETED, MachineState.CANCELLED):
        assert may_start_print(state, "benchy.gcode.3mf", ["benchy.gcode.3mf"]) is None


def test_a_file_the_printer_never_listed_is_refused():
    refusal = may_start_print(MachineState.READY, "../../etc/passwd", ["benchy.gcode.3mf"])
    assert refusal is PrintRefused.UNKNOWN_FILE


def test_an_empty_name_is_refused():
    assert may_start_print(MachineState.READY, "", ["benchy.gcode.3mf"]) is PrintRefused.UNKNOWN_FILE


def test_the_match_is_exact_not_a_prefix():
    assert may_start_print(MachineState.READY, "benchy", ["benchy.gcode.3mf"]) is PrintRefused.UNKNOWN_FILE


def test_an_unreadable_state_is_refused_rather_than_assumed_idle():
    assert may_start_print(None, "benchy.gcode.3mf", ["benchy.gcode.3mf"]) is PrintRefused.NOT_READY
