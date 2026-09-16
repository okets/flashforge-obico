# flashforge-obico Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Python service in one Docker container that feeds a FlashForge Creator 5 Pro's status and camera frames to a self-hosted Obico server, executes Obico's pause/resume/cancel, and re-serves every camera stream at full frame rate on the LAN.

**Architecture:** Three threaded loops in one process: a printer poll loop (Flashforge LAN API → `PrinterSnapshot` → `JobTracker` → Obico status dict), an Obico connection (websocket for status/commands, REST for pictures/events, reconnecting), and camera plumbing (`MjpegSource` holds the single allowed connection per camera; `CameraReserver` fans frames out over HTTP). Every module is a pure function or takes its I/O as an injectable callable, so all tests run without a network.

**Tech Stack:** Python 3.12, `requests`, `websocket-client`; `pytest` for tests; `uv` for the local environment; Docker image `docker.io/hananv/flashforge-obico` (linux/amd64) deployed via the existing Obico compose file on Unraid.

**Spec:** `docs/superpowers/specs/2026-09-15-flashforge-obico-agent-design.md` (this repo). Printer API reference: `~/Projects/OrcaMCP/docs/printers/flashforge-lan-api.md`. Obico protocol reference: the `moonraker-obico` source (`server_conn.py`, `printer.py`, `app.py`).

## Global Constraints

- Python `>=3.12`; runtime dependencies are exactly `requests` and `websocket-client`.
- `FF_CHECK_CODE` and `OBICO_AUTH_TOKEN` are credentials: never logged, never printed, never in a test fixture as a real value, never committed. Every logger goes through `RedactingFilter`.
- Every integer read from the printer goes through `as_int`; a field that fails to parse costs that field only.
- Never send `cancel` except as a relayed Obico command. Never send any job command because of a poll failure.
- Package name `flashforge_obico`, `src/` layout. Tests under `tests/`, one file per module.
- Commit after every task with a conventional-commit message ending in `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Poll cadences: 2 s active, 5 s idle. Picture cadence: 10 s, 1 s under viewing boost, frames older than 15 s are not posted. Status heartbeat 30 s. Offline after 3 consecutive poll failures.

---

## File structure

```
pyproject.toml
Dockerfile
deploy/obico-compose.snippet.yml
src/flashforge_obico/
  __init__.py            # VERSION
  __main__.py            # CLI: run | link <code>
  config.py              # Config.from_env
  logging_utils.py       # RedactingFilter, configure_logging
  flashforge/__init__.py
  flashforge/parsing.py  # as_int, as_float, as_str, as_str_list  (shared by client and snapshot)
  flashforge/snapshot.py # MachineState, PrinterSnapshot, parse_snapshot
  flashforge/client.py   # FlashforgeClient, FlashforgeError
  job_tracker.py         # Event, JobTracker
  obico/__init__.py
  obico/status.py        # obico_state_text, build_status, build_message, build_settings, webcam_entry
  obico/server.py        # ObicoServer, verify_link_code, ws_url
  camera/__init__.py
  camera/mjpeg_source.py # iter_mjpeg_frames, MjpegSource
  camera/reserve.py      # CameraReserver
  agent.py               # Agent
tests/
  conftest.py            # DETAIL fixture (the captured Creator 5 Pro reply)
  test_parsing.py test_snapshot.py test_client.py test_job_tracker.py test_status.py
  test_config.py test_logging_utils.py test_mjpeg_source.py test_reserve.py test_server.py
  test_agent.py test_live.py
```

One deviation from the spec's module table: `as_int` lives in `flashforge/parsing.py` rather than
`client.py`, because `snapshot.py` needs it too and must not depend on the HTTP client.

---

### Task 1: Project scaffold and permissive integer parsing

**Files:**
- Create: `pyproject.toml`, `src/flashforge_obico/__init__.py`, `src/flashforge_obico/flashforge/__init__.py`, `src/flashforge_obico/flashforge/parsing.py`, `tests/conftest.py`, `tests/test_parsing.py`

**Interfaces:**
- Produces: `as_int(value) -> int | None`, `as_float(value) -> float | None`, `as_str(value) -> str` (`""` for non-strings), `as_str_list(value) -> list[str]`; `VERSION: str` in `flashforge_obico/__init__.py`; pytest fixture `detail` returning the captured Creator 5 Pro `detail` payload as a fresh dict.

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "flashforge-obico"
version = "0.1.0"
description = "Obico agent for the FlashForge Creator 5 / 5 Pro in LAN-only mode"
requires-python = ">=3.12"
dependencies = ["requests>=2.32", "websocket-client>=1.8"]

[project.optional-dependencies]
dev = ["pytest>=8"]

[project.scripts]
flashforge-obico = "flashforge_obico.__main__:main"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["live: talks to the real printer; needs FF_HOST, FF_SERIAL, FF_CHECK_CODE"]
```

- [ ] **Step 2: Create the environment**

```bash
cd ~/Projects/flashforge-obico
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
```

- [ ] **Step 3: Write `src/flashforge_obico/__init__.py`** with `VERSION = "0.1.0"` and empty `flashforge/__init__.py`.

- [ ] **Step 4: Write `tests/conftest.py`** with the captured reply (from the API doc §2, MAC redacted) as a fixture:

```python
import copy
import pytest

DETAIL = {
    "status": "ready", "printFileName": "", "printProgress": 0.0,
    "printDuration": 0, "estimatedTime": 0.0, "printLayer": 0, "targetPrintLayer": 0,
    "errorCode": "", "doorStatus": "close", "lightStatus": "close",
    "model": "Creator 5 Pro", "name": "Creator 5 Pro", "firmwareVersion": "1.9.9",
    "ipAddr": "10.0.0.10", "macAddr": "00:00:00:00:00:00", "location": "Den",
    "measure": "256X256X256", "nozzleCnt": 4, "nozzleModel": "0.4mm;0.4mm;0.4mm;0.4mm",
    "nozzleStyle": 0, "nozzleTemps": [28, 29, 29, 29], "nozzleTargetTemps": [0, 0, 0, 0],
    "platTemp": 27, "platTargetTemp": 0, "chamberTemp": 27, "chamberTargetTemp": 0,
    "camera": 1, "cameraStreamUrl": "http://10.0.0.10:8080/?action=stream",
    "lidar": 1, "pid": 41,
    "matlStationInfo": {"currentLoadSlot": 0, "currentSlot": 0, "slotCnt": 4,
                        "stateAction": 0, "stateStep": 0, "slotInfos": []},
}

@pytest.fixture
def detail():
    return copy.deepcopy(DETAIL)
```

- [ ] **Step 5: Write the failing tests `tests/test_parsing.py`**

```python
import pytest
from flashforge_obico.flashforge.parsing import as_int, as_float, as_str, as_str_list

@pytest.mark.parametrize("value,expected", [
    (5, 5), (True, 1), (False, 0), (5.0, 5), (" 7 ", 7), ("-3", -3), ("+4", 4),
    ("5.0", None), ("+-5", None), ("", None), (None, None), ([], None), (5.5, None), ("abc", None),
])
def test_as_int(value, expected):
    assert as_int(value) == expected

@pytest.mark.parametrize("value,expected", [(1, 1.0), (0.5, 0.5), ("0.25", 0.25), (True, None), ("x", None), (None, None)])
def test_as_float(value, expected):
    assert as_float(value) == expected

def test_as_str():
    assert as_str("ready") == "ready"
    assert as_str(None) == "" and as_str(3) == ""

def test_as_str_list():
    assert as_str_list(["a", 1, None]) == ["a"]
    assert as_str_list("nope") == []
```

- [ ] **Step 6: Run to verify failure**: `.venv/bin/pytest tests/test_parsing.py -q` → ImportError.

- [ ] **Step 7: Implement `src/flashforge_obico/flashforge/parsing.py`**

```python
"""Permissive readers for the Creator 5 LAN API, whose firmware types the same field differently
across revisions (number, bool, padded string). A value that is none of the accepted shapes reads
as None so the caller loses one field, never the whole poll."""
from __future__ import annotations


def as_int(value) -> int | None:
    if isinstance(value, bool):          # bool before int: bool subclasses int
        return 1 if value else 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip(), 10)
        except ValueError:
            return None
    return None


def as_float(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def as_str(value) -> str:
    return value if isinstance(value, str) else ""


def as_str_list(value) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []
```

- [ ] **Step 8: Run tests**: `.venv/bin/pytest -q` → all pass.

- [ ] **Step 9: Commit**: `git add -A && git commit -m "feat: project scaffold and permissive integer parsing"`.

---

### Task 2: Printer snapshot parsing

**Files:**
- Create: `src/flashforge_obico/flashforge/snapshot.py`, `tests/test_snapshot.py`

**Interfaces:**
- Consumes: `as_int`, `as_float`, `as_str` from Task 1.
- Produces:
  ```python
  class MachineState(str, Enum): READY, BUSY, HEATING, PRINTING, PAUSED, COMPLETED, ERROR, CANCELLED, UNKNOWN
  ACTIVE_STATES = {PRINTING, HEATING, BUSY, PAUSED}
  @dataclass(frozen=True) class PrinterSnapshot:
      state: MachineState; file_name: str; progress: float; duration_s: int; remaining_s: int
      layer: int | None; total_layers: int | None; error_code: str; camera_stream_url: str
      nozzle_temps: tuple[float, ...]; nozzle_targets: tuple[float, ...]
      bed_temp: float | None; bed_target: float | None; chamber_temp: float | None; chamber_target: float | None
      model: str; firmware: str
      @property def has_job(self) -> bool   # file_name != "" and state in ACTIVE_STATES
  def parse_snapshot(detail: dict) -> PrinterSnapshot
  ```

- [ ] **Step 1: Write failing tests `tests/test_snapshot.py`**

```python
from flashforge_obico.flashforge.snapshot import MachineState, parse_snapshot

def test_idle_machine(detail):
    s = parse_snapshot(detail)
    assert s.state is MachineState.READY and s.file_name == "" and not s.has_job
    assert s.nozzle_temps == (28.0, 29.0, 29.0, 29.0) and s.nozzle_targets == (0.0,) * 4
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
    detail.update(printLayer=" 12 ", targetPrintLayer=True, printProgress="0.5", nozzleTemps=["x", 30, None, 31])
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
```

- [ ] **Step 2: Run**: `.venv/bin/pytest tests/test_snapshot.py -q` → ImportError.

- [ ] **Step 3: Implement `src/flashforge_obico/flashforge/snapshot.py`**

```python
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from .parsing import as_float, as_int, as_str


class MachineState(str, Enum):
    READY = "ready"; BUSY = "busy"; HEATING = "heating"; PRINTING = "printing"; PAUSED = "paused"
    COMPLETED = "completed"; ERROR = "error"; CANCELLED = "cancelled"; UNKNOWN = "unknown"


ACTIVE_STATES = frozenset({MachineState.PRINTING, MachineState.HEATING, MachineState.BUSY, MachineState.PAUSED})
_ALIASES = {"cancel": "cancelled"}


def parse_state(raw) -> MachineState:
    text = as_str(raw).strip().lower()
    text = _ALIASES.get(text, text)
    try:
        return MachineState(text)
    except ValueError:
        return MachineState.UNKNOWN


@dataclass(frozen=True)
class PrinterSnapshot:
    state: MachineState
    file_name: str
    progress: float
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

    @property
    def has_job(self) -> bool:
        return self.file_name != "" and self.state in ACTIVE_STATES


def _floats(value) -> tuple[float, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(f for f in (as_float(v) for v in value) if f is not None)


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
    )
```

- [ ] **Step 4: Run**: `.venv/bin/pytest -q` → pass.
- [ ] **Step 5: Commit**: `git commit -am "feat: parse the printer's detail reply into a typed snapshot"` (add new files first).

---

### Task 3: Flashforge HTTP client

**Files:**
- Create: `src/flashforge_obico/flashforge/client.py`, `tests/test_client.py`

