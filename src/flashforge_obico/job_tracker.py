"""Turns consecutive printer snapshots into Obico print events.

Owns `current_print_ts`, the integer Obico uses to identify one print session (-1 = no job).
"""
from __future__ import annotations

from enum import Enum

from .flashforge.snapshot import MachineState, PrinterSnapshot

DONE_THRESHOLD = 0.995


class Event(str, Enum):
    STARTED = "PrintStarted"
    PAUSED = "PrintPaused"
    RESUMED = "PrintResumed"
    DONE = "PrintDone"
    CANCELLED = "PrintCancelled"
    FAILED = "PrintFailed"


class JobTracker:
    def __init__(self) -> None:
        self.current_print_ts = -1
        self.active_file: str | None = None
        self.paused = False
        self._last_progress = 0.0

    def observe(self, snapshot: PrinterSnapshot | None, now: float) -> list[Event]:
        """`snapshot` None means the printer could not be read; an unknown state is not a change."""
        if snapshot is None or snapshot.state is MachineState.UNKNOWN:
            return []
        events: list[Event] = []
        if self.active_file is not None and (not snapshot.has_job or snapshot.file_name != self.active_file):
            events.append(self._end(snapshot))
        if snapshot.has_job:
            if self.active_file is None:
                events.append(self._start(snapshot, now))
            else:
                events.extend(self._pause_transition(snapshot))
            self._last_progress = snapshot.progress
        return events

    def _start(self, snapshot: PrinterSnapshot, now: float) -> Event:
        self.current_print_ts = int(now)
        self.active_file = snapshot.file_name
        self.paused = snapshot.state is MachineState.PAUSED
        return Event.STARTED

    def _pause_transition(self, snapshot: PrinterSnapshot) -> list[Event]:
        paused_now = snapshot.state is MachineState.PAUSED
        if paused_now == self.paused:
            return []
        self.paused = paused_now
        return [Event.PAUSED if paused_now else Event.RESUMED]

    def _end(self, snapshot: PrinterSnapshot) -> Event:
        event = self._end_event(snapshot)
        self.current_print_ts = -1
        self.active_file = None
        self.paused = False
        self._last_progress = 0.0
        return event

    def _end_event(self, snapshot: PrinterSnapshot) -> Event:
        if snapshot.state is MachineState.COMPLETED:
            return Event.DONE
        if snapshot.state is MachineState.CANCELLED:
            return Event.CANCELLED
        finished = self._last_progress >= DONE_THRESHOLD
        if snapshot.state is MachineState.READY and finished:
            return Event.DONE
        return Event.FAILED
