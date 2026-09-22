# flashforge-obico

An [Obico](https://www.obico.io/) agent for the FlashForge Creator 5 / 5 Pro in LAN-only mode.

**Image:** [`hananv/flashforge-obico` on Docker Hub](https://hub.docker.com/r/hananv/flashforge-obico) ·
**Source:** [github.com/okets/flashforge-obico](https://github.com/okets/flashforge-obico)

It polls the printer's LAN API, reports status and print events to a self-hosted Obico server,
posts a camera frame every ten seconds for failure detection, and carries Obico's pause, resume and
cancel commands back to the printer. Obico decides whether a print is failing and what to do about
it; this agent is only the bridge.

Design: [docs/superpowers/specs/2026-09-15-flashforge-obico-agent-design.md](docs/superpowers/specs/2026-09-15-flashforge-obico-agent-design.md).
Printer protocol: `docs/printers/flashforge-lan-api.md` in [OrcaMCP](https://github.com/okets/OrcaMCP).

## The camera is single-client

The printer's built-in MJPG-Streamer serves **one viewer at a time** and has no snapshot endpoint.
This agent is that one viewer. It re-serves every camera it holds, to any number of clients, on
port 8081:

| URL | What |
|---|---|
| `http://<host>:8081/cameras/0/stream` | MJPEG stream of camera 0 (the primary, used for detection) |
| `http://<host>:8081/cameras/0/snapshot` | Latest JPEG of camera 0 |
| `http://<host>:8081/healthz` | `ok` |

Point OrcaSlicer, browsers and anything else at these, never at the printer's port 8080 while the
agent runs. The agent also publishes these URLs to Obico in the printer's webcam list
(`settings.webcams[i].stream_url`), so a client that only knows the Obico server can discover them.

The slot is only free once the viewer's connection goes away, so the agent hangs up on the camera
the moment it is asked to stop. Kill it outright instead -- `docker kill`, or a host that loses
power -- and the printer goes on holding a client that is no longer there: every reconnect is
refused with `Connection reset by peer` until the firmware's own TCP timeout expires, which took
two hours and twenty minutes the one time it happened here. Nothing restarts that faster, so the
console says why the picture is missing instead of showing a broken image, and `/api/status`
carries the same in `cameras[i].last_error`.

The re-server answers with `Access-Control-Allow-Origin: *`, like the printer's own camera server
does, so a browser page (OrcaSlicer's console) can read the stream and notice when it stops. It
carries no credential; anything on the LAN can already view the printer's camera, and this relay
keeps that boundary where it was. Do not expose port 8081 beyond the LAN.

## The phone console

The same port serves a small page for watching a print from a phone at home:

| URL | What |
|---|---|
| `http://<host>:8081/` | Live camera, state, progress, temperatures, material slots, the stored files, **Pause / Resume / Light / Print** |
| `http://<host>:8081/api/status` | The JSON behind it |
| `POST /api/job` `{"action": "pause"\|"resume"}` | Same retry / read-back / warm-up-deferral path as an Obico command |
| `POST /api/light` `{"on": true\|false}` | Chamber light |
| `GET /api/files` | The files stored on the printer, with print time, filament weight and per-tool materials |
| `GET /api/files/<name>/thumbnail` | That file's own thumbnail, as a PNG |
| `POST /api/print` `{"file": "<name>"}` | Start a stored file. 409 with `not_ready` or `unknown_file` when refused |

There is deliberately no login: it is for the LAN (or the tailnet, which is the same thing). That
was easy to argue while the page only paused a print and never touched a heater. `/api/print` is
not in that class -- it starts a real job on a real machine -- so the guard stands in for the
missing login: the printer must be idle, and the file name must be one the printer itself just
listed, re-read on every request rather than trusted from the page. The button needs two taps. A
job already running can only be paused or resumed, never replaced. Cancel is still not offered
here on purpose. Do not expose the port beyond the LAN.

## Configuration

Everything is an environment variable.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `FF_HOST` | yes | | Printer IP or hostname |
| `FF_SERIAL` | yes | | Printer serial number |
| `FF_CHECK_CODE` | yes | | LAN check code. **A credential**: never logged, never committed |
| `OBICO_URL` | no | `http://web:3334` | Obico server base URL (the compose-internal name by default) |
| `OBICO_AUTH_TOKEN` | for `run` | | The printer's Obico auth token. Also a credential |
| `CAMERA_URLS` | no | printer's own camera | Comma-separated MJPEG stream URLs; first is primary |
| `CAMERA_NAMES` | no | `Printer`, `Camera 2`… | Comma-separated names matching `CAMERA_URLS` |
| `RESERVE_PORT` | no | `8081` | Camera re-server port; `0` disables it |
| `PUBLIC_HOST` | if re-server on | | Host or IP that LAN clients use to reach the re-server |
| `LOG_LEVEL` | no | `INFO` | |

## How it fits together

```
 your LAN
 ┌──────────────────┐   HTTP, LAN API       ┌────────────────────┐   websocket + HTTP   ┌──────────────┐
 │ Creator 5 / 5 Pro│◄─────────────────────►│  flashforge-obico  │◄────────────────────►│ Obico server │
 │ LAN mode         │   camera (1 viewer)   │  (this container)  │   status, pictures,  │ (self-hosted)│
 │ :8898  :8080     │──────────────────────►│  re-serves camera  │   pause/resume       │ :3334        │
 └──────────────────┘                       │  phone console :8081│                      └──────┬───────┘
                                            └─────────┬──────────┘                             │
                                                      │ full-rate video, pause, light           │ app, web, alerts
                                                      ▼                                         ▼
                                                 your phone / OrcaMCP                   Obico app / Telegram / e-mail
```

The agent is the only thing that talks to the printer. It polls the printer's LAN API, holds the
printer's single-viewer camera stream, posts a frame to Obico every ten seconds, tells Obico when a
print starts, pauses, resumes or ends, and carries Obico's pause/resume back to the printer. Obico
does the failure detection and the notifications. Nothing here needs the cloud.

## Before you start

### The printer

1. **Switch the printer to LAN mode** on its touchscreen (network settings). Cloud features stop
   working; that is the point of this project.
2. **Note the serial number and the check code** shown on the same screen. The check code is the
   printer's only credential: it grants full control, heaters included. Keep it out of chats,
   screenshots and commits.
3. **Give the printer a fixed address**: a DHCP reservation on your router. The agent reconnects by
   address, and a printer that changes IP after a power cut silently disappears.
4. **Check it answers** from the machine that will run the agent (replace the placeholders):

   ```bash
   curl -s -X POST http://<printer-ip>:8898/detail -H "Content-Type: application/json" \
     -d '{"serialNumber":"<serial>","checkCode":"<check code>"}' | head -c 300
   ```

   You should see JSON with `"status": "ready"`. The printer's camera is at
   `http://<printer-ip>:8080/?action=stream` and accepts **one viewer at a time**; once the agent
   runs, close Flash Print's camera view and point everything else at the agent's port 8081.

### The Obico server

Install the self-hosted Obico server if you do not have one: follow
[Obico's server guide](https://www.obico.io/docs/server-guides/) (it is a `git clone` plus
`docker compose up -d`, usually on the same home server). Then:

1. Open `http://<server-ip>:3334`, create the first account and log in.
2. In `.env` of the Obico checkout, set your e-mail and, if you want them, Telegram or other
   notification channels, exactly as Obico's guide describes; the agent needs nothing there.
3. Install the Obico mobile app and point it at your server address. From home the LAN address
   works; from anywhere else the simplest route is Tailscale on both the server and the phone
   (a home connection behind carrier-grade NAT cannot forward ports at all).
4. Register the printer so it gets an **auth token**. Either use the app's "link printer" flow,
   which shows a 6-digit code you exchange in step 4 of the install below, or create it directly
   in the server's shell and copy the token it prints:

   ```bash
   cd <obico checkout>
   docker compose exec web python manage.py shell -c "
   from secrets import token_hex
   from app.models import Printer, User
   u = User.objects.get(email='<your obico login email>')
   p = Printer.objects.create(name='Creator 5 Pro', user=u, auth_token=token_hex(16))
   print(p.auth_token)"
   ```

   The token is a credential too: whoever has it can pause and see your printer.

## Install the agent

1. **Add the service.** Append the block from
   [deploy/obico-compose.snippet.yml](deploy/obico-compose.snippet.yml) under `services:` in
   Obico's `docker-compose.yml`. It pulls the published image, so nothing is built:

   ```yaml
     flashforge_agent:
       image: docker.io/hananv/flashforge-obico:latest
       restart: unless-stopped
       depends_on: [web]
       ports: ["8081:8081"]
       environment:
         FF_HOST: '${FF_HOST-10.0.0.10}'
         FF_SERIAL: '${FF_SERIAL}'
         FF_CHECK_CODE: '${FF_CHECK_CODE}'
         OBICO_URL: 'http://web:3334'
         OBICO_AUTH_TOKEN: '${OBICO_AUTH_TOKEN}'
         PUBLIC_HOST: '${PUBLIC_HOST-10.0.0.2}'
         RESERVE_PORT: '8081'
   ```

   `OBICO_URL` uses the compose-internal name `web`, so the agent reaches Obico without touching
   the network at all; `PUBLIC_HOST` is the address your phone and slicer use to reach this server.

2. **Add the secrets to Obico's `.env`** (same directory as the compose file):

   ```
   FF_HOST=<printer IP>
   FF_SERIAL=<printer serial>
   FF_CHECK_CODE=<printer check code>
   OBICO_AUTH_TOKEN=<printer token>
   PUBLIC_HOST=<this server's LAN IP>
   ```

3. **Start it** and watch for `connected to Obico` and `camera discovered from the printer`:

   ```bash
   docker compose pull flashforge_agent
   docker compose up -d flashforge_agent
   docker compose logs -f flashforge_agent
   ```

4. **Only if you used a 6-digit link code**: exchange it for the token, put the token in `.env`,
   and start again:

   ```bash
   docker compose run --rm flashforge_agent link 123456
   ```

5. **Check the result.** In Obico the printer shows *Operational* with a picture that refreshes.
   The phone console is at `http://<this server's LAN IP>:8081/`. In Obico's printer settings,
   leave the failure action on *pause the printer and notify me* or change it to notify only.
   If you use OrcaMCP, put the same server URL and token into the printer's connection dialog and
   its Device tab shows the camera and Obico's watch state.

To upgrade: `docker compose pull flashforge_agent && docker compose up -d flashforge_agent`.
Tags on Docker Hub follow the version in `pyproject.toml`; `latest` is the newest.

### Building the image yourself

```bash
docker buildx build --platform linux/amd64 -t docker.io/<you>/flashforge-obico:latest --push .
```

## Behaviour worth knowing

- Poll cadence is 2 s while printing or paused, 5 s when idle. Three failed polls in a row mark the
  printer Offline in Obico. A network drop never triggers a command.
- Commands are idempotent and read back: after a pause the agent polls until the printer reports
  paused; if it never does, Obico gets a printer event so you are told.
- The agent never cancels on its own. Cancel only happens when you press it in the Obico app.
- Pictures from secondary cameras are not posted; this Obico version stores only the primary
  camera's. Secondary cameras are still re-served on `/cameras/<i>/stream`.

## Development

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/pytest -q                     # no network needed
FF_HOST=… FF_SERIAL=… FF_CHECK_CODE=… .venv/bin/pytest -q -m live   # against the real printer
```

The live test reads status and grabs a camera frame. It never sends job commands.

## License

MIT, see [LICENSE](LICENSE).