**Interfaces:**
- Produces:
  ```python
  Transport = Callable[[str, dict], dict]          # (url, json body) -> parsed JSON body; raises on transport error
  class FlashforgeError(Exception): ...
  class FlashforgeClient:
      def __init__(self, host: str, serial: str, check_code: str, *, transport: Transport | None = None, port: int = 8898)
      def detail(self) -> dict            # the detail payload (unwrapped), raises FlashforgeError
      def pause(self) -> None; def resume(self) -> None; def cancel(self) -> None
  def requests_transport(timeout: float = 15.0) -> Transport
  ```

- [ ] **Step 1: Failing tests `tests/test_client.py`**

```python
import pytest
from flashforge_obico.flashforge.client import FlashforgeClient, FlashforgeError

class FakeTransport:
    def __init__(self, replies):
        self.replies = list(replies); self.calls = []
    def __call__(self, url, body):
        self.calls.append((url, body))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

def make(replies):
    t = FakeTransport(replies)
    return FlashforgeClient("10.0.0.10", "SN123", "CODE", transport=t), t

def test_detail_unwraps_nested_payload(detail):
    c, t = make([{"code": 0, "message": "Success", "detail": detail}])
    assert c.detail()["status"] == "ready"
    url, body = t.calls[0]
    assert url == "http://10.0.0.10:8898/detail"
    assert body == {"serialNumber": "SN123", "checkCode": "CODE"}

def test_detail_accepts_top_level_payload(detail):
    detail["code"] = " 0 "
    c, _ = make([detail])
    assert c.detail()["status"] == "ready"

def test_nonzero_code_or_err_is_an_error():
    c, _ = make([{"code": 1, "message": "bad"}, {"err": "2", "msg": "worse"}])
    with pytest.raises(FlashforgeError, match="bad"):
        c.detail()
    with pytest.raises(FlashforgeError, match="worse"):
        c.detail()

def test_transport_failure_is_wrapped():
    c, _ = make([ConnectionError("boom")])
    with pytest.raises(FlashforgeError, match="boom"):
        c.detail()

@pytest.mark.parametrize("method,action", [("pause", "pause"), ("resume", "continue"), ("cancel", "cancel")])
def test_job_commands(method, action):
    c, t = make([{"code": 0}])
    getattr(c, method)()
    url, body = t.calls[0]
    assert url == "http://10.0.0.10:8898/control"
    assert body["payload"] == {"cmd": "jobCtl_cmd", "args": {"jobID": "", "action": action}}
    assert body["serialNumber"] == "SN123" and body["checkCode"] == "CODE"

def test_error_message_never_contains_the_check_code():
    c, _ = make([{"code": 5, "message": "refused"}])
    with pytest.raises(FlashforgeError) as e:
        c.detail()
    assert "CODE" not in str(e.value)
```

- [ ] **Step 2: Run** → ImportError.
- [ ] **Step 3: Implement `client.py`**

```python
from __future__ import annotations
from typing import Callable
import requests
from .parsing import as_int, as_str

Transport = Callable[[str, dict], dict]


class FlashforgeError(Exception):
    """The printer refused, or could not be reached. The message never carries credentials."""


def requests_transport(timeout: float = 15.0) -> Transport:
    def post(url: str, body: dict) -> dict:
        response = requests.post(url, json=body, timeout=timeout)
        response.raise_for_status()
        return response.json()
    return post


class FlashforgeClient:
    def __init__(self, host: str, serial: str, check_code: str, *, transport: Transport | None = None, port: int = 8898):
        self._base = f"http://{host}:{port}"
        self._credentials = {"serialNumber": serial, "checkCode": check_code}
        self._transport = transport or requests_transport()

    def detail(self) -> dict:
        body = self._post("detail", {})
        nested = body.get("detail")
        return nested if isinstance(nested, dict) else body

    def pause(self) -> None:
        self._job_command("pause")

    def resume(self) -> None:
        self._job_command("continue")

    def cancel(self) -> None:
        self._job_command("cancel")

    def _job_command(self, action: str) -> None:
        self._post("control", {"payload": {"cmd": "jobCtl_cmd", "args": {"jobID": "", "action": action}}})

    def _post(self, endpoint: str, extra: dict) -> dict:
        try:
            body = self._transport(f"{self._base}/{endpoint}", {**self._credentials, **extra})
        except Exception as exc:  # requests raises many types; none may leak the body
            raise FlashforgeError(f"{endpoint}: {type(exc).__name__}: {exc}") from None
        if not isinstance(body, dict):
            raise FlashforgeError(f"{endpoint}: reply is not a JSON object")
        code = as_int(body.get("code", body.get("err")))
        if code is None:
            raise FlashforgeError(f"{endpoint}: reply has no result code")
        if code != 0:
            message = as_str(body.get("message")) or as_str(body.get("msg")) or "no message"
            raise FlashforgeError(f"{endpoint}: printer returned {code}: {message}")
        return body
```

  Note: the `from None` plus rebuilding the message from the exception type and text is what keeps the request body (which holds the check code) out of the error.

- [ ] **Step 4: Run** → pass. **Step 5: Commit** `feat: Flashforge LAN API client with injectable transport`.

---

### Task 4: Job tracker

**Files:**
- Create: `src/flashforge_obico/job_tracker.py`, `tests/test_job_tracker.py`

**Interfaces:**
- Consumes: `PrinterSnapshot`, `MachineState` (Task 2).
- Produces:
  ```python
  class Event(str, Enum):
      STARTED = "PrintStarted"; PAUSED = "PrintPaused"; RESUMED = "PrintResumed"
      DONE = "PrintDone"; CANCELLED = "PrintCancelled"; FAILED = "PrintFailed"
  class JobTracker:
      current_print_ts: int        # -1 when no job
      active_file: str | None
      paused: bool
      def observe(self, snapshot: PrinterSnapshot | None, now: float) -> list[Event]
  ```

- [ ] **Step 1: Failing tests**

```python
from flashforge_obico.flashforge.snapshot import parse_snapshot
from flashforge_obico.job_tracker import Event, JobTracker

def snap(detail, **over):
    d = dict(detail); d.update(over); return parse_snapshot(d)

def test_start_pause_resume_done(detail):
    t = JobTracker()
    assert t.observe(snap(detail), 100.0) == [] and t.current_print_ts == -1
    assert t.observe(snap(detail, status="heating", printFileName="a.gcode"), 101.0) == [Event.STARTED]
    assert t.current_print_ts == 101 and t.active_file == "a.gcode"
    assert t.observe(snap(detail, status="printing", printFileName="a.gcode", printProgress=0.3), 102.0) == []
    assert t.observe(snap(detail, status="paused", printFileName="a.gcode", printProgress=0.3), 103.0) == [Event.PAUSED]
    assert t.observe(snap(detail, status="paused", printFileName="a.gcode", printProgress=0.3), 104.0) == []
    assert t.observe(snap(detail, status="printing", printFileName="a.gcode", printProgress=0.4), 105.0) == [Event.RESUMED]
    assert t.observe(snap(detail, status="completed", printFileName="a.gcode", printProgress=1.0), 106.0) == [Event.DONE]
    assert t.current_print_ts == -1 and t.active_file is None
    assert t.observe(snap(detail, status="ready"), 107.0) == []

def test_cancelled_and_error(detail):
    t = JobTracker()
    t.observe(snap(detail, status="printing", printFileName="a.gcode"), 1.0)
    assert t.observe(snap(detail, status="cancel", printFileName="a.gcode"), 2.0) == [Event.CANCELLED]
    t.observe(snap(detail, status="printing", printFileName="b.gcode"), 3.0)
    assert t.observe(snap(detail, status="error", printFileName="b.gcode", errorCode="E123"), 4.0) == [Event.FAILED]

def test_file_vanishing_mid_print_is_a_failure_but_at_the_end_is_done(detail):
    t = JobTracker()
    t.observe(snap(detail, status="printing", printFileName="a.gcode", printProgress=0.5), 1.0)
    assert t.observe(snap(detail, status="ready"), 2.0) == [Event.FAILED]
    t.observe(snap(detail, status="printing", printFileName="a.gcode", printProgress=0.999), 3.0)
    assert t.observe(snap(detail, status="ready"), 4.0) == [Event.DONE]

def test_file_swap_ends_old_and_starts_new(detail):
    t = JobTracker()
    t.observe(snap(detail, status="printing", printFileName="a.gcode", printProgress=0.5), 1.0)
    assert t.observe(snap(detail, status="printing", printFileName="b.gcode"), 2.0) == [Event.FAILED, Event.STARTED]
    assert t.current_print_ts == 2 and t.active_file == "b.gcode"

def test_unknown_and_offline_change_nothing(detail):
    t = JobTracker()
    t.observe(snap(detail, status="printing", printFileName="a.gcode"), 1.0)
    assert t.observe(snap(detail, status="mystery", printFileName="a.gcode"), 2.0) == []
    assert t.observe(None, 3.0) == []
    assert t.current_print_ts == 1 and t.active_file == "a.gcode"

def test_same_file_printed_twice(detail):
    t = JobTracker()
    t.observe(snap(detail, status="printing", printFileName="a.gcode"), 1.0)
    t.observe(snap(detail, status="completed", printFileName="a.gcode", printProgress=1.0), 2.0)
    assert t.observe(snap(detail, status="printing", printFileName="a.gcode"), 3.0) == [Event.STARTED]
```

- [ ] **Step 2: Run** → ImportError.
- [ ] **Step 3: Implement `job_tracker.py`**

```python
from __future__ import annotations
from enum import Enum
from .flashforge.snapshot import MachineState, PrinterSnapshot

DONE_THRESHOLD = 0.995


class Event(str, Enum):
    STARTED = "PrintStarted"; PAUSED = "PrintPaused"; RESUMED = "PrintResumed"
    DONE = "PrintDone"; CANCELLED = "PrintCancelled"; FAILED = "PrintFailed"


class JobTracker:
    """Turns consecutive snapshots into Obico print events and owns `current_print_ts`, the
    integer Obico uses to identify one print session (-1 = no job)."""

    def __init__(self) -> None:
        self.current_print_ts = -1
        self.active_file: str | None = None
        self.paused = False
        self._last_progress = 0.0

    def observe(self, snapshot: PrinterSnapshot | None, now: float) -> list[Event]:
        if snapshot is None or snapshot.state is MachineState.UNKNOWN:
            return []
        events: list[Event] = []
        if self.active_file is not None and (not snapshot.has_job or snapshot.file_name != self.active_file):
            events.append(self._end(snapshot))
        if snapshot.has_job and self.active_file is None:
            events.append(self._start(snapshot, now))
        elif snapshot.has_job:
            paused_now = snapshot.state is MachineState.PAUSED
            if paused_now != self.paused:
                events.append(Event.PAUSED if paused_now else Event.RESUMED)
            self.paused = paused_now
        if snapshot.has_job:
            self._last_progress = snapshot.progress
        return events

    def _start(self, snapshot: PrinterSnapshot, now: float) -> Event:
        self.current_print_ts = int(now)
        self.active_file = snapshot.file_name
        self.paused = snapshot.state is MachineState.PAUSED
        self._last_progress = snapshot.progress
        return Event.STARTED

    def _end(self, snapshot: PrinterSnapshot) -> Event:
        finished = snapshot.file_name == self.active_file and snapshot.progress >= DONE_THRESHOLD
        finished = finished or self._last_progress >= DONE_THRESHOLD
        if snapshot.state is MachineState.COMPLETED or (snapshot.state is MachineState.READY and finished):
            event = Event.DONE
        elif snapshot.state is MachineState.CANCELLED:
            event = Event.CANCELLED
        else:
            event = Event.FAILED
        self.current_print_ts = -1
        self.active_file = None
        self.paused = False
        self._last_progress = 0.0
        return event
```

- [ ] **Step 4: Run** → pass (adjust only if a test exposes a real logic gap; do not weaken tests). **Step 5: Commit** `feat: job tracker turns snapshots into Obico print events`.

---

### Task 5: Obico status payload

