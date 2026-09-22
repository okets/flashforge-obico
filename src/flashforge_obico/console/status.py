"""The JSON the phone console polls: what the agent already knows, shaped for one small page."""
from __future__ import annotations

from typing import Sequence

from ..camera.mjpeg_source import CameraHealth
from ..flashforge.snapshot import MachineState, PrinterSnapshot


def _camera(health: CameraHealth) -> dict:
    """Enough for the page to say why a picture is missing instead of showing a broken image."""
    return {"name": health.name, "streaming": health.streaming,
            "frame_age_s": None if health.frame_age_s is None else round(health.frame_age_s, 1),
            "last_error": health.last_error}


def build_console_status(*, snapshot: PrinterSnapshot | None, state_text: str, obico_connected: bool,
                         pending_command: str | None, viewing: bool,
                         cameras: Sequence[CameraHealth], version: str, now: float) -> dict:
    """`connected` is false when the printer is unreachable; the rest of the printer block is then absent."""
    status: dict = {
        "ts": now,
        "version": version,
        "connected": snapshot is not None,
        "state_text": state_text,
        "obico": {"connected": obico_connected, "viewing": viewing, "pending_command": pending_command},
        "camera": "/cameras/0/stream" if cameras else None,
        "cameras": [_camera(health) for health in cameras],
        "controls": ["pause", "resume", "light"],
    }
    if snapshot is None:
        return status
    paused = snapshot.state is MachineState.PAUSED
    status["printer"] = {
        "state": snapshot.state.value,
        "has_job": snapshot.has_job,
        "warming_up": snapshot.warming_up,
        "paused": paused,
        "file": snapshot.file_name,
        "progress": snapshot.progress,
        "duration_s": snapshot.duration_s,
        "remaining_est_s": snapshot.remaining_estimate_s,       # our projection; None until progress is meaningful
        "firmware_estimated_s": snapshot.remaining_s,            # the printer's estimatedTime, kept for study
        "layer": snapshot.layer,
        "total_layers": snapshot.total_layers,
        "error_code": snapshot.error_code,
        "light_on": snapshot.light_on,
        "door_open": snapshot.door_open,
        "model": snapshot.model,
        "temperatures": {
            "bed": {"current": snapshot.bed_temp, "target": snapshot.bed_target},
            "chamber": {"current": snapshot.chamber_temp, "target": snapshot.chamber_target},
            "nozzles": [{"tool": i, "current": snapshot.nozzle_temps[i],
                         "target": snapshot.nozzle_targets[i] if i < len(snapshot.nozzle_targets) else None}
                        for i in range(len(snapshot.nozzle_temps))],
        },
        "slots": [{"slot_id": m.slot_id, "has_filament": m.has_filament, "material": m.material, "color": m.color}
                  for m in snapshot.slots],
        # What the buttons may do right now; the page greys out the rest.
        "can_pause": snapshot.has_job and not paused,
        "can_resume": snapshot.has_job and paused,
    }
    return status
