"""Wires the loops together: poll the printer, track the job, tell Obico, post frames, run commands."""
from __future__ import annotations

import logging
from enum import Enum
import queue
import threading
import time
from typing import Callable

from .camera.mjpeg_source import MjpegSource
from . import VERSION
from .config import Config
from .console.status import build_console_status
from .flashforge.client import FlashforgeClient, FlashforgeError
from .flashforge.snapshot import MachineState, PrinterSnapshot, parse_snapshot
from .job_tracker import Event, JobTracker
from .obico.status import (OFFLINE, PAUSED, PRINTING, build_message, build_settings, build_status,
                           obico_state_text, webcam_entry)

_logger = logging.getLogger(__name__)

POLL_ACTIVE_S, POLL_IDLE_S = 2.0, 5.0
PIC_INTERVAL_S, PIC_BOOST_INTERVAL_S, PIC_MAX_AGE_S = 10.0, 1.0, 15.0
HEARTBEAT_S = 30.0
OFFLINE_AFTER_FAILURES = 3
READBACK_ATTEMPTS, READBACK_INTERVAL_S = 5, 1.0
MAX_COMMAND_ATTEMPTS = 3   # send + read-back cycles before a command is declared not taken
EXIT_OK, EXIT_SHARED_TOKEN = 0, 2
COMMANDS = ("pause", "resume", "cancel")


class PrintRefused(str, Enum):
    """Why a start was refused. The page shows the reason; the log records it."""
    NOT_READY = "not_ready"
    UNKNOWN_FILE = "unknown_file"


# A finished or cancelled job leaves the machine idle, so those count as ready; everything else,
# including a state we could not read, does not. Pure so the rule can be tested without a printer.
_READY_TO_START = frozenset({MachineState.READY, MachineState.COMPLETED, MachineState.CANCELLED})


def may_start_print(state, file_name: str, known_files) -> PrintRefused | None:
    """None when the start is allowed, otherwise why not.

    The console has no login. That was defensible while it only paused a print and never touched a
    heater; starting one is neither. So this carries the weight the absent login would have: the
    machine must be idle, and the name must be one the printer itself listed -- which also means a
    caller cannot reach a path of its own choosing.
    """
    if state not in _READY_TO_START:
        return PrintRefused.NOT_READY
    if not file_name or file_name not in set(known_files):
        return PrintRefused.UNKNOWN_FILE
    return None