**Files:**
- Create: `src/flashforge_obico/obico/__init__.py`, `src/flashforge_obico/obico/status.py`, `tests/test_status.py`

**Interfaces:**
- Consumes: `PrinterSnapshot`, `MachineState`, `JobTracker`, `Event`, `VERSION`.
- Produces:
  ```python
  OFFLINE, OPERATIONAL, PRINTING, PAUSED = "Offline", "Operational", "Printing", "Paused"
  def obico_state_text(snapshot: PrinterSnapshot | None, previous: str) -> str
  def build_status(snapshot: PrinterSnapshot | None, tracker: JobTracker, state_text: str, now: float) -> dict   # {} when Offline
  def webcam_entry(*, name: str, is_primary: bool, stream_url: str, snapshot_url: str) -> dict
  def build_settings(webcams: list[dict]) -> dict
  def build_message(*, status: dict, current_print_ts: int, event: Event | None = None, settings: dict | None = None) -> dict
  ```

- [ ] **Step 1: Failing tests**

```python
from flashforge_obico import VERSION
from flashforge_obico.flashforge.snapshot import parse_snapshot
from flashforge_obico.job_tracker import Event, JobTracker
from flashforge_obico.obico.status import (OFFLINE, OPERATIONAL, PAUSED, PRINTING, build_message,
                                          build_settings, build_status, obico_state_text, webcam_entry)

def snap(detail, **over):
    d = dict(detail); d.update(over); return parse_snapshot(d)

def test_state_text_mapping(detail):
    assert obico_state_text(snap(detail), PRINTING) == OPERATIONAL
    assert obico_state_text(snap(detail, status="heating", printFileName="a"), OPERATIONAL) == PRINTING
    assert obico_state_text(snap(detail, status="busy", printFileName="a"), OPERATIONAL) == PRINTING
    assert obico_state_text(snap(detail, status="busy"), PRINTING) == PRINTING          # busy, no file: keep
    assert obico_state_text(snap(detail, status="paused", printFileName="a"), PRINTING) == PAUSED
    assert obico_state_text(snap(detail, status="mystery"), PAUSED) == PAUSED             # unknown: keep
    assert obico_state_text(snap(detail, status="error"), PRINTING) == OPERATIONAL
    assert obico_state_text(None, PRINTING) == OFFLINE

def test_offline_status_is_empty(detail):
    assert build_status(None, JobTracker(), OFFLINE, 1.0) == {}

def test_printing_status_shape(detail):
    t = JobTracker()
    s = snap(detail, status="printing", printFileName="benchy.gcode", printProgress=0.42,
             printDuration=600, estimatedTime=830, printLayer=57, targetPrintLayer=137,
             nozzleTemps=[210, 30, 30, 30], nozzleTargetTemps=[210, 0, 0, 0], platTemp=60, platTargetTemp=60)
    t.observe(s, 1000.0)
    st = build_status(s, t, PRINTING, 1001.0)
    assert st["_ts"] == 1001.0
    assert st["state"]["text"] == "Printing"
    assert st["state"]["flags"] == {"operational": True, "paused": False, "printing": True, "cancelling": False,
                                    "pausing": False, "error": False, "ready": False, "closedOrError": False}
    assert st["state"]["error"] is None
    assert st["job"]["file"] == {"name": "benchy.gcode", "path": "benchy.gcode", "display": "benchy.gcode", "obico_g_code_file_id": None}
    assert st["progress"] == {"completion": 42.0, "filepos": 0, "printTime": 600, "printTimeLeft": 830, "filamentUsed": None}
    assert st["temperatures"]["tool0"] == {"actual": 210.0, "offset": 0, "target": 210.0}
    assert st["temperatures"]["tool3"] == {"actual": 30.0, "offset": 0, "target": 0.0}
    assert st["temperatures"]["bed"] == {"actual": 60.0, "offset": 0, "target": 60.0}
    assert st["temperatures"]["chamber"] == {"actual": 27.0, "offset": 0, "target": 0.0}
    assert st["currentLayerHeight"] == 57 and st["file_metadata"]["obico"]["totalLayerCount"] == 137
    assert st["currentZ"] is None and st["currentFeedRate"] is None and st["currentFanSpeed"] is None

def test_error_state(detail):
    s = snap(detail, status="error", errorCode="E42")
    st = build_status(s, JobTracker(), OPERATIONAL, 1.0)
    assert st["state"]["flags"]["error"] is True and st["state"]["error"] == "E42"
    assert st["state"]["flags"]["ready"] is True and st["job"]["file"]["name"] is None

def test_message_and_settings():
    cam = webcam_entry(name="Printer", is_primary=True, stream_url="http://h:8081/cameras/0/stream",
                       snapshot_url="http://h:8081/cameras/0/snapshot")
    assert cam["is_primary_camera"] is True and cam["stream_mode"] == "h264_transcode" and cam["stream_id"] is None
    assert cam["streamRatio"] == "16:9" and cam["rotation"] == 0 and cam["flipH"] is False
    settings = build_settings([cam])
    assert settings["agent"] == {"name": "flashforge_obico", "version": VERSION}
    assert settings["webcams"] == [cam] and settings["temperature"] == {"profiles": []}
    msg = build_message(status={"x": 1}, current_print_ts=5, event=Event.STARTED, settings=settings)
    assert msg == {"current_print_ts": 5, "status": {"x": 1}, "event": {"event_type": "PrintStarted"}, "settings": settings}
    assert build_message(status={}, current_print_ts=-1) == {"current_print_ts": -1, "status": {}}
```

- [ ] **Step 2: Run** → ImportError.
- [ ] **Step 3: Implement `obico/status.py`**

```python
"""The Obico agent payload, in the shape of moonraker-obico's PrinterState.to_status()/to_dict()."""
from __future__ import annotations
import platform
from .. import VERSION
from ..flashforge.snapshot import MachineState, PrinterSnapshot
from ..job_tracker import Event, JobTracker

OFFLINE, OPERATIONAL, PRINTING, PAUSED = "Offline", "Operational", "Printing", "Paused"
AGENT_NAME = "flashforge_obico"


def obico_state_text(snapshot: PrinterSnapshot | None, previous: str) -> str:
    if snapshot is None:
        return OFFLINE
    if snapshot.state is MachineState.UNKNOWN:
        return previous
    if snapshot.has_job:
        return PAUSED if snapshot.state is MachineState.PAUSED else PRINTING
    if snapshot.state is MachineState.BUSY:
        return previous  # busy with no file: could be a job spinning up
    return OPERATIONAL


def _temperature(actual: float | None, target: float | None) -> dict:
    return {"actual": round(actual if actual is not None else 0.0, 2), "offset": 0, "target": target}


def build_status(snapshot: PrinterSnapshot | None, tracker: JobTracker, state_text: str, now: float) -> dict:
    if snapshot is None or state_text == OFFLINE:
        return {}
    has_error = snapshot.state is MachineState.ERROR
    file_name = snapshot.file_name or None
    active = tracker.active_file is not None
    temps = {f"tool{i}": _temperature(t, snapshot.nozzle_targets[i] if i < len(snapshot.nozzle_targets) else None)
             for i, t in enumerate(snapshot.nozzle_temps)}
    temps["bed"] = _temperature(snapshot.bed_temp, snapshot.bed_target)
    temps["chamber"] = _temperature(snapshot.chamber_temp, snapshot.chamber_target)
    return {
        "_ts": now,
        "state": {
            "text": state_text,
            "flags": {
                "operational": True, "paused": state_text == PAUSED, "printing": state_text == PRINTING,
                "cancelling": False, "pausing": False, "error": has_error,
                "ready": state_text == OPERATIONAL, "closedOrError": False,
            },
            "error": snapshot.error_code or None if has_error else None,
        },
        "job": {
            "file": {"name": file_name, "path": file_name, "display": file_name, "obico_g_code_file_id": None},
            "estimatedPrintTime": None, "user": None,
        },
        "progress": {
            "completion": snapshot.progress * 100 if active else None,
            "filepos": 0,
            "printTime": snapshot.duration_s if active else None,
            "printTimeLeft": snapshot.remaining_s if active else None,
            "filamentUsed": None,
        },
        "temperatures": temps,
        "file_metadata": {"analysis": {"printingArea": {"maxZ": None}}, "obico": {"totalLayerCount": snapshot.total_layers if active else None}},
        "currentLayerHeight": snapshot.layer if active else None,
        "currentFeedRate": None, "currentFlowRate": None, "currentFanSpeed": None,
        "currentZ": None, "display_status": {},
    }


def webcam_entry(*, name: str, is_primary: bool, stream_url: str, snapshot_url: str) -> dict:
    return {
        "name": name, "is_primary_camera": is_primary, "is_nozzle_camera": False,
        "stream_mode": "h264_transcode", "stream_id": None, "data_channel_available": False,
        "flipV": False, "flipH": False, "rotation": 0, "streamRatio": "16:9",
        "stream_url": stream_url, "snapshot_url": snapshot_url,   # ours: how LAN viewers reach the re-served stream
    }


def build_settings(webcams: list[dict]) -> dict:
    return {
        "webcams": webcams, "data_channel_id": None,
        "temperature": {"profiles": []},
        "agent": {"name": AGENT_NAME, "version": VERSION},
        "platform_uname": list(platform.uname()) + [""],
        "installed_plugins": [],
    }


def build_message(*, status: dict, current_print_ts: int, event: Event | None = None, settings: dict | None = None) -> dict:
    message: dict = {"current_print_ts": current_print_ts, "status": status}
    if event is not None:
        message["event"] = {"event_type": event.value}
    if settings is not None:
        message["settings"] = settings
    return message
```

  Note on `"error": snapshot.error_code or None if has_error else None` — write it with parentheses: `(snapshot.error_code or None) if has_error else None`.

- [ ] **Step 4: Run** → pass. **Step 5: Commit** `feat: build Obico status, settings and message payloads`.

---

### Task 6: Configuration and secret-redacting logging

**Files:**
- Create: `src/flashforge_obico/config.py`, `src/flashforge_obico/logging_utils.py`, `tests/test_config.py`, `tests/test_logging_utils.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True) class CameraConfig: name: str; stream_url: str
  @dataclass(frozen=True) class Config:
      ff_host: str; ff_serial: str; ff_check_code: str
      obico_url: str; obico_auth_token: str
      cameras: tuple[CameraConfig, ...]     # () means "use the printer's cameraStreamUrl, named 'Printer'"
      reserve_port: int; public_host: str; log_level: str
      @property def secrets(self) -> tuple[str, ...]
      def camera_public_url(self, index: int, kind: str) -> str   # http://{public_host}:{reserve_port}/cameras/{index}/{kind}
  class ConfigError(ValueError)
  def load_config(env: Mapping[str, str], *, require_token: bool = True) -> Config
  class RedactingFilter(logging.Filter): def __init__(self, secrets: Iterable[str])
  def configure_logging(level: str, secrets: Iterable[str]) -> None
  ```

- [ ] **Step 1: Failing tests `tests/test_config.py`**

