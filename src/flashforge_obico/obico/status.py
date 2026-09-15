"""The Obico agent payload, in the shape of moonraker-obico's PrinterState.to_status()/to_dict()."""
from __future__ import annotations

import platform

from .. import VERSION
from ..flashforge.snapshot import MachineState, PrinterSnapshot
from ..job_tracker import Event, JobTracker

OFFLINE, OPERATIONAL, PRINTING, PAUSED = "Offline", "Operational", "Printing", "Paused"
AGENT_NAME = "flashforge_obico"


def obico_state_text(snapshot: PrinterSnapshot | None, previous: str) -> str:
    """Maps a snapshot to Obico's state text. Unfamiliar states keep the previous text, so a mid-print
    value we have never seen can never flip the printer to Operational."""
    if snapshot is None:
        return OFFLINE
    if snapshot.state is MachineState.UNKNOWN:
        return previous
    if snapshot.has_job:
        return PAUSED if snapshot.state is MachineState.PAUSED else PRINTING
    if snapshot.state is MachineState.BUSY:
        return previous  # busy with no file: a job may be spinning up
    return OPERATIONAL


def _temperature(actual: float | None, target: float | None) -> dict:
    return {"actual": round(actual if actual is not None else 0.0, 2), "offset": 0, "target": target}


def _temperatures(snapshot: PrinterSnapshot) -> dict:
    temps = {}
    for index, actual in enumerate(snapshot.nozzle_temps):
        target = snapshot.nozzle_targets[index] if index < len(snapshot.nozzle_targets) else None
        temps[f"tool{index}"] = _temperature(actual, target)
    temps["bed"] = _temperature(snapshot.bed_temp, snapshot.bed_target)
    temps["chamber"] = _temperature(snapshot.chamber_temp, snapshot.chamber_target)
    return temps


def build_status(snapshot: PrinterSnapshot | None, tracker: JobTracker, state_text: str, now: float) -> dict:
    if snapshot is None or state_text == OFFLINE:
        return {}
    has_error = snapshot.state is MachineState.ERROR
    file_name = snapshot.file_name or None
    active = tracker.active_file is not None
    return {
        "_ts": now,
        "state": {
            "text": state_text,
            "flags": {
                "operational": True,
                "paused": state_text == PAUSED,
                "printing": state_text == PRINTING,
                "cancelling": False,
                "pausing": False,
                "error": has_error,
                "ready": state_text == OPERATIONAL,
                "closedOrError": False,
            },
            "error": (snapshot.error_code or None) if has_error else None,
        },
        "job": {
            "file": {"name": file_name, "path": file_name, "display": file_name, "obico_g_code_file_id": None},
            "estimatedPrintTime": None,
            "user": None,
        },
        "progress": {
            "completion": snapshot.progress * 100 if active else None,
            "filepos": 0,
            "printTime": snapshot.duration_s if active else None,
            "printTimeLeft": snapshot.remaining_s if active else None,
            "filamentUsed": None,
        },
        "temperatures": _temperatures(snapshot),
        "file_metadata": {
            "analysis": {"printingArea": {"maxZ": None}},
            "obico": {"totalLayerCount": snapshot.total_layers if active else None},
        },
        "currentLayerHeight": snapshot.layer if active else None,
        "currentFeedRate": None,
        "currentFlowRate": None,
        "currentFanSpeed": None,
        "currentZ": None,
        "display_status": {},
    }


def webcam_entry(*, name: str, is_primary: bool, stream_url: str, snapshot_url: str) -> dict:
    """One entry of `settings.webcams`. The first ten keys are what moonraker-obico sends; `stream_url`
    and `snapshot_url` are ours and tell LAN viewers where the re-served stream lives."""
    return {
        "name": name,
        "is_primary_camera": is_primary,
        "is_nozzle_camera": False,
        "stream_mode": "h264_transcode",
        "stream_id": None,
        "data_channel_available": False,
        "flipV": False,
        "flipH": False,
        "rotation": 0,
        "streamRatio": "16:9",
        "stream_url": stream_url,
        "snapshot_url": snapshot_url,
    }


def build_settings(webcams: list[dict]) -> dict:
    return {
        "webcams": webcams,
        "data_channel_id": None,
        "temperature": {"profiles": []},
        "agent": {"name": AGENT_NAME, "version": VERSION},
        "platform_uname": list(platform.uname()) + [""],
        "installed_plugins": [],
    }


def build_message(*, status: dict, current_print_ts: int, event: Event | None = None,
                  settings: dict | None = None) -> dict:
    message: dict = {"current_print_ts": current_print_ts, "status": status}
    if event is not None:
        message["event"] = {"event_type": event.value}
    if settings is not None:
        message["settings"] = settings
    return message