class Agent:
    def __init__(self, config: Config, printer: FlashforgeClient, obico, cameras: list[MjpegSource], *,
                 clock: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.config, self.printer, self.obico, self.cameras = config, printer, obico, cameras
        self._clock, self._monotonic, self._sleep = clock, monotonic, sleep
        # Set by the CLI: where a camera discovered later is registered, and how one is built.
        self.reserver = None
        self.camera_factory = lambda url, name: MjpegSource(url, name=name)
        self.tracker = JobTracker()
        self.viewing = False
        self._snapshot: PrinterSnapshot | None = None
        self._state_text = OFFLINE
        self._failures = 0
        self._settings_sent = False
        self._last_sent_at = float("-inf")
        self._faults_reported: set[str] = set()
        # A pause the printer ignored because it was still warming up; re-issued once extrusion starts.
        self.pending_command: str | None = None
        self._commands: queue.Queue[str] = queue.Queue()
        self._command_lock = threading.Lock()
        self._command_thread: threading.Thread | None = None

    # ── printer side ───────────────────────────────────────────────────────────────────────
    def poll_once(self) -> None:
        if not self._read_printer():
            return
        self._discover_camera()
        self._publish(self._clock())
        self._report_fault()
        self._service_pending_command()

    def _discover_camera(self) -> None:
        """Without configured cameras, the printer's own camera is taken from the first `detail` that
        names one. Done here rather than at startup so an agent that boots while the printer is still
        off (a power outage, say) picks the camera up as soon as the printer answers."""
        snapshot = self._snapshot
        if self.cameras or snapshot is None or not snapshot.camera_stream_url:
            return
        camera = self.camera_factory(snapshot.camera_stream_url, "Printer")
        camera.start()
        self.cameras.append(camera)
        if self.reserver is not None:
            self.reserver.add_source(camera)
        _logger.info("camera discovered from the printer: %s", snapshot.camera_stream_url)

    def _publish(self, now: float) -> None:
        """Runs the tracker over the current snapshot and tells Obico about anything that changed."""
        events = self.tracker.observe(self._snapshot, now)
        new_text = obico_state_text(self._snapshot, self._state_text)
        changed = new_text != self._state_text or bool(events) or not self._settings_sent
        self._state_text = new_text
        if changed or now - self._last_sent_at >= HEARTBEAT_S:
            self._send_status(events[0] if events else None, now)
            for extra in events[1:]:
                self._send_status(extra, now)

    def _service_pending_command(self) -> None:
        snapshot = self._snapshot
        if self.pending_command is None or snapshot is None:
            return
        if not snapshot.has_job:
            _logger.info("dropping pending %s: the job is over", self.pending_command)
            self.pending_command = None
            return
        if snapshot.state is MachineState.PAUSED:
            self.pending_command = None
            return
        if snapshot.warming_up:
            return  # the firmware still ignores job control; keep waiting
        cmd, self.pending_command = self.pending_command, None
        _logger.info("printing has started; issuing the deferred %s", cmd)
        self.execute_command(cmd)

    def _read_printer(self) -> bool:
        """Refreshes the snapshot. False while failures are still below the offline threshold."""
        try:
            self._snapshot = parse_snapshot(self.printer.detail())
            self._failures = 0
            return True
        except FlashforgeError as exc:
            self._failures += 1
            _logger.warning("printer poll failed (%d in a row): %s", self._failures, exc)
            if self._failures < OFFLINE_AFTER_FAILURES:
                return False
            self._snapshot = None
            return True

    def _send_status(self, event: Event | None, now: float) -> None:
        settings = None if self._settings_sent else build_settings(self._webcams())
        status = build_status(self._snapshot, self.tracker, self._state_text, now)
        self.obico.send(build_message(status=status, current_print_ts=self.tracker.current_print_ts,
                                      event=event, settings=settings))
        self._settings_sent = True
        self._last_sent_at = now
        if event is not None:
            _logger.info("%s (%s)", event.value, self.tracker.active_file or "no file")

    def _webcams(self) -> list[dict]:
        names = [camera.name for camera in self.cameras] or ["Printer"]
        return [webcam_entry(name=name, is_primary=(index == 0),
                             stream_url=self.config.camera_public_url(index, "stream"),
                             snapshot_url=self.config.camera_public_url(index, "snapshot"))
                for index, name in enumerate(names)]

    def _report_fault(self) -> None:
        snapshot = self._snapshot
        if snapshot is None or not snapshot.error_code or snapshot.error_code in self._faults_reported:
            return
        self._faults_reported.add(snapshot.error_code)
        _logger.error("printer reports error code %s", snapshot.error_code)
        self.obico.post_printer_event(title=f"Printer error {snapshot.error_code}",
                                      text=f"The printer reports error code {snapshot.error_code}.",
                                      snapshot=self._latest_primary_frame())

    def _latest_primary_frame(self) -> bytes | None:
        latest = self.cameras[0].latest() if self.cameras else None
        return latest[0] if latest else None

    def poll_interval(self) -> float:
        return POLL_ACTIVE_S if self._state_text in (PRINTING, PAUSED) else POLL_IDLE_S

    # ── the phone console ──────────────────────────────────────────────────────────────────
    def console_status(self) -> dict:
        return build_console_status(snapshot=self._snapshot, state_text=self._state_text,
                                    obico_connected=self.obico.connected.is_set(), pending_command=self.pending_command,
                                    viewing=self.viewing, camera_count=len(self.cameras), version=VERSION,
                                    now=self._clock())

    def set_light(self, on: bool) -> bool:
        """The chamber light, from the phone console. Immediate; the next poll shows the new state."""
        try:
            self.printer.set_light(on)
        except FlashforgeError as exc:
            _logger.warning("light %s refused: %s", "on" if on else "off", exc)
            return False
        _logger.info("console switched the light %s", "on" if on else "off")
        return True

    def files(self) -> list[dict]:
        """The printer's stored files. An unreachable printer is an empty list, not an error page."""
        try:
            return self.printer.gcode_files()
        except FlashforgeError as exc:
            _logger.warning("file list refused: %s", exc)
            return []

    def thumbnail(self, file_name: str) -> bytes | None:
        try:
            return self.printer.gcode_thumbnail(file_name)
        except FlashforgeError as exc:
            _logger.warning("thumbnail for %r refused: %s", file_name, exc)
            return None

    def start_print(self, file_name: str) -> PrintRefused | None:
        """Start a stored file from the phone console. None on success, otherwise why not.

        The file list is re-read here rather than trusted from the page: the guard has to check the
        name against what the printer says it holds right now, not against what a stale tab shows.
        """
        try:
            files = self.printer.gcode_files()
        except FlashforgeError as exc:
            _logger.warning("could not read the file list before starting %r: %s", file_name, exc)
            return PrintRefused.UNKNOWN_FILE

        state = self._snapshot.state if self._snapshot else None
        refusal = may_start_print(state, file_name, [entry["name"] for entry in files])
        if refusal is not None:
            _logger.warning("refused to start %r: %s", file_name, refusal.value)
            return refusal

        # Reuse the tool-to-slot mapping the file was sliced with; the firmware needs it to feed the
        # right material, and the file itself is the only place that knows.
        chosen = next((entry for entry in files if entry["name"] == file_name), {})
        mappings = [{"toolId": tool["tool_id"], "slotId": tool["slot_id"]}
                    for tool in chosen.get("tools", [])
                    if tool["tool_id"] is not None and tool["slot_id"] is not None]

        try:
            self.printer.print_gcode(file_name, material_mappings=mappings if chosen.get("uses_material_station") else [])
        except FlashforgeError as exc:
            _logger.warning("printer refused to start %r: %s", file_name, exc)
            return PrintRefused.NOT_READY
        _logger.info("console started %r", file_name)
        return None

    def request_command(self, cmd: str) -> bool:
        """A pause/resume from the phone console. Queued like an Obico command, so it gets the same
        retries, read-back and warm-up deferral. False for anything else."""
        if cmd not in ("pause", "resume"):
            return False
        _logger.info("console asked to %s", cmd)
        self._commands.put(cmd)
        self._ensure_command_thread()
        return True

    # ── Obico side ─────────────────────────────────────────────────────────────────────────
    def handle_server_message(self, message: dict) -> None:
        remote = message.get("remote_status")
        if isinstance(remote, dict) and "viewing" in remote:
            self.viewing = bool(remote["viewing"])
        for command in message.get("commands") or []:
            cmd = command.get("cmd") if isinstance(command, dict) else None
            if cmd in COMMANDS:
                self._commands.put(cmd)
                self._ensure_command_thread()
            else:
                _logger.warning("ignoring unknown command from Obico: %r", command)

    def _ensure_command_thread(self) -> None:
        if self._command_thread is None or not self._command_thread.is_alive():
            self._command_thread = threading.Thread(target=self._drain_commands, name="commands", daemon=True)
            self._command_thread.start()

    def _drain_commands(self) -> None:
        while True:
            try:
                cmd = self._commands.get(timeout=0.5)
            except queue.Empty:
                return
            try:
                self.execute_command(cmd)
            except Exception:
                _logger.exception("command %s failed unexpectedly", cmd)
            finally:
                self._commands.task_done()

    def wait_for_commands(self, timeout: float) -> bool:
        """For tests: True once every queued command has finished."""
        deadline = time.monotonic() + timeout
        while self._commands.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        return self._commands.unfinished_tasks == 0

    def execute_command(self, cmd: str) -> None:
        """Forwards one Obico command to the printer and confirms it took by reading the state back.

        The printer is the authority: a command is only "done" when `detail` reports the new state.
        Firmware 1.9.9 acknowledges a pause with code 0 during warm-up and then ignores it, so a
        pause that does not take while the machine is still heating is kept as a pending intent and
        re-issued the moment extrusion starts; a resume or cancel from the user drops that intent."""
        with self._command_lock:
            if cmd in ("resume", "cancel") and self.pending_command is not None:
                _logger.info("%s requested: dropping the pending %s", cmd, self.pending_command)
                self.pending_command = None
            snapshot = self._snapshot
            if snapshot is None or not snapshot.has_job:
                _logger.info("ignoring %s: no active job", cmd)
                return
            paused = snapshot.state is MachineState.PAUSED
            if (cmd == "pause" and paused) or (cmd == "resume" and not paused):
                _logger.info("ignoring %s: the printer is already in that state", cmd)
                return
            _logger.info("Obico asked to %s; forwarding to the printer", cmd)
            outcome = self._attempt_command(cmd)
            if outcome == "taken":
                _logger.info("%s confirmed by the printer", cmd)
            elif outcome == "job-over":
                _logger.info("%s moot: the job ended meanwhile", cmd)
            elif cmd == "pause" and self._snapshot is not None and self._snapshot.warming_up:
                self.pending_command = "pause"
                _logger.warning("the printer ignores pause while warming up; will pause once printing starts")
                self.obico.post_printer_event(
                    title="Pause deferred until printing starts", event_class="WARNING",
                    text="The printer accepted the pause but ignores job control while it is still heating. "
                         "The pause will be sent again automatically as soon as the first layer starts.")
            else:
                state = self._snapshot.state.value if self._snapshot else "unreachable"
                _logger.error("%s did not take after %d attempts; printer reports %r", cmd, MAX_COMMAND_ATTEMPTS, state)
                self.obico.post_printer_event(title=f"{cmd.capitalize()} did not take",
                                              text=f"The printer was asked to {cmd} {MAX_COMMAND_ATTEMPTS} times "
                                                   f"but still reports '{state}'.")
            # Whatever happened, Obico gets the printer's real state now rather than at the next heartbeat.
            self._publish(self._clock())

    def _attempt_command(self, cmd: str) -> str:
        """Sends `cmd` up to MAX_COMMAND_ATTEMPTS times, reading the state back after each.
        Returns "taken", "job-over" or "not-taken"."""
        for attempt in range(1, MAX_COMMAND_ATTEMPTS + 1):
            try:
                getattr(self.printer, cmd)()
                _logger.info("printer accepted %s (attempt %d, result code 0)", cmd, attempt)
            except FlashforgeError as exc:
                _logger.warning("printer refused %s (attempt %d): %s", cmd, attempt, exc)
                if attempt == MAX_COMMAND_ATTEMPTS:
                    self.obico.post_printer_event(title=f"{cmd.capitalize()} failed", text=str(exc))
                    return "not-taken"
                continue
            result = self._read_back(cmd)
            if result != "not-taken":
                return result
        return "not-taken"

    def _read_back(self, cmd: str) -> str:
        expected = {
            "pause": lambda s: s.state is MachineState.PAUSED,
            "resume": lambda s: s.has_job and s.state is not MachineState.PAUSED,
            "cancel": lambda s: not s.has_job,
        }[cmd]
        for _ in range(READBACK_ATTEMPTS):
            self._sleep(READBACK_INTERVAL_S)
            try:
                self._snapshot = parse_snapshot(self.printer.detail())
            except FlashforgeError:
                continue
            if expected(self._snapshot):
                return "taken"
            if cmd != "cancel" and not self._snapshot.has_job:
                return "job-over"
        return "not-taken"

    def post_primary_frame(self) -> bool:
        """The regular frame, every PIC_INTERVAL_S. Never flagged as a viewing frame: Obico writes a
        viewing frame to one fixed path whose signed URL never changes (so clients keep showing a
        cached picture) and skips failure detection on it. Regular frames during a print get unique
        URLs and go to the detector, which is the whole point."""
        return self._post_frame(viewing_boost=False)

    def post_boost_frame(self) -> bool:
        """An extra frame while someone is watching in the Obico app, as moonraker-obico does."""
        if not self.viewing:
            return False
        return self._post_frame(viewing_boost=True)

    def _post_frame(self, *, viewing_boost: bool) -> bool:
        if not self.cameras:
            return False
        latest = self.cameras[0].latest()
        if latest is None or self._monotonic() - latest[1] > PIC_MAX_AGE_S:
            return False
        return self.obico.post_pic(latest[0], camera_name=self.cameras[0].name, is_primary=True,
                                   viewing_boost=viewing_boost)

    # ── main loop ──────────────────────────────────────────────────────────────────────────
    def run(self, stop: threading.Event) -> int:
        threading.Thread(target=self._pic_loop, args=(stop,), name="pics", daemon=True).start()
        threading.Thread(target=self._boost_loop, args=(stop,), name="pics-boost", daemon=True).start()
        while not stop.is_set():
            if self.obico.shared_token_detected.is_set():
                return EXIT_SHARED_TOKEN
            self.poll_once()
            stop.wait(self.poll_interval())
        return EXIT_OK

    def _pic_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.post_primary_frame()
            stop.wait(PIC_INTERVAL_S)

    def _boost_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.post_boost_frame()
            stop.wait(PIC_BOOST_INTERVAL_S)