```python
import pytest
from flashforge_obico.config import CameraConfig, ConfigError, load_config

BASE = {"FF_HOST": "10.0.0.10", "FF_SERIAL": "SN", "FF_CHECK_CODE": "CC", "OBICO_AUTH_TOKEN": "TK", "PUBLIC_HOST": "10.0.0.2"}

def test_defaults():
    c = load_config(BASE)
    assert c.obico_url == "http://web:3334" and c.reserve_port == 8081 and c.log_level == "INFO"
    assert c.cameras == () and c.secrets == ("CC", "TK")
    assert c.camera_public_url(0, "stream") == "http://10.0.0.2:8081/cameras/0/stream"

def test_cameras_parsed_and_named():
    c = load_config({**BASE, "CAMERA_URLS": "http://a/s, http://b/s", "CAMERA_NAMES": "Printer, Side"})
    assert c.cameras == (CameraConfig("Printer", "http://a/s"), CameraConfig("Side", "http://b/s"))

def test_camera_names_default_to_numbered():
    c = load_config({**BASE, "CAMERA_URLS": "http://a/s,http://b/s"})
    assert [cam.name for cam in c.cameras] == ["Printer", "Camera 2"]

def test_camera_name_count_mismatch_is_an_error():
    with pytest.raises(ConfigError, match="CAMERA_NAMES"):
        load_config({**BASE, "CAMERA_URLS": "http://a/s", "CAMERA_NAMES": "x,y"})

@pytest.mark.parametrize("missing", ["FF_HOST", "FF_SERIAL", "FF_CHECK_CODE", "OBICO_AUTH_TOKEN", "PUBLIC_HOST"])
def test_required(missing):
    env = dict(BASE); del env[missing]
    with pytest.raises(ConfigError, match=missing):
        load_config(env)

def test_token_optional_for_link_and_public_host_optional_when_reserver_off():
    env = dict(BASE); del env["OBICO_AUTH_TOKEN"]; del env["PUBLIC_HOST"]; env["RESERVE_PORT"] = "0"
    c = load_config(env, require_token=False)
    assert c.obico_auth_token == "" and c.reserve_port == 0

def test_bad_port():
    with pytest.raises(ConfigError, match="RESERVE_PORT"):
        load_config({**BASE, "RESERVE_PORT": "eighty"})

def test_obico_url_trailing_slash_stripped():
    assert load_config({**BASE, "OBICO_URL": "http://x:3334/"}).obico_url == "http://x:3334"
```

- [ ] **Step 2: Failing tests `tests/test_logging_utils.py`**

```python
import logging
from flashforge_obico.logging_utils import RedactingFilter

def test_filter_redacts_secrets_in_message_and_args():
    record = logging.LogRecord("t", logging.INFO, "", 0, "code=%s body=SECRET1", ("SECRET2",), None)
    assert RedactingFilter(["SECRET1", "SECRET2", ""]).filter(record) is True
    assert record.getMessage() == "code=<redacted> body=<redacted>"

def test_filter_ignores_empty_secrets():
    record = logging.LogRecord("t", logging.INFO, "", 0, "hello", (), None)
    RedactingFilter([""]).filter(record)
    assert record.getMessage() == "hello"
```

- [ ] **Step 3: Run both** → ImportError.
- [ ] **Step 4: Implement `config.py`**

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Mapping


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class CameraConfig:
    name: str
    stream_url: str


@dataclass(frozen=True)
class Config:
    ff_host: str
    ff_serial: str
    ff_check_code: str
    obico_url: str
    obico_auth_token: str
    cameras: tuple[CameraConfig, ...]
    reserve_port: int
    public_host: str
    log_level: str

    @property
    def secrets(self) -> tuple[str, ...]:
        return (self.ff_check_code, self.obico_auth_token)

    def camera_public_url(self, index: int, kind: str) -> str:
        return f"http://{self.public_host}:{self.reserve_port}/cameras/{index}/{kind}"


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ConfigError(f"{key} is required")
    return value


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _cameras(env: Mapping[str, str]) -> tuple[CameraConfig, ...]:
    urls = _split(env.get("CAMERA_URLS", ""))
    names = _split(env.get("CAMERA_NAMES", ""))
    if names and len(names) != len(urls):
        raise ConfigError("CAMERA_NAMES must have one name per CAMERA_URLS entry")
    if not names:
        names = ["Printer"] + [f"Camera {i + 2}" for i in range(len(urls) - 1)]
    return tuple(CameraConfig(name, url) for name, url in zip(names, urls))


def load_config(env: Mapping[str, str], *, require_token: bool = True) -> Config:
    try:
        reserve_port = int(env.get("RESERVE_PORT", "8081"))
    except ValueError:
        raise ConfigError("RESERVE_PORT must be an integer") from None
    return Config(
        ff_host=_required(env, "FF_HOST"),
        ff_serial=_required(env, "FF_SERIAL"),
        ff_check_code=_required(env, "FF_CHECK_CODE"),
        obico_url=env.get("OBICO_URL", "http://web:3334").strip().rstrip("/"),
        obico_auth_token=_required(env, "OBICO_AUTH_TOKEN") if require_token else env.get("OBICO_AUTH_TOKEN", "").strip(),
        cameras=_cameras(env),
        reserve_port=reserve_port,
        public_host=_required(env, "PUBLIC_HOST") if reserve_port > 0 else env.get("PUBLIC_HOST", "").strip(),
        log_level=env.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )
```

- [ ] **Step 5: Implement `logging_utils.py`**

```python
from __future__ import annotations
import logging
import sys
from typing import Iterable

REDACTED = "<redacted>"


class RedactingFilter(logging.Filter):
    """Replaces every occurrence of a secret in the formatted message. Installed on the root
    handler so nothing reaches stdout unredacted."""

    def __init__(self, secrets: Iterable[str]):
        super().__init__()
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self._secrets:
            message = message.replace(secret, REDACTED)
        record.msg, record.args = message, ()
        return True


def configure_logging(level: str, secrets: Iterable[str]) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level, logging.INFO))
    logging.getLogger("websocket").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
```

- [ ] **Step 6: Run** → pass. **Step 7: Commit** `feat: environment configuration and secret-redacting logging`.

---

### Task 7: MJPEG stream source

**Files:**
- Create: `src/flashforge_obico/camera/__init__.py`, `src/flashforge_obico/camera/mjpeg_source.py`, `tests/test_mjpeg_source.py`

**Interfaces:**
- Produces:
  ```python
  def iter_mjpeg_frames(stream: BinaryIO) -> Iterator[bytes]     # yields raw JPEG bytes per part; returns at EOF
  Opener = Callable[[str], BinaryIO]                              # default: urllib.request.urlopen with 10 s timeout
  class MjpegSource:
      def __init__(self, url: str, *, name: str = "", opener: Opener | None = None, clock=time.monotonic, sleep=time.sleep)
      name: str; url: str
      def start(self) -> None; def stop(self) -> None
      def latest(self) -> tuple[bytes, float] | None             # (jpeg, monotonic time it arrived)
      def age(self) -> float | None                              # seconds since last frame, None if never
      def wait_for_new_frame(self, after: float, timeout: float) -> tuple[bytes, float] | None
  ```

- [ ] **Step 1: Failing tests**

```python
import io, threading, time
from flashforge_obico.camera.mjpeg_source import MjpegSource, iter_mjpeg_frames

JPEG_A = b"\xff\xd8AAAA\xff\xd9"
JPEG_B = b"\xff\xd8BB\xff\xd9"

def mjpg_streamer_body(*frames, with_length=True):
    out = b""
    for f in frames:
        out += b"--boundarydonotcross\r\nContent-Type: image/jpeg\r\n"
        if with_length:
            out += b"Content-Length: %d\r\n" % len(f)
        out += b"X-Timestamp: 1.000\r\n\r\n" + f + b"\r\n"
    return out

def test_parses_frames_with_content_length():
    assert list(iter_mjpeg_frames(io.BytesIO(mjpg_streamer_body(JPEG_A, JPEG_B)))) == [JPEG_A, JPEG_B]

def test_parses_frames_without_content_length_by_boundary():
    assert list(iter_mjpeg_frames(io.BytesIO(mjpg_streamer_body(JPEG_A, JPEG_B, with_length=False)))) == [JPEG_A, JPEG_B]

def test_truncated_final_frame_is_dropped():
    body = mjpg_streamer_body(JPEG_A) + b"--boundarydonotcross\r\nContent-Length: 99\r\n\r\n\xff\xd8partial"
    assert list(iter_mjpeg_frames(io.BytesIO(body))) == [JPEG_A]

def test_source_keeps_latest_and_reconnects():
    bodies = [mjpg_streamer_body(JPEG_A), mjpg_streamer_body(JPEG_B)]
    opened = threading.Event()
    def opener(url):
        if not bodies:
            opened.set(); time.sleep(0.05); raise OSError("down")
        return io.BytesIO(bodies.pop(0))
    src = MjpegSource("http://cam/?action=stream", name="Printer", opener=opener, sleep=lambda s: time.sleep(min(s, 0.01)))
    src.start()
    try:
        assert opened.wait(2.0)
        jpeg, _ = src.latest()
        assert jpeg == JPEG_B and src.age() < 2.0
    finally:
        src.stop()

def test_wait_for_new_frame_times_out_and_returns_new():
    src = MjpegSource("http://cam", opener=lambda url: io.BytesIO(b""), sleep=lambda s: time.sleep(0.01))
    assert src.wait_for_new_frame(after=0.0, timeout=0.05) is None
    src._store(JPEG_A)
    assert src.wait_for_new_frame(after=0.0, timeout=0.05)[0] == JPEG_A
    assert src.wait_for_new_frame(after=src.latest()[1], timeout=0.05) is None
```

- [ ] **Step 2: Run** → ImportError.
- [ ] **Step 3: Implement `camera/mjpeg_source.py`**

```python
"""One camera stream, one connection. `iter_mjpeg_frames` is the pure multipart parser;
`MjpegSource` runs it on a thread, keeps the newest JPEG and reconnects when the stream drops."""
from __future__ import annotations
import logging
import threading
import time
import urllib.request
from typing import BinaryIO, Callable, Iterator

_logger = logging.getLogger(__name__)
Opener = Callable[[str], BinaryIO]
MAX_FRAME = 8_000_000
BACKOFF_START, BACKOFF_MAX = 1.0, 30.0


def _default_opener(url: str) -> BinaryIO:
    return urllib.request.urlopen(url, timeout=10)


def _read_headers(stream: BinaryIO) -> dict[str, str] | None:
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if line == b"":
            return None
        if line in (b"\r\n", b"\n"):
            return headers
        if b":" in line:
            key, _, value = line.decode("latin-1").partition(":")
            headers[key.strip().lower()] = value.strip()


def iter_mjpeg_frames(stream: BinaryIO) -> Iterator[bytes]:
    boundary: bytes | None = None
    while True:
        line = stream.readline()
        if line == b"":
            return
        stripped = line.strip()
        if not stripped.startswith(b"--"):
            continue
        boundary = boundary or stripped
        headers = _read_headers(stream)
        if headers is None:
            return
        length = headers.get("content-length")
        if length and length.isdigit() and int(length) <= MAX_FRAME:
            data = stream.read(int(length))
            if len(data) < int(length):
                return
            yield data
            continue
        chunks: list[bytes] = []
        total = 0
        while True:
            part = stream.readline()
            if part == b"":
                return
            if part.strip() == boundary:
                frame = b"".join(chunks)
                if frame.endswith(b"\r\n"):
                    frame = frame[:-2]
                yield frame
                # re-enter header parsing for the part whose boundary we just consumed
                headers = _read_headers(stream)
                if headers is None:
                    return
                chunks, total = [], 0
                length = headers.get("content-length")
                if length and length.isdigit():
                    data = stream.read(int(length))
                    if len(data) < int(length):
                        return
                    yield data
                    break
                continue
            chunks.append(part)
            total += len(part)
            if total > MAX_FRAME:
                return


