"""Wires the loops together: poll the printer, track the job, tell Obico, post frames, run commands."""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable

from .camera.mjpeg_source import MjpegSource
from .config import Config
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
EXIT_OK, EXIT_SHARED_TOKEN = 0, 2
COMMANDS = ("pause", "resume", "cancel")


class Agent:
    def __init__(self, config: Config, printer: FlashforgeClient, obico, cameras: list[MjpegSource], *,
                 clock: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.config, self.printer, self.obico, self.cameras = config, printer, obico, cameras
        self._clock, self._monotonic, self._sleep = clock, monotonic, sleep
        self.tracker = JobTracker()
        self.viewing = False
        self._snapshot: PrinterSnapshot | None = None
        self._state_text = OFFLINE
        self._failures = 0
        self._settings_sent = False
        self._last_sent_at = float("-inf")
        self._faults_reported: set[str] = set()
        self._commands: queue.Queue[str] = queue.Queue()
        self._command_lock = threading.Lock()
        self._command_thread: threading.Thread | None = None

    # ── printer side ───────────────────────────────────────────────────────────────────────
    def poll_once(self) -> None:
        if not self._read_printer():
            return
        now = self._clock()
        events = self.tracker.observe(self._snapshot, now)
        new_text = obico_state_text(self._snapshot, self._state_text)
        changed = new_text != self._state_text or bool(events) or not self._settings_sent
        self._state_text = new_text
        self._report_fault()
        if changed or now - self._last_sent_at >= HEARTBEAT_S:
            self._send_status(events[0] if events else None, now)
            for extra in events[1:]:
                self._send_status(extra, now)

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
        """Forwards one Obico command to the printer and confirms it took by reading the state back."""
        with self._command_lock:
            snapshot = self._snapshot
            if snapshot is None or not snapshot.has_job:
                _logger.info("ignoring %s: no active job", cmd)
                return
            paused = snapshot.state is MachineState.PAUSED
            if (cmd == "pause" and paused) or (cmd == "resume" and not paused):
                _logger.info("ignoring %s: the printer is already in that state", cmd)
                return
            _logger.info("Obico asked to %s; forwarding to the printer", cmd)
            try:
                getattr(self.printer, cmd)()
            except FlashforgeError as exc:
                self.obico.post_printer_event(title=f"{cmd.capitalize()} failed", text=str(exc))
                return
            if not self._read_back(cmd):
                state = self._snapshot.state.value if self._snapshot else "unreachable"
                self.obico.post_printer_event(title=f"{cmd.capitalize()} did not take",
                                              text=f"The printer was asked to {cmd} but still reports '{state}'.")

    def _read_back(self, cmd: str) -> bool:
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
                return True
        return False

    def post_primary_frame(self) -> bool:
        if not self.cameras:
            return False
        latest = self.cameras[0].latest()
        if latest is None or self._monotonic() - latest[1] > PIC_MAX_AGE_S:
            return False
        return self.obico.post_pic(latest[0], camera_name=self.cameras[0].name, is_primary=True,
                                   viewing_boost=self.viewing)

    # ── main loop ──────────────────────────────────────────────────────────────────────────
    def run(self, stop: threading.Event) -> int:
        threading.Thread(target=self._pic_loop, args=(stop,), name="pics", daemon=True).start()
        while not stop.is_set():
            if self.obico.shared_token_detected.is_set():
                return EXIT_SHARED_TOKEN
            self.poll_once()
            stop.wait(self.poll_interval())
        return EXIT_OK

    def _pic_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.post_primary_frame()
            stop.wait(PIC_BOOST_INTERVAL_S if self.viewing else PIC_INTERVAL_S)
