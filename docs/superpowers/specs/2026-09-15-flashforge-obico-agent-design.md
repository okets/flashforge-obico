# flashforge-obico — design

**Date:** 2026-09-15
**Status:** draft for review

## 1. Purpose

Restore print-failure detection for a FlashForge Creator 5 Pro that has been switched to
LAN-only mode. The printer's own cloud-based detection is gone; a self-hosted
[Obico](https://www.obico.io/) server already runs on the user's Unraid machine and has the
detection model, the notification channels (Telegram, e-mail) and the mobile app. Obico ships
agents for OctoPrint and Klipper only.

`flashforge-obico` is that missing agent: a small Python service that speaks Obico's agent
protocol on one side and the Creator 5 LAN API on the other. Obico decides *whether* a print is
failing and what to do about it; the agent only feeds it status and camera frames, and carries
its pause/resume/cancel commands back to the printer.

Out of scope for this version: live WebRTC video in the Obico app (Obico's "premium streaming"
via Janus), G-code upload/start through Obico, the Obico tunnel to a local web UI, and any
detection logic of our own.

## 2. Environment (as found)

- Unraid host `HV-Unraid`, LAN 10.0.0.2, x86_64, Docker Compose v2.40, Docker Hub account
  `hananv` already logged in. An RTX 5060 Ti with the `nvidia` runtime exists but Obico's ML API
  currently runs on CPU; that is fine for one frame every ten seconds and is not changed here.
- Obico server checked out from source at `/mnt/user/appdata/obico-server`, four services
  (`web`, `tasks`, `ml_api`, `redis`) on the compose network `obico-server_default`. Web UI on
  port 3334. Secrets already live in that directory's `.env`.
- Printer at 10.0.0.10. LAN API on port 8898, documented in OrcaMCP's
  `docs/printers/flashforge-lan-api.md`, which is the source of truth for that side.
- Printer camera: MJPG-Streamer 0.2 at `http://10.0.0.10:8080/?action=stream`.
  Verified: it serves **one client at a time** (a second concurrent client gets an empty reply)
  and **has no snapshot action** (`?action=snapshot` returns an empty reply).
- Protocol source of truth for the Obico side: the `moonraker-obico` agent, specifically
  `server_conn.py` (transport), `printer.py` (`to_status()` payload shape), `app.py`
  (`process_server_msg`) and `utils.verify_link_code` (linking).

## 3. Architecture

```
                 LAN, plain HTTP                          compose network
┌──────────────┐  POST /detail, /control   ┌───────────────────┐  ws /ws/dev/ (status, commands)   ┌───────────┐
│ Creator 5 Pro│◄─────────────────────────►│  flashforge-obico │◄─────────────────────────────────►│ Obico web │
│  :8898       │                           │  (one container)  │  POST /api/v1/octo/pic/ (jpeg)    │  :3334    │
│  camera :8080│──── MJPEG (sole client) ─►│                   │  POST /api/v1/octo/printer_events/│           │
└──────────────┘                           │  re-serves camera │                                   └───────────┘
                                           │  :8081 /stream    │◄── OrcaSlicer, browser, anything
                                           └───────────────────┘
```

One process, one container, added as a fifth service to the existing Obico compose file. Threads,
not asyncio: the workload is three slow loops and the reference agent is threaded too.

### Modules (package `flashforge_obico`)

| Module | One responsibility | Depends on |
|---|---|---|
| `config.py` | Read and validate settings from environment variables. | nothing |
| `flashforge/client.py` | HTTP calls to the printer: `detail()`, `pause()`, `resume()`, `cancel()`. Credentials in every body, 15 s timeout, permissive integer parsing (`as_int`). | `config` |
| `flashforge/snapshot.py` | Turn a raw `detail` body into a typed `PrinterSnapshot` (state, file, progress, times, layers, temps, error code, camera URL). Tolerates missing or oddly-typed fields field-by-field. | nothing |
| `obico/status.py` | Build the Obico status dict from a `PrinterSnapshot` plus job-tracking state, in the exact shape of moonraker-obico's `to_status()`. | `snapshot` |
| `obico/server.py` | The Obico connection: websocket with reconnect/backoff, status sender, command receiver, REST posts for pictures and printer events, one-shot link-code exchange. | `config`, `status` |
| `camera/mjpeg_source.py` | Hold the single connection to a camera stream, parse multipart frames, keep the latest JPEG; reconnect on drop. | nothing |
| `camera/reserve.py` | Tiny HTTP server exposing `/stream` (multi-client MJPEG) and `/snapshot` (latest JPEG) from a source. | `mjpeg_source` |
| `job_tracker.py` | Decide print start/end from consecutive snapshots; own `current_print_ts`; emit `PrintStarted` / `PrintDone` / `PrintCancelled` / `PrintFailed` / `PrintPaused` / `PrintResumed`. | `snapshot` |
| `agent.py` | Wire the loops together: poll printer, update tracker, send status, post frames, execute commands. | all of the above |
| `__main__.py` | CLI: `run` (default) and `link <code>`. | `agent`, `server` |

Each module is testable without a network: the printer client and Obico connection take an
injectable HTTP/websocket layer, everything else is pure functions over dicts and dataclasses.

## 4. Behaviour

### 4.1 Printer polling

- Poll `detail` every **2 s** while the printer reports printing, heating, busy or paused; every
  **5 s** when idle; these are the cadences OrcaMCP uses and the machine tolerates.
- Every integer field goes through `as_int`. A field that cannot be parsed costs that field, not
  the poll. `status` is lowercased and `cancel` is mapped to `cancelled`. Unknown status values
  become `unknown` and are treated as *possibly printing*, never as idle.
- Three consecutive failed polls mark the printer **Offline** to Obico (an empty status dict, as
  the reference agent does). Offline is a connectivity fact, not a print failure; no command is
  ever sent because of it.

### 4.2 State mapping

| Flashforge `status` | Obico state text | Notes |
|---|---|---|
| `ready`, `completed`, `cancelled`, `error` (no active job) | `Operational` | `error` also sets `state.flags.error` and `state.error` from `errorCode`. |
| `printing`, `heating`, `busy` with a `printFileName` | `Printing` | Heating counts as printing so Obico's session starts at job start. |
| `paused` | `Paused` | |
| `unknown`, or `busy` without a file | keep previous state | Do not flip to Operational on an unfamiliar value mid-print. |
| unreachable (3 polls) | `Offline` | |

Payload fields filled from the printer: `progress.completion` (`printProgress × 100`),
`progress.printTime` (`printDuration`), `progress.printTimeLeft` (`estimatedTime`),
`job.file.name/path/display` (`printFileName`), `currentLayerHeight` (`printLayer`),
`file_metadata.obico.totalLayerCount` (`targetPrintLayer`), `temperatures.tool0..tool3`
(`nozzleTemps`/`nozzleTargetTemps`), `temperatures.bed` (`platTemp`/`platTargetTemp`),
`temperatures.chamber` (`chamberTemp`/`chamberTargetTemp`). Fields the printer cannot provide
(`currentZ`, feed/flow rate, fan) are sent as `null`, which the reference agent also does.

### 4.3 Job tracking and events

`current_print_ts` is how Obico identifies a print session. The tracker:

- sets `current_print_ts = int(now)` and emits `PrintStarted` when a snapshot first shows an
  active job (`printFileName` non-empty and state Printing/Paused) after none;
- emits `PrintPaused` / `PrintResumed` on Printing↔Paused transitions;
- emits `PrintDone` when the job ends with status `completed`, `PrintCancelled` on `cancelled`,
  `PrintFailed` on `error` or when the file simply disappears mid-progress (below 100 %), then sets
  `current_print_ts = -1`;
- treats a change of `printFileName` while active as end-of-old, start-of-new.

Status is sent to Obico immediately on any state or event change, otherwise at most every 30 s
(the reference agent's non-critical interval). The first message after (re)connect carries
`settings` (agent name/version, webcam list, platform).

### 4.4 Commands from Obico

Obico sends `{"commands": [{"cmd": "pause"|"resume"|"cancel"}]}` when either the user presses a
button in the app or, for `pause` only, when its detection fires and the printer's Obico setting
is "pause on failure". The agent:

- maps `pause` → `jobCtl_cmd pause`, `resume` → `jobCtl_cmd continue`, `cancel` →
  `jobCtl_cmd cancel`;
- is idempotent: `pause` while already paused and `resume` while printing are no-ops; a command
  with no active job is logged and dropped;
- **reads state back**: after sending, polls `detail` up to 5 times at 1 s and logs whether the
  printer actually changed state; if it did not, posts a `PRINTER_ERROR` printer event so the
  user is told the pause did not take;
- never issues `cancel` on its own initiative. `cancel` is executed only because a human pressed
  it in the Obico app; Obico's failure action is pause or notify, never cancel.

`{"remote_status": {"viewing": true}}` raises the snapshot rate to one frame per second for as
long as the app is open, matching the reference agent's "viewing boost".

### 4.5 Camera

- `MjpegSource` opens **one** persistent connection to the printer stream and keeps the latest
  frame. It is the only thing that ever talks to port 8080. On error it reconnects with backoff
  (1 s → 30 s).
- Every **10 s** (1 s under viewing boost) the agent posts the latest frame to
  `/api/v1/octo/pic/` with `is_primary_camera=true`, `camera_name`. Frames older than 15 s are not
  posted (stale camera is worse than no frame for the detector).
- The re-server on port **8081** exposes `/stream` (an MJPEG multipart to any number of clients)
  and `/snapshot` (the latest JPEG). This is what OrcaSlicer's device page and a browser should
  point at from now on, since the printer's own port refuses a second viewer.
- Cameras are a **list** in configuration; the first is primary and feeds detection. Adding the
  user's better camera later is a config change: a second `MjpegSource`, a second entry in the
  `settings.webcams` list sent to Obico, and a second `/stream` path. If it becomes the detection
  camera it moves to first position. No new modules.

### 4.6 Hard faults

A non-empty `errorCode` posts a `PRINTER_ERROR` printer event with the latest frame attached,
once per distinct code per run. Obico turns printer events into notifications.

### 4.7 Failure handling summary

| Failure | Behaviour |
|---|---|
| Printer unreachable | Offline to Obico after 3 polls; keep retrying; no commands. |
| Obico unreachable | Keep polling the printer; websocket reconnects with exponential backoff capped at 5 min; queued messages bounded at 50, oldest dropped. |
| Camera stream drops | Reconnect with backoff; stop posting frames until fresh ones exist. |
| Firmware returns a field in an unexpected type | That field is `null` for that poll; logged once at debug. |
| Pause command does not take | Printer event `PRINTER_ERROR` so the user is alerted. |
| Shared-token close code 4321 from Obico | Stop reconnecting and exit non-zero (another agent is using this token). |

### 4.8 Secrets and logging

- `FF_CHECK_CODE` and `OBICO_AUTH_TOKEN` are read from the environment only. A logging filter
  redacts both values from every log record. Request bodies are never logged.
- Logs go to stdout for Docker; default level INFO, `LOG_LEVEL` overrides.

## 5. Configuration

All via environment variables, so the compose file is the whole configuration:

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `FF_HOST` | yes | — | Printer IP or hostname. |
| `FF_SERIAL` | yes | — | Printer serial number. |
| `FF_CHECK_CODE` | yes | — | LAN check code (credential). |
| `OBICO_URL` | no | `http://web:3334` | Obico server base URL (compose-internal by default). |
| `OBICO_AUTH_TOKEN` | yes for `run` | — | Printer auth token from Obico. |
| `CAMERA_URLS` | no | printer's `cameraStreamUrl` from `detail` | Comma-separated MJPEG stream URLs; first is primary. |
| `CAMERA_NAMES` | no | `Printer` | Comma-separated names matching `CAMERA_URLS`. |
| `RESERVE_PORT` | no | `8081` | Port of the camera re-server; `0` disables it. |
| `LOG_LEVEL` | no | `INFO` | |

`flashforge-obico link <6-digit code>` posts the code to `/api/v1/octo/verify/` and prints the
returned auth token once, for pasting into `.env`. Nothing is written to disk by the agent.

## 6. Delivery

- **Repo:** `~/Projects/flashforge-obico`, Python ≥ 3.12, `pyproject.toml`, dependencies kept to
  `requests` and `websocket-client`. Tests with `pytest`.
- **Image:** `docker.io/hananv/flashforge-obico`, tags `latest` and the version. `python:3.12-slim`,
  non-root user, `linux/amd64` (Unraid's architecture), built and pushed from the Mac with
  `docker buildx` or on Unraid itself; the compose file pulls, it does not build.
- **Compose:** a `flashforge_agent` service appended to
  `/mnt/user/appdata/obico-server/docker-compose.yml`, `depends_on: web`, `restart:
  unless-stopped`, `ports: ["8081:8081"]`, log rotation as on the Lutronic container
  (`max-size 1m`, `max-file 5`), environment read from the existing `.env`. The snippet is kept in
  the repo under `deploy/` as the reference copy.
- **Obico side, one-time:** add a new printer in the Obico web UI, link it with the 6-digit code
  via `link`, set its failure action to "pause" and pick the notification channels. Optionally
  archive the old JGMaker printer.

## 7. Testing

- **Unit:** `as_int` and snapshot parsing against the real `detail` capture from the API doc
  plus deliberately mistyped variants (booleans, padded strings); state mapping table; job tracker
  transitions (start, pause/resume, done, cancelled, failed, file swap, unknown mid-print);
  status dict shape checked key-by-key against the reference; MJPEG chunker against a synthetic
  multipart body with the printer's boundary; redaction filter.
- **Integration (no hardware):** a fake Obico websocket + REST server and a fake printer HTTP
  server in-process; assert that a pause command from the fake server produces the exact
  `jobCtl_cmd` body, that the read-back happens, and that a viewing-boost message changes the
  frame cadence.
- **Live, opt-in:** a `live`-marked test that runs only when `FF_HOST`, `FF_SERIAL` and
  `FF_CHECK_CODE` are set: reads `detail`, grabs one camera frame, confirms the re-server serves it
  to two concurrent clients. It never sends job commands.
- **Acceptance on Unraid:** the container comes up healthy, the printer shows Operational in the
  Obico UI with a live-refreshing snapshot, a real print shows Printing with progress, and
  pressing Pause in the Obico app pauses the printer and the app reflects Paused within ~10 s.

## 8. Decisions taken

- **Direct Obico protocol, not a Moonraker emulator.** A fake Moonraker plus the stock client would
  reuse maintained code but emulate a far larger API surface and add a second layer to debug.
- **Agent owns the camera and re-serves it.** Forced by the single-client MJPG-Streamer; also
  gives OrcaSlicer a stable camera URL.
- **Snapshots only, no WebRTC.** Detection needs one frame per ten seconds; live video is a
  later, separate project.
- **Cancel is accepted but only ever human-initiated.** Obico never auto-cancels; refusing the
  app's Cancel button would surprise the user.
- **Environment-variable configuration.** Matches how the Obico compose stack is already configured.