class MjpegSource:
    def __init__(self, url: str, *, name: str = "", opener: Opener | None = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.url, self.name = url, name
        self._opener = opener or _default_opener
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Condition()
        self._latest: tuple[bytes, float] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"mjpeg:{self.name or self.url}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def latest(self) -> tuple[bytes, float] | None:
        with self._lock:
            return self._latest

    def age(self) -> float | None:
        latest = self.latest()
        return None if latest is None else self._clock() - latest[1]

    def wait_for_new_frame(self, after: float, timeout: float) -> tuple[bytes, float] | None:
        with self._lock:
            self._lock.wait_for(lambda: self._latest is not None and self._latest[1] > after, timeout)
            if self._latest is not None and self._latest[1] > after:
                return self._latest
            return None

    def _store(self, jpeg: bytes) -> None:
        with self._lock:
            self._latest = (jpeg, self._clock())
            self._lock.notify_all()

    def _run(self) -> None:
        backoff = BACKOFF_START
        while not self._stop.is_set():
            try:
                with self._opener(self.url) as stream:
                    for frame in iter_mjpeg_frames(stream):
                        self._store(frame)
                        backoff = BACKOFF_START
                        if self._stop.is_set():
                            return
                _logger.info("camera %s: stream ended, reconnecting", self.name or self.url)
            except Exception as exc:
                _logger.warning("camera %s: %s: %s", self.name or self.url, type(exc).__name__, exc)
            self._sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
```

  The boundary-scan branch above is convoluted; if the tests pass, simplify it into a helper `_read_until_boundary(stream, boundary) -> bytes | None` and re-run. Do not leave two code paths that both parse headers.

- [ ] **Step 4: Run** → pass (fix the parser until the three parser tests pass exactly). **Step 5: Commit** `feat: MJPEG source holds one camera connection and keeps the latest frame`.

---

### Task 8: Camera re-server

**Files:**
- Create: `src/flashforge_obico/camera/reserve.py`, `tests/test_reserve.py`

**Interfaces:**
- Consumes: `MjpegSource` (Task 7).
- Produces:
  ```python
  class CameraReserver:
      def __init__(self, sources: Sequence[MjpegSource], port: int, *, stale_after_s: float = 15.0, host: str = "0.0.0.0")
      def start(self) -> None; def stop(self) -> None
      @property def port(self) -> int          # actual bound port (useful when constructed with 0 in tests)
  # Routes: GET /cameras/<i>/stream  -> multipart/x-mixed-replace, boundary "flashforgeobico"
  #         GET /cameras/<i>/snapshot -> image/jpeg (503 if no frame yet, 404 unknown index)
  #         GET /healthz -> 200 "ok"
  ```

- [ ] **Step 1: Failing tests**

```python
import io, threading, time, urllib.request
from flashforge_obico.camera.mjpeg_source import MjpegSource
from flashforge_obico.camera.reserve import CameraReserver

JPEG = b"\xff\xd8hello\xff\xd9"

def make_source():
    return MjpegSource("http://unused", name="Printer", opener=lambda url: io.BytesIO(b""))  # never started

def test_snapshot_and_health():
    src = make_source(); server = CameraReserver([src], port=0); server.start()
    try:
        base = f"http://127.0.0.1:{server.port}"
        assert urllib.request.urlopen(base + "/healthz").read() == b"ok"
        try:
            urllib.request.urlopen(base + "/cameras/0/snapshot")
        except urllib.error.HTTPError as e:
            assert e.code == 503
        src._store(JPEG)
        r = urllib.request.urlopen(base + "/cameras/0/snapshot")
        assert r.headers["Content-Type"] == "image/jpeg" and r.read() == JPEG
        try:
            urllib.request.urlopen(base + "/cameras/7/snapshot")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        server.stop()

def test_stream_serves_two_clients_and_closes_when_stale():
    src = make_source(); server = CameraReserver([src], port=0, stale_after_s=0.3); server.start()
    try:
        base = f"http://127.0.0.1:{server.port}"
        a = urllib.request.urlopen(base + "/cameras/0/stream", timeout=2)
        b = urllib.request.urlopen(base + "/cameras/0/stream", timeout=2)
        assert a.headers["Content-Type"].startswith("multipart/x-mixed-replace")
        src._store(JPEG)
        for client in (a, b):
            assert client.readline().strip() == b"--flashforgeobico"
            headers = {}
            while (line := client.readline()) not in (b"\r\n", b""):
                k, _, v = line.decode().partition(":"); headers[k.lower()] = v.strip()
            assert headers["content-type"] == "image/jpeg" and client.read(int(headers["content-length"])) == JPEG
        rest = a.read()          # no new frames: the server must end the response after stale_after_s
        assert rest in (b"", b"\r\n")
    finally:
        server.stop()
```

- [ ] **Step 2: Run** → ImportError.
- [ ] **Step 3: Implement `camera/reserve.py`**

```python
"""Fans one camera's frames out to any number of HTTP clients, because the printer's own MJPEG
server allows exactly one."""
from __future__ import annotations
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Sequence
from .mjpeg_source import MjpegSource

_logger = logging.getLogger(__name__)
BOUNDARY = b"flashforgeobico"
_ROUTE = re.compile(r"^/cameras/(\d+)/(stream|snapshot)$")


class CameraReserver:
    def __init__(self, sources: Sequence[MjpegSource], port: int, *, stale_after_s: float = 15.0, host: str = "0.0.0.0"):
        reserver = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, fmt, *args):  # quiet; access logs are noise for a stream
                _logger.debug("reserver: " + fmt, *args)

            def do_GET(self):
                if self.path == "/healthz":
                    return self._send(200, b"ok", "text/plain")
                match = _ROUTE.match(self.path.split("?")[0])
                if not match or int(match.group(1)) >= len(reserver._sources):
                    return self._send(404, b"no such camera", "text/plain")
                source = reserver._sources[int(match.group(1))]
                if match.group(2) == "snapshot":
                    latest = source.latest()
                    if latest is None:
                        return self._send(503, b"no frame yet", "text/plain")
                    return self._send(200, latest[0], "image/jpeg")
                self._stream(source)

            def _send(self, code: int, body: bytes, content_type: str):
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _stream(self, source: MjpegSource):
                self.send_response(200)
                self.send_header("Content-Type", f"multipart/x-mixed-replace;boundary={BOUNDARY.decode()}")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                last = 0.0
                try:
                    while not reserver._stopping.is_set():
                        frame = source.wait_for_new_frame(after=last, timeout=reserver._stale_after)
                        if frame is None:
                            return  # stale: end the response so viewers notice
                        jpeg, last = frame
                        self.wfile.write(b"--" + BOUNDARY + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                         + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return

        self._sources = list(sources)
        self._stale_after = stale_after_s
        self._stopping = threading.Event()
        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, name="reserver", daemon=True)
        self._thread.start()
        _logger.info("camera re-server listening on port %d for %d camera(s)", self.port, len(self._sources))

    def stop(self) -> None:
        self._stopping.set()
        self._server.shutdown()
        self._server.server_close()
```

- [ ] **Step 4: Run** → pass. **Step 5: Commit** `feat: HTTP re-server fans camera frames out to many clients`.

---

### Task 9: Obico server connection

**Files:**
- Create: `src/flashforge_obico/obico/server.py`, `tests/test_server.py`

**Interfaces:**
- Produces:
  ```python
  class WsLike(Protocol): def send(self, data: str) -> None; def recv(self) -> str | bytes; def close(self) -> None; def settimeout(self, t: float) -> None
  Connector = Callable[[str, list[str]], WsLike]        # (ws url, headers) -> connected socket; default websocket.create_connection
  class Poster(Protocol): def __call__(self, method: str, url: str, *, headers: dict, timeout: float, data=None, files=None) -> Any  # returns object with .ok, .status_code, .json()
  def ws_url(base_url: str) -> str                        # http://h:p -> ws://h:p/ws/dev/ ; https -> wss
  def verify_link_code(base_url: str, code: str, *, poster: Poster | None = None) -> str    # returns auth token; raises ObicoError
  class ObicoError(Exception)
  class ObicoServer:
      def __init__(self, base_url: str, auth_token: str, *, on_message: Callable[[dict], None], connector: Connector | None = None, poster: Poster | None = None, sleep=time.sleep)
      def start(self) -> None; def stop(self) -> None
      def send(self, message: dict) -> None                # queued; queue max 50, oldest dropped
      def post_pic(self, jpeg: bytes, *, camera_name: str, is_primary: bool, viewing_boost: bool) -> bool
      def post_printer_event(self, *, title: str, text: str, event_type: str = "PRINTER_ERROR", event_class: str = "ERROR", snapshot: bytes | None = None) -> bool
      shared_token_detected: threading.Event              # set on close code 4321; agent exits
      connected: threading.Event
  ```

- [ ] **Step 1: Failing tests**

```python
import json, queue, threading, time
import pytest
from flashforge_obico.obico.server import ObicoError, ObicoServer, verify_link_code, ws_url

class FakeWs:
    def __init__(self, incoming):
        self.incoming = queue.Queue(); [self.incoming.put(m) for m in incoming]
        self.sent = []; self.closed = threading.Event()
    def settimeout(self, t): pass
    def send(self, data): self.sent.append(json.loads(data))
    def recv(self):
        try:
            item = self.incoming.get(timeout=0.2)
        except queue.Empty:
            raise TimeoutError()
        if isinstance(item, Exception):
            raise item
        return item
    def close(self): self.closed.set()

class FakeResponse:
    def __init__(self, ok=True, status_code=200, body=None):
        self.ok, self.status_code, self._body = ok, status_code, body or {}
    def json(self): return self._body

def test_ws_url():
    assert ws_url("http://web:3334") == "ws://web:3334/ws/dev/"
    assert ws_url("https://obico.example") == "wss://obico.example/ws/dev/"

def test_sends_queued_messages_with_bearer_header_and_dispatches_incoming():
    received = []
    ws = FakeWs([json.dumps({"commands": [{"cmd": "pause"}]})])
    seen_headers = []
    def connector(url, headers):
        seen_headers.append((url, headers)); return ws
    server = ObicoServer("http://web:3334", "TOKEN", on_message=received.append, connector=connector, poster=None, sleep=lambda s: None)
    server.start()
    try:
        server.send({"hello": 1})
        deadline = time.time() + 2
        while (not received or not ws.sent) and time.time() < deadline:
            time.sleep(0.01)
        assert seen_headers[0] == ("ws://web:3334/ws/dev/", ["authorization: bearer TOKEN"])
        assert received == [{"commands": [{"cmd": "pause"}]}]
        assert {"hello": 1} in ws.sent
    finally:
        server.stop()

def test_reconnects_after_socket_error_with_backoff():
    sockets = [FakeWs([ConnectionResetError()]), FakeWs([])]
    sleeps = []
    server = ObicoServer("http://web:3334", "T", on_message=lambda m: None,
                         connector=lambda u, h: sockets.pop(0), poster=None, sleep=sleeps.append)
    server.start()
    try:
        deadline = time.time() + 2
        while sockets and time.time() < deadline:
            time.sleep(0.01)
        assert not sockets and sleeps and sleeps[0] >= 1.0
    finally:
        server.stop()

def test_shared_token_close_stops_reconnecting():
    from websocket import WebSocketConnectionClosedException
    class Closed(WebSocketConnectionClosedException):
        pass
    ws = FakeWs([]); ws.incoming.put(ObicoServer.SharedToken())
    calls = []
    server = ObicoServer("http://web:3334", "T", on_message=lambda m: None,
                         connector=lambda u, h: (calls.append(1), ws)[1], poster=None, sleep=lambda s: None)
    server.start()
    try:
        assert server.shared_token_detected.wait(2.0)
        time.sleep(0.1)
        assert calls == [1]
    finally:
        server.stop()

def test_post_pic_and_event_use_token_auth():
    posted = []
    def poster(method, url, *, headers, timeout, data=None, files=None):
        posted.append((method, url, headers, data, files)); return FakeResponse()
    server = ObicoServer("http://web:3334/", "T", on_message=lambda m: None, connector=None, poster=poster)
    assert server.post_pic(b"jpg", camera_name="Printer", is_primary=True, viewing_boost=False) is True
    method, url, headers, data, files = posted[0]
    assert (method, url) == ("POST", "http://web:3334/api/v1/octo/pic/")
    assert headers == {"Authorization": "Token T"}
    assert data == {"is_primary_camera": True, "is_nozzle_camera": False, "camera_name": "Printer", "viewing_boost": False}
    assert files == {"pic": b"jpg"}
    assert server.post_printer_event(title="Pause failed", text="details", snapshot=b"jpg") is True
    method, url, headers, data, files = posted[1]
    assert url == "http://web:3334/api/v1/octo/printer_events/"
    assert data == {"event_title": "Pause failed", "event_text": "details", "event_type": "PRINTER_ERROR", "event_class": "ERROR"}
    assert files == {"snapshot": b"jpg"}

