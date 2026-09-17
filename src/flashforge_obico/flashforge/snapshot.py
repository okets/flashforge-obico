"""A typed view of one `detail` reply from the printer."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .parsing import as_float, as_int, as_str


class MachineState(str, Enum):
    READY = "ready"
    BUSY = "busy"
    HEATING = "heating"
    PRINTING = "printing"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


MIN_PROGRESS_FOR_ESTIMATE = 0.02
ACTIVE_STATES = frozenset({MachineState.PRINTING, MachineState.HEATING, MachineState.BUSY, MachineState.PAUSED})
# Firmware 1.9.9 reports the short forms; the long forms are kept so a future firmware that says
# "paused" is not suddenly unknown. Verified on hardware 2026-09-15.
_ALIASES = {"cancel": "cancelled", "pause": "paused"}


def parse_state(raw) -> MachineState:
    text = as_str(raw).strip().lower()
    text = _ALIASES.get(text, text)
    try:
        return MachineState(text)
    except ValueError:
        return MachineState.UNKNOWN


@dataclass(frozen=True)
class MaterialSlot:
    slot_id: int
    has_filament: bool
    material: str
    color: str


@dataclass(frozen=True)
class PrinterSnapshot:
    state: MachineState
    file_name: str
    progress: float  # 0.0 to 1.0
    duration_s: int
    remaining_s: int
    layer: int | None
    total_layers: int | None
    error_code: str
    camera_stream_url: str
    nozzle_temps: tuple[float, ...]
    nozzle_targets: tuple[float, ...]
    bed_temp: float | None
    bed_target: float | None
    chamber_temp: float | None
    chamber_target: float | None
    model: str
    firmware: str
    light_on: bool = False
    door_open: bool = False
    slots: tuple[MaterialSlot, ...] = ()

    @property
    def has_job(self) -> bool:
        return self.file_name != "" and self.state in ACTIVE_STATES

    @property
    def remaining_estimate_s(self) -> int | None:
        """Time left, projected from elapsed time and progress. The firmware's own `estimatedTime`
        does not mean "remaining" (on 1.9.9 it tracked the elapsed time, observed 2026-09-18), so the
        estimate is ours; it needs a few percent of progress before the projection is worth showing."""
        if not self.has_job or self.duration_s <= 0 or self.progress < MIN_PROGRESS_FOR_ESTIMATE:
            return None
        return int(round(self.duration_s * (1.0 - self.progress) / self.progress))

    @property
    def warming_up(self) -> bool:
        """The firmware says "printing" from the moment a job is accepted, while it is still heating.
        Nothing has been extruded until printDuration or printLayer moves off zero. The firmware also
        ignores job-control commands during this phase (observed 2026-09-15)."""
        return (self.has_job and self.state is not MachineState.PAUSED
                and self.duration_s == 0 and not self.layer)


def _floats(value) -> tuple[float, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(f for f in (as_float(v) for v in value) if f is not None)


def _slots(detail: dict) -> tuple[MaterialSlot, ...]:
    station = detail.get("matlStationInfo")
    infos = station.get("slotInfos") if isinstance(station, dict) else None
    if not isinstance(infos, list):
        return ()
    slots = []
    for index, info in enumerate(infos):
        if not isinstance(info, dict):
            continue
        slots.append(MaterialSlot(slot_id=as_int(info.get("slotId")) or index + 1,
                                  has_filament=bool(as_int(info.get("hasFilament"))),
                                  material=as_str(info.get("materialName")),
                                  color=as_str(info.get("materialColor"))))
    return tuple(slots)


def parse_snapshot(detail: dict) -> PrinterSnapshot:
    progress = as_float(detail.get("printProgress")) or 0.0
    return PrinterSnapshot(
        state=parse_state(detail.get("status")),
        file_name=as_str(detail.get("printFileName")),
        progress=min(max(progress, 0.0), 1.0),
        duration_s=as_int(detail.get("printDuration")) or 0,
        remaining_s=int(as_float(detail.get("estimatedTime")) or 0),
        layer=as_int(detail.get("printLayer")),
        total_layers=as_int(detail.get("targetPrintLayer")),
        error_code=as_str(detail.get("errorCode")),
        camera_stream_url=as_str(detail.get("cameraStreamUrl")),
        nozzle_temps=_floats(detail.get("nozzleTemps")),
        nozzle_targets=_floats(detail.get("nozzleTargetTemps")),
        bed_temp=as_float(detail.get("platTemp")),
        bed_target=as_float(detail.get("platTargetTemp")),
        chamber_temp=as_float(detail.get("chamberTemp")),
        chamber_target=as_float(detail.get("chamberTargetTemp")),
        model=as_str(detail.get("model")),
        firmware=as_str(detail.get("firmwareVersion")),
        light_on=as_str(detail.get("lightStatus")).lower() == "open",
        door_open=as_str(detail.get("doorStatus")).lower() == "open",
        slots=_slots(detail),
    )