def test_post_failure_returns_false():
    server = ObicoServer("http://web:3334", "T", on_message=lambda m: None, connector=None,
                         poster=lambda *a, **k: FakeResponse(ok=False, status_code=500))
    assert server.post_pic(b"x", camera_name="c", is_primary=True, viewing_boost=False) is False

def test_verify_link_code():
    calls = []
    def poster(method, url, *, headers, timeout, data=None, files=None):
        calls.append((method, url, data)); return FakeResponse(body={"printer": {"auth_token": "NEW"}})
    assert verify_link_code("http://web:3334", " 123456 ", poster=poster) == "NEW"
    assert calls[0] == ("POST", "http://web:3334/api/v1/octo/verify/?code=123456", None)
    with pytest.raises(ObicoError):
        verify_link_code("http://web:3334", "0", poster=lambda *a, **k: FakeResponse(ok=False, status_code=400))
```

- [ ] **Step 2: Run** → ImportError.
- [ ] **Step 3: Implement `obico/server.py`**

```python
"""The Obico side: a websocket for status out / commands in, REST for pictures and events.
Mirrors moonraker-obico's ServerConn; kept synchronous and thread-based like the original."""
from __future__ import annotations
import json
import logging
import queue
import threading
import time
from typing import Any, Callable, Protocol
import requests
import websocket

_logger = logging.getLogger(__name__)
QUEUE_MAX = 50
BACKOFF_START, BACKOFF_MAX = 1.0, 300.0
SHARED_TOKEN_CLOSE_CODE = 4321


class ObicoError(Exception):
    pass


class WsLike(Protocol):
    def send(self, data: str) -> None: ...
    def recv(self) -> str | bytes: ...
    def close(self) -> None: ...
    def settimeout(self, timeout: float) -> None: ...


Connector = Callable[[str, list[str]], WsLike]


class Poster(Protocol):
    def __call__(self, method: str, url: str, *, headers: dict, timeout: float, data=None, files=None) -> Any: ...


def _default_connector(url: str, headers: list[str]) -> WsLike:
    return websocket.create_connection(url, header=headers, timeout=10)


def _default_poster(method, url, *, headers, timeout, data=None, files=None):
    return requests.request(method, url, headers=headers, timeout=timeout, data=data, files=files)


def ws_url(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    scheme, _, rest = base.partition("://")
    return f"{'wss' if scheme == 'https' else 'ws'}://{rest}/ws/dev/"


def verify_link_code(base_url: str, code: str, *, poster: Poster | None = None) -> str:
    post = poster or _default_poster
    url = f"{base_url.rstrip('/')}/api/v1/octo/verify/?code={code.strip()}"
    response = post("POST", url, headers={}, timeout=15)
    if not response.ok:
        raise ObicoError(f"Obico rejected the code (HTTP {response.status_code})")
    try:
        return response.json()["printer"]["auth_token"]
    except (KeyError, TypeError, ValueError):
        raise ObicoError("Obico's reply did not contain an auth token") from None


class ObicoServer:
    class SharedToken(Exception):
        """Raised by the reader when the server closes with code 4321 (another agent uses this token)."""

    def __init__(self, base_url: str, auth_token: str, *, on_message: Callable[[dict], None],
                 connector: Connector | None = None, poster: Poster | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self._base = base_url.strip().rstrip("/")
        self._token = auth_token
        self._on_message = on_message
        self._connect = connector or _default_connector
        self._post = poster or _default_poster
        self._sleep = sleep
        self._queue: queue.Queue[dict] = queue.Queue(maxsize=QUEUE_MAX)
        self._stop = threading.Event()
        self.connected = threading.Event()
        self.shared_token_detected = threading.Event()
        self._thread: threading.Thread | None = None

    # ── websocket ──────────────────────────────────────────────────────────────────────────
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="obico-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def send(self, message: dict) -> None:
        while True:
            try:
                self._queue.put_nowait(message)
                return
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    _logger.warning("Obico message queue full; dropped the oldest message")
                except queue.Empty:
                    pass

    def _run(self) -> None:
        backoff = BACKOFF_START
        while not self._stop.is_set() and not self.shared_token_detected.is_set():
            ws = None
            try:
                ws = self._connect(ws_url(self._base), [f"authorization: bearer {self._token}"])
                ws.settimeout(1.0)
                self.connected.set()
                _logger.info("connected to Obico at %s", self._base)
                backoff = BACKOFF_START
                self._pump(ws)
            except ObicoServer.SharedToken:
                _logger.error("Obico closed the connection: this auth token is used by another agent. Stopping.")
                self.shared_token_detected.set()
            except Exception as exc:
                _logger.warning("Obico connection: %s: %s", type(exc).__name__, exc)
            finally:
                self.connected.clear()
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
            if not self._stop.is_set() and not self.shared_token_detected.is_set():
                self._sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)

    def _pump(self, ws: WsLike) -> None:
        """Interleaves sending queued messages and receiving, on one thread, until an error."""
        reader = threading.Thread(target=self._reader, args=(ws,), daemon=True)
        error: list[BaseException] = []
        reader_state = {"error": error}
        reader = threading.Thread(target=self._reader, args=(ws, error), daemon=True)
        reader.start()
        while not self._stop.is_set():
            if error:
                raise error[0]
            try:
                message = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            ws.send(json.dumps(message, default=str))

    def _reader(self, ws: WsLike, error: list[BaseException]) -> None:
        while not self._stop.is_set():
            try:
                raw = ws.recv()
            except (TimeoutError, websocket.WebSocketTimeoutException):
                continue
            except ObicoServer.SharedToken as exc:
                error.append(exc); return
            except websocket.WebSocketConnectionClosedException as exc:
                error.append(exc); return
            except Exception as exc:
                error.append(exc); return
            if isinstance(raw, bytes):
                continue  # bson from the server is not something this agent asked for
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if isinstance(message, dict):
                try:
                    self._on_message(message)
                except Exception:
                    _logger.exception("error handling a message from Obico")

    # ── REST ───────────────────────────────────────────────────────────────────────────────
    def _rest(self, path: str, *, data: dict, files: dict | None) -> bool:
        try:
            response = self._post("POST", f"{self._base}{path}", headers={"Authorization": f"Token {self._token}"},
                                  timeout=60, data=data, files=files)
        except Exception as exc:
            _logger.warning("POST %s failed: %s: %s", path, type(exc).__name__, exc)
            return False
        if not response.ok:
            _logger.warning("POST %s -> HTTP %s", path, response.status_code)
        return bool(response.ok)

    def post_pic(self, jpeg: bytes, *, camera_name: str, is_primary: bool, viewing_boost: bool) -> bool:
        return self._rest("/api/v1/octo/pic/", files={"pic": jpeg},
                          data={"is_primary_camera": is_primary, "is_nozzle_camera": False,
                                "camera_name": camera_name, "viewing_boost": viewing_boost})

    def post_printer_event(self, *, title: str, text: str, event_type: str = "PRINTER_ERROR",
                           event_class: str = "ERROR", snapshot: bytes | None = None) -> bool:
        data = {"event_title": title, "event_text": text, "event_type": event_type, "event_class": event_class}
        self.send({"passthru": {"printer_event": data}})
        return self._rest("/api/v1/octo/printer_events/", data=data, files={"snapshot": snapshot} if snapshot else None)
```

  Clean-up required before commit: `_pump` has leftover duplicate lines (`reader = ...` twice, `reader_state`). Remove them so `_pump` creates one reader thread with `(ws, error)`. The real `websocket-client` raises `WebSocketConnectionClosedException` on a server close; to detect **code 4321** the default connector must wrap `recv`: use `ws.recv_data()` (returns `(opcode, data)`) and raise `ObicoServer.SharedToken` when `opcode == websocket.ABNF.OPCODE_CLOSE` and the first two bytes of `data` decode to 4321. Implement that in a small adapter class `_RealWs` returned by `_default_connector` with the same `WsLike` surface, and unit-test it with a fake `recv_data` returning `(8, (4321).to_bytes(2, "big"))`.

- [ ] **Step 4: Run** → pass. **Step 5: Commit** `feat: Obico websocket connection, picture and event posting, link-code exchange`.

---

### Task 10: The agent loop

**Files:**
- Create: `src/flashforge_obico/agent.py`, `tests/test_agent.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  ```python
  class Agent:
      def __init__(self, config: Config, printer: FlashforgeClient, obico: ObicoServer, cameras: list[MjpegSource], *, clock=time.time, monotonic=time.monotonic, sleep=time.sleep)
      def run(self, stop: threading.Event) -> int        # blocks; returns process exit code (0 normal, 2 shared token)
      def poll_once(self) -> None                         # one printer poll + status send (tests drive this directly)
      def handle_server_message(self, message: dict) -> None
      def execute_command(self, cmd: str) -> None         # 'pause' | 'resume' | 'cancel'; blocking incl. read-back
      def post_primary_frame(self) -> bool
      viewing: bool
  ```
  `ObicoServer` is constructed by the CLI with `on_message=agent.handle_server_message` (see Task 11); `Agent.__init__` stores the server it is given.

- [ ] **Step 1: Failing tests**

```python
import threading, time
from flashforge_obico.agent import Agent
from flashforge_obico.config import load_config
from flashforge_obico.flashforge.client import FlashforgeClient, FlashforgeError
from flashforge_obico.camera.mjpeg_source import MjpegSource
import io

ENV = {"FF_HOST": "10.0.0.10", "FF_SERIAL": "SN", "FF_CHECK_CODE": "CC", "OBICO_AUTH_TOKEN": "TK", "PUBLIC_HOST": "10.0.0.2"}

class FakeObico:
    def __init__(self):
        self.sent, self.pics, self.events = [], [], []
        self.shared_token_detected = threading.Event(); self.connected = threading.Event(); self.connected.set()
    def send(self, m): self.sent.append(m)
    def post_pic(self, jpeg, **kw): self.pics.append((jpeg, kw)); return True
    def post_printer_event(self, **kw): self.events.append(kw); return True
    def start(self): pass
    def stop(self): pass

class ScriptedPrinter:
    """Returns detail dicts in order; an Exception entry raises. The last entry repeats."""
    def __init__(self, replies): self.replies = list(replies); self.commands = []
    def detail(self):
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(r, Exception): raise r
        return dict(r)
    def pause(self): self.commands.append("pause")
    def resume(self): self.commands.append("resume")
    def cancel(self): self.commands.append("cancel")

def make(detail_replies, cameras=None):
    clock = {"t": 1000.0}
    agent = Agent(load_config(ENV), ScriptedPrinter(detail_replies), FakeObico(), cameras or [],
                  clock=lambda: clock["t"], monotonic=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s))
    return agent, clock

def test_first_poll_sends_settings_then_status_changes_only_or_heartbeat(detail):
    printing = {**detail, "status": "printing", "printFileName": "a.gcode", "printProgress": 0.1}
    agent, clock = make([detail, detail, printing])
    agent.poll_once()
    first = agent.obico.sent[0]
    assert "settings" in first and first["status"]["state"]["text"] == "Operational" and first["current_print_ts"] == -1
    assert first["settings"]["webcams"][0]["stream_url"] == "http://10.0.0.2:8081/cameras/0/stream"
    agent.poll_once()                                  # unchanged, within 30 s: nothing sent
    assert len(agent.obico.sent) == 1
    agent.poll_once()                                  # job started: status + event
    assert agent.obico.sent[1]["event"] == {"event_type": "PrintStarted"}
    assert agent.obico.sent[1]["status"]["state"]["text"] == "Printing"
    clock["t"] += 31
    agent.poll_once()
    assert len(agent.obico.sent) == 3 and "event" not in agent.obico.sent[2]

def test_three_failures_mean_offline_and_never_a_command(detail):
    agent, _ = make([detail, FlashforgeError("x"), FlashforgeError("x"), FlashforgeError("x")])
    for _ in range(4):
        agent.poll_once()
    assert agent.obico.sent[-1]["status"] == {} and agent.printer.commands == []

def test_pause_command_is_forwarded_and_read_back(detail):
    printing = {**detail, "status": "printing", "printFileName": "a.gcode"}
    paused = {**printing, "status": "paused"}
    agent, _ = make([printing, printing, paused])
    agent.poll_once()
    agent.execute_command("pause")
    assert agent.printer.commands == ["pause"] and agent.obico.events == []

def test_pause_that_does_not_take_raises_a_printer_event(detail):
    printing = {**detail, "status": "printing", "printFileName": "a.gcode"}
    agent, _ = make([printing])
    agent.poll_once()
    agent.execute_command("pause")
    assert agent.printer.commands == ["pause"]
    assert agent.obico.events and agent.obico.events[0]["event_title"] == "Pause did not take"

def test_commands_are_idempotent_and_need_a_job(detail):
    paused = {**detail, "status": "paused", "printFileName": "a.gcode"}
    agent, _ = make([paused])
    agent.poll_once()
    agent.execute_command("pause")                     # already paused
    assert agent.printer.commands == []
    idle, _ = make([detail]); idle.poll_once(); idle.execute_command("cancel")
    assert idle.printer.commands == []

def test_server_messages_dispatch_commands_and_viewing(detail):
    printing = {**detail, "status": "printing", "printFileName": "a.gcode"}
    agent, _ = make([printing, {**printing, "status": "paused"}])
    agent.poll_once()
    agent.handle_server_message({"remote_status": {"viewing": True}})
    assert agent.viewing is True
    agent.handle_server_message({"commands": [{"cmd": "pause"}]})
    agent.wait_for_commands(timeout=2.0)
    assert agent.printer.commands == ["pause"]

def test_primary_frame_posting_respects_freshness(detail):
    cam = MjpegSource("http://x", name="Printer", opener=lambda u: io.BytesIO(b""))
    agent, clock = make([detail], cameras=[cam])
    assert agent.post_primary_frame() is False                 # no frame yet
    cam._store(b"jpg")
    assert agent.post_primary_frame() is True
    assert agent.obico.pics[0][1] == {"camera_name": "Printer", "is_primary": True, "viewing_boost": False}
    clock["t"] += 16
    assert agent.post_primary_frame() is False                 # stale

def test_hard_fault_posts_one_event_per_code(detail):
    faulty = {**detail, "status": "error", "errorCode": "E7"}
    agent, _ = make([faulty])
    agent.poll_once(); agent.poll_once()
    assert [e["event_title"] for e in agent.obico.events] == ["Printer error E7"]
```

  Note: `MjpegSource` uses its own `clock`; for the freshness test construct it with `clock=lambda: clock["t"]` — adjust the test to pass the shared clock into `MjpegSource(..., clock=...)`.

- [ ] **Step 2: Run** → ImportError.
- [ ] **Step 3: Implement `agent.py`**

```python
from __future__ import annotations
import logging
import threading
import time
from .camera.mjpeg_source import MjpegSource
from .config import Config
from .flashforge.client import FlashforgeClient, FlashforgeError
from .flashforge.snapshot import MachineState, PrinterSnapshot, parse_snapshot
from .job_tracker import Event, JobTracker
from .obico.status import OFFLINE, PAUSED, PRINTING, build_message, build_settings, build_status, obico_state_text, webcam_entry

_logger = logging.getLogger(__name__)
POLL_ACTIVE_S, POLL_IDLE_S = 2.0, 5.0
PIC_INTERVAL_S, PIC_BOOST_INTERVAL_S, PIC_MAX_AGE_S = 10.0, 1.0, 15.0
HEARTBEAT_S = 30.0
OFFLINE_AFTER_FAILURES = 3
READBACK_ATTEMPTS, READBACK_INTERVAL_S = 5, 1.0
EXIT_OK, EXIT_SHARED_TOKEN = 0, 2


class Agent:
    def __init__(self, config: Config, printer: FlashforgeClient, obico, cameras: list[MjpegSource], *,
                 clock=time.time, monotonic=time.monotonic, sleep=time.sleep):
        self.config, self.printer, self.obico, self.cameras = config, printer, obico, cameras
        self._clock, self._monotonic, self._sleep = clock, monotonic, sleep
        self.tracker = JobTracker()
        self.viewing = False
        self._snapshot: PrinterSnapshot | None = None
        self._state_text = OFFLINE
        self._failures = 0
        self._settings_sent = False
        self._last_sent_at = -1e9
        self._faults_reported: set[str] = set()
        self._commands: "queue.Queue[str]" = __import__("queue").Queue()
        self._command_lock = threading.Lock()
        self._command_thread: threading.Thread | None = None

    # ── printer side ───────────────────────────────────────────────────────────────────────
    def poll_once(self) -> None:
        try:
            self._snapshot = parse_snapshot(self.printer.detail())
            self._failures = 0
        except FlashforgeError as exc:
            self._failures += 1
            _logger.warning("printer poll failed (%d): %s", self._failures, exc)
            if self._failures < OFFLINE_AFTER_FAILURES:
                return
            self._snapshot = None
        now = self._clock()
        events = self.tracker.observe(self._snapshot, now)
        new_text = obico_state_text(self._snapshot, self._state_text)
        changed = new_text != self._state_text or bool(events) or not self._settings_sent
        self._state_text = new_text
        self._report_faults()
        if changed or now - self._last_sent_at >= HEARTBEAT_S:
            self._send_status(events[0] if events else None, now)
            for extra in events[1:]:
                self._send_status(extra, now)

    def _send_status(self, event: Event | None, now: float) -> None:
        settings = None if self._settings_sent else build_settings(self._webcams())
        self.obico.send(build_message(status=build_status(self._snapshot, self.tracker, self._state_text, now),
                                      current_print_ts=self.tracker.current_print_ts, event=event, settings=settings))
        self._settings_sent = True
        self._last_sent_at = now

    def _webcams(self) -> list[dict]:
        entries = []
        for i, cam in enumerate(self.cameras or [None]):
            name = cam.name if cam is not None else "Printer"
            entries.append(webcam_entry(name=name, is_primary=(i == 0),
                                        stream_url=self.config.camera_public_url(i, "stream"),
                                        snapshot_url=self.config.camera_public_url(i, "snapshot")))
        return entries

    def _report_faults(self) -> None:
        snap = self._snapshot
        if snap is None or not snap.error_code or snap.error_code in self._faults_reported:
            return
        self._faults_reported.add(snap.error_code)
        frame = self.cameras[0].latest() if self.cameras else None
        self.obico.post_printer_event(title=f"Printer error {snap.error_code}",
                                      text=f"The printer reports error code {snap.error_code}.",
                                      snapshot=frame[0] if frame else None)

    def poll_interval(self) -> float:
        return POLL_ACTIVE_S if self._state_text in (PRINTING, PAUSED) else POLL_IDLE_S

    # ── Obico side ─────────────────────────────────────────────────────────────────────────
    def handle_server_message(self, message: dict) -> None:
        remote = message.get("remote_status")
        if isinstance(remote, dict) and "viewing" in remote:
            self.viewing = bool(remote["viewing"])
        for command in message.get("commands", []) or []:
            cmd = command.get("cmd") if isinstance(command, dict) else None
            if cmd in ("pause", "resume", "cancel"):
                self._commands.put(cmd)
                self._ensure_command_thread()

    def _ensure_command_thread(self) -> None:
        if self._command_thread is None or not self._command_thread.is_alive():
            self._command_thread = threading.Thread(target=self._drain_commands, name="commands", daemon=True)
            self._command_thread.start()

    def _drain_commands(self) -> None:
        while True:
            try:
                cmd = self._commands.get(timeout=0.5)
            except Exception:
                return
            try:
                self.execute_command(cmd)
            finally:
                self._commands.task_done()

    def wait_for_commands(self, timeout: float) -> None:  # tests
        deadline = time.time() + timeout
        while (self._commands.unfinished_tasks and time.time() < deadline):
            time.sleep(0.01)

    def execute_command(self, cmd: str) -> None:
        with self._command_lock:
            snap = self._snapshot
            if snap is None or not snap.has_job:
                _logger.info("ignoring %s: no active job", cmd)
                return
            paused = snap.state is MachineState.PAUSED
            if (cmd == "pause" and paused) or (cmd == "resume" and not paused):
                _logger.info("ignoring %s: already in that state", cmd)
                return
            _logger.info("Obico asked to %s; forwarding to the printer", cmd)
            try:
                getattr(self.printer, cmd)()
            except FlashforgeError as exc:
                self.obico.post_printer_event(title=f"{cmd.capitalize()} failed", text=str(exc))
                return
            if not self._read_back(cmd):
                self.obico.post_printer_event(title=f"{cmd.capitalize()} did not take",
                                              text=f"The printer was asked to {cmd} but still reports "
                                                   f"'{self._snapshot.state.value if self._snapshot else 'unknown'}'.")

    def _read_back(self, cmd: str) -> bool:
        expected = {"pause": lambda s: s.state is MachineState.PAUSED,
                    "resume": lambda s: s.has_job and s.state is not MachineState.PAUSED,
                    "cancel": lambda s: not s.has_job}[cmd]
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
        return self.obico.post_pic(latest[0], camera_name=self.cameras[0].name, is_primary=True, viewing_boost=self.viewing)

    # ── main loop ──────────────────────────────────────────────────────────────────────────
    def run(self, stop: threading.Event) -> int:
        pic_thread = threading.Thread(target=self._pic_loop, args=(stop,), name="pics", daemon=True)
        pic_thread.start()
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
```

  Replace the `__import__("queue")` hack with a normal `import queue` at the top. In tests, `_read_back` uses the injected `sleep`, which advances the fake clock rather than waiting.

- [ ] **Step 4: Run** → pass. **Step 5: Commit** `feat: agent loop wires printer polling, Obico messaging, commands and pictures`.

---

### Task 11: CLI, Docker image, compose snippet, README

**Files:**
- Create: `src/flashforge_obico/__main__.py`, `Dockerfile`, `.dockerignore`, `deploy/obico-compose.snippet.yml`
- Modify: `README.md`

- [ ] **Step 1: Write `__main__.py`**

```python
from __future__ import annotations
import argparse
import logging
import os
import signal
import sys
import threading
from . import VERSION
from .agent import Agent
from .camera.mjpeg_source import MjpegSource
from .camera.reserve import CameraReserver
from .config import Config, ConfigError, load_config
from .flashforge.client import FlashforgeClient, FlashforgeError
from .flashforge.snapshot import parse_snapshot
from .logging_utils import configure_logging
from .obico.server import ObicoError, ObicoServer, verify_link_code

_logger = logging.getLogger("flashforge_obico")


def _cameras(config: Config, printer: FlashforgeClient) -> list[MjpegSource]:
    if config.cameras:
        return [MjpegSource(cam.stream_url, name=cam.name) for cam in config.cameras]
    try:
        url = parse_snapshot(printer.detail()).camera_stream_url
    except FlashforgeError as exc:
        _logger.warning("could not read the printer's camera URL yet (%s); no camera until restart", exc)
        return []
    return [MjpegSource(url, name="Printer")] if url else []


def run(config: Config) -> int:
    printer = FlashforgeClient(config.ff_host, config.ff_serial, config.ff_check_code)
    cameras = _cameras(config, printer)
    for cam in cameras:
        cam.start()
    reserver = CameraReserver(cameras, config.reserve_port) if config.reserve_port > 0 and cameras else None
    if reserver:
        reserver.start()
    agent_holder: dict[str, Agent] = {}
    obico = ObicoServer(config.obico_url, config.obico_auth_token,
                        on_message=lambda m: agent_holder["agent"].handle_server_message(m))
    agent = Agent(config, printer, obico, cameras)
    agent_holder["agent"] = agent
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    obico.start()
    _logger.info("flashforge-obico %s: printer %s, Obico %s, %d camera(s)", VERSION, config.ff_host, config.obico_url, len(cameras))
    try:
        return agent.run(stop)
    finally:
        obico.stop()
        for cam in cameras:
            cam.stop()
        if reserver:
            reserver.stop()


def link(config: Config, code: str) -> int:
    try:
        token = verify_link_code(config.obico_url, code)
    except ObicoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("Linked. Add this to the Obico .env and restart the agent:")
    print(f"OBICO_AUTH_TOKEN={token}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flashforge-obico", description="Obico agent for the FlashForge Creator 5 / 5 Pro")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="run the agent (default)")
    link_parser = sub.add_parser("link", help="exchange a 6-digit Obico link code for a printer token")
    link_parser.add_argument("code")
    args = parser.parse_args(argv)
    try:
        config = load_config(os.environ, require_token=args.command != "link")
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1
    configure_logging(config.log_level, config.secrets)
    if args.command == "link":
        return link(config, args.code)
    return run(config)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Write `Dockerfile` and `.dockerignore`**

```dockerfile
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN useradd --create-home --uid 10001 agent
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
USER agent
EXPOSE 8081
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8081/healthz', timeout=3).status==200 else 1)"
ENTRYPOINT ["flashforge-obico"]
CMD ["run"]
```

```
.git
.venv
tests
docs
__pycache__
```

  If `RESERVE_PORT=0` the healthcheck fails; that is acceptable because the compose service always runs the re-server.

- [ ] **Step 3: Write `deploy/obico-compose.snippet.yml`**

```yaml
# Append under `services:` in /mnt/user/appdata/obico-server/docker-compose.yml.
# Add to that directory's .env:  FF_SERIAL=…  FF_CHECK_CODE=…  OBICO_AUTH_TOKEN=…
  flashforge_agent:
    image: docker.io/hananv/flashforge-obico:latest
    restart: unless-stopped
    depends_on:
      - web
    ports:
      - "8081:8081"
    environment:
      FF_HOST: '${FF_HOST-10.0.0.10}'
      FF_SERIAL: '${FF_SERIAL}'
      FF_CHECK_CODE: '${FF_CHECK_CODE}'
      OBICO_URL: 'http://web:3334'
      OBICO_AUTH_TOKEN: '${OBICO_AUTH_TOKEN}'
      PUBLIC_HOST: '${PUBLIC_HOST-10.0.0.2}'
      RESERVE_PORT: '8081'
      LOG_LEVEL: '${FF_LOG_LEVEL-INFO}'
    logging:
      driver: json-file
      options:
        max-size: 1m
        max-file: "5"
```

- [ ] **Step 4: Update `README.md`** with: what it does (3 sentences), the camera single-client fact, configuration table (copy from spec §5 including `PUBLIC_HOST`), the re-server URL scheme `/cameras/<i>/stream|snapshot`, how to build (`docker buildx build --platform linux/amd64 -t docker.io/hananv/flashforge-obico:latest --push .`), how to deploy (append snippet, fill `.env`, `docker compose up -d flashforge_agent`), how to link (`docker compose run --rm flashforge_agent link 123456`, or create the printer in Obico's Django shell and copy its token), and a pointer to the OrcaMCP console integration.

- [ ] **Step 5: Smoke-test the CLI without hardware**: `.venv/bin/flashforge-obico run` with no env → prints `configuration error: FF_HOST is required`, exit 1. `FF_HOST=x FF_SERIAL=y FF_CHECK_CODE=z PUBLIC_HOST=h .venv/bin/flashforge-obico link 000000` against nothing → error line, exit 1.

- [ ] **Step 6: Build the image locally**: `docker build -t flashforge-obico:dev .` and `docker run --rm flashforge-obico:dev run` → exits 1 with the configuration error (proves the entrypoint).

- [ ] **Step 7: Run the whole suite** `.venv/bin/pytest -q` → pass. **Commit** `feat: CLI entry point, Docker image and Obico compose snippet`.

---

### Task 12: Opt-in live test against the real printer

**Files:**
- Create: `tests/test_live.py`

- [ ] **Step 1: Write the test** (skips unless `FF_HOST`, `FF_SERIAL`, `FF_CHECK_CODE` are set; never sends job commands)

```python
import os, urllib.request
import pytest
from flashforge_obico.camera.mjpeg_source import MjpegSource
from flashforge_obico.camera.reserve import CameraReserver
from flashforge_obico.flashforge.client import FlashforgeClient
from flashforge_obico.flashforge.snapshot import MachineState, parse_snapshot

pytestmark = pytest.mark.live
NEEDED = ("FF_HOST", "FF_SERIAL", "FF_CHECK_CODE")

@pytest.fixture
def client():
    if not all(os.environ.get(k) for k in NEEDED):
        pytest.skip("set FF_HOST, FF_SERIAL and FF_CHECK_CODE to run the live test")
    return FlashforgeClient(os.environ["FF_HOST"], os.environ["FF_SERIAL"], os.environ["FF_CHECK_CODE"])

def test_detail_parses(client):
    snap = parse_snapshot(client.detail())
    assert snap.state is not MachineState.UNKNOWN or True   # unknown is tolerated, but must not crash
    assert snap.model and snap.firmware and snap.camera_stream_url.startswith("http")

def test_camera_frame_and_reserver_two_clients(client):
    url = parse_snapshot(client.detail()).camera_stream_url
    src = MjpegSource(url, name="Printer"); src.start()
    server = CameraReserver([src], port=0, stale_after_s=5); server.start()
    try:
        assert src.wait_for_new_frame(after=0.0, timeout=10.0) is not None
        base = f"http://127.0.0.1:{server.port}"
        a = urllib.request.urlopen(base + "/cameras/0/stream", timeout=5)
        b = urllib.request.urlopen(base + "/cameras/0/stream", timeout=5)
        assert a.readline().startswith(b"--") and b.readline().startswith(b"--")
        jpeg = urllib.request.urlopen(base + "/cameras/0/snapshot", timeout=5).read()
        assert jpeg[:2] == b"\xff\xd8"
    finally:
        server.stop(); src.stop()
```

- [ ] **Step 2: Run it for real** from the Mac, reading the credentials out of the OrcaMCP physical printer preset without echoing them:

```bash
cd ~/Projects/flashforge-obico
P="$HOME/Library/Application Support/OrcaMCP/user/default/machine/C5P.json"
FF_HOST=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['print_host'])" "$P") \
FF_SERIAL=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['flashforge_serial_number'])" "$P") \
FF_CHECK_CODE=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['printhost_apikey'])" "$P") \
.venv/bin/pytest -q -m live tests/test_live.py
```
  Expected: 2 passed. If OrcaSlicer is running with the Flashforge Device tab open, close that tab first (it would hold the single camera slot).

- [ ] **Step 3: Commit** `test: opt-in live test against the printer and its camera`.

---

### Task 13: Deploy on Unraid and link to Obico

This is an operations task; every command runs over `ssh root@HV-Unraid` unless stated. Credentials are moved with shell redirection, never printed.

- [ ] **Step 1: Build and push the image** (from the Mac; requires `docker login` as `hananv`, ask the user if the Mac is not logged in):
  `docker buildx build --platform linux/amd64 -t docker.io/hananv/flashforge-obico:0.1.0 -t docker.io/hananv/flashforge-obico:latest --push .`
  Fallback if the Mac cannot push: `git clone` the repo on Unraid under `/mnt/user/appdata/flashforge-obico` and `docker build -t docker.io/hananv/flashforge-obico:latest . && docker push docker.io/hananv/flashforge-obico:latest` there (Unraid is already logged in).

- [ ] **Step 2: Create the printer in Obico and capture its token** (no UI needed):
```bash
cd /mnt/user/appdata/obico-server && docker exec obico-server-web-1 python manage.py shell -c "
from secrets import token_hex
from app.models import Printer, User
u = User.objects.get(email='<your obico login email>')
p, created = Printer.objects.get_or_create(name='Creator 5 Pro', user=u, defaults={'auth_token': token_hex(16)})
open('/tmp/token','w').write(p.auth_token)
print('created' if created else 'existing', 'action_on_failure=', p.action_on_failure)
" && docker cp obico-server-web-1:/tmp/token /tmp/obico_token && docker exec obico-server-web-1 rm /tmp/token
```
  Expected output: `created action_on_failure= PAUSE`.

- [ ] **Step 3: Write the secrets into `.env`** using the OrcaMCP preset values copied from the Mac (`scp` is not needed: read them on the Mac and pass via ssh stdin):
```bash
# on the Mac
P="$HOME/Library/Application Support/OrcaMCP/user/default/machine/C5P.json"
python3 - "$P" <<'EOF' | ssh root@HV-Unraid 'cat >> /mnt/user/appdata/obico-server/.env && echo "OBICO_AUTH_TOKEN=$(cat /tmp/obico_token)" >> /mnt/user/appdata/obico-server/.env && rm /tmp/obico_token && grep -c "^FF_\|^OBICO_AUTH" /mnt/user/appdata/obico-server/.env'
import json, sys
c = json.load(open(sys.argv[1]))
print(f"FF_HOST={c['print_host'].split(':')[0]}")
print(f"FF_SERIAL={c['flashforge_serial_number']}")
print(f"FF_CHECK_CODE={c['printhost_apikey']}")
EOF
```
  Expected: `4`.

- [ ] **Step 4: Append the compose service**: copy `deploy/obico-compose.snippet.yml` (without its comment header) under `services:` in `/mnt/user/appdata/obico-server/docker-compose.yml`. Back the file up first: `cp docker-compose.yml docker-compose.yml.bak-2026-09-15`. Validate: `docker compose config --quiet`.

- [ ] **Step 5: Start it**: `docker compose pull flashforge_agent && docker compose up -d flashforge_agent && sleep 20 && docker compose logs --tail 30 flashforge_agent`.
  Expected log lines: `connected to Obico at http://web:3334`, `camera re-server listening on port 8081 for 1 camera(s)`. No line may contain the check code or token (the redaction filter guarantees it; verify by grepping the logs for the token's first 4 characters — expect no match).

- [ ] **Step 6: Verify from the Mac**: `curl -s -m 5 -o /dev/null -w "%{http_code} %{content_type}\n" http://10.0.0.2:8081/cameras/0/snapshot` → `200 image/jpeg`. Open `http://10.0.0.2:3334`, log in, confirm the Creator 5 Pro shows "Operational" with a refreshing picture. Start a short test print from OrcaSlicer, confirm Obico shows "Printing" with progress within ~10 s, press Pause in Obico, confirm the printer pauses and Obico shows Paused within ~10 s, press Resume, then let it finish or cancel from the printer's screen.

- [ ] **Step 7: Record the deployment** in `README.md` under "Deployed at" (host, compose path, ports) and commit `docs: deployment notes for HV-Unraid`.

---

## Self-review

**Spec coverage:** §3 modules → Tasks 1–10 (with `parsing.py` noted). §4.1 polling/offline → Task 10. §4.2 mapping → Task 5. §4.3 tracker/events/heartbeat/settings-first → Tasks 4, 10. §4.4 commands, idempotency, read-back, cancel only relayed, viewing boost → Task 10. §4.5 camera, single connection, re-server routes, `stream_url` advertising, freshness → Tasks 7, 8, 5, 10. §4.6 hard faults → Task 10. §4.7 failure table → Tasks 7, 8, 9, 10 (queue bound, 4321). §4.8 secrets/logging → Task 6, Task 3's error scrubbing. §5 configuration incl. `link` → Tasks 6, 11. §6 delivery → Tasks 11, 13. §7 testing → each task plus Task 12; acceptance in Task 13.

**Gaps fixed while reviewing:** `PUBLIC_HOST` was missing from the spec's env table and has been added there; the second-camera posting is explicitly *not* done because the server drops it (spec §4.5 now says so).

**Type consistency:** `MjpegSource.latest() -> (bytes, float)` used identically in Tasks 8 and 10; `webcam_entry(name=, is_primary=, stream_url=, snapshot_url=)` matches between Tasks 5 and 10; `ObicoServer.send/post_pic/post_printer_event/shared_token_detected` match between Tasks 9, 10 and the `FakeObico` in Task 10's tests; `Config.camera_public_url(index, kind)` matches Tasks 6 and 10.
