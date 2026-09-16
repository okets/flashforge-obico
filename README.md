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

The re-server answers with `Access-Control-Allow-Origin: *`, like the printer's own camera server
does, so a browser page (OrcaSlicer's console) can read the stream and notice when it stops. It
carries no credential; anything on the LAN can already view the printer's camera, and this relay
keeps that boundary where it was. Do not expose port 8081 beyond the LAN.

## The phone console

The same port serves a small page for watching a print from a phone at home:

| URL | What |
|---|---|
| `http://<host>:8081/` | Live camera, state, progress, temperatures, material slots, **Pause / Resume / Light** |
| `http://<host>:8081/api/status` | The JSON behind it |
| `POST /api/job` `{"action": "pause"\|"resume"}` | Same retry / read-back / warm-up-deferral path as an Obico command |
| `POST /api/light` `{"on": true\|false}` | Chamber light |

There is deliberately no login: it is for the LAN (or the tailnet, which is the same thing), it
never touches heaters, and pausing a print is a nuisance if abused rather than a hazard. Cancel is
not offered here on purpose. Do not expose the port beyond the LAN.

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

## Install

You need a self-hosted [Obico server](https://github.com/TheSpaghettiDetective/obico-server)
running from its `docker-compose.yml`, a Creator 5 / 5 Pro in LAN mode, and the printer's
**serial number** and **check code** from its touchscreen (network / LAN-mode screen).

1. **Register the printer in Obico.** In the Obico web UI add a printer and start the "link
   printer" flow to get a 6-digit code, or create it from the server's Django shell and copy its
   `auth_token`. You can exchange a 6-digit code for the token with the agent itself once the
   service exists (step 4).

2. **Add the service.** Append the block from
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

3. **Add the secrets to Obico's `.env`** (same directory as the compose file):

   ```
   FF_HOST=<printer IP>
   FF_SERIAL=<printer serial>
   FF_CHECK_CODE=<printer check code>
   OBICO_AUTH_TOKEN=<printer token from step 1>
   PUBLIC_HOST=<this server's LAN IP>
   ```

4. **Start it** and watch for `connected to Obico`:

   ```bash
   docker compose pull flashforge_agent
   docker compose up -d flashforge_agent
   docker compose logs -f flashforge_agent
   ```

   If you only have a 6-digit link code, get the token with
   `docker compose run --rm flashforge_agent link 123456`, put it in `.env`, and start again.

5. **Open the phone console** at `http://<this server's LAN IP>:8081/` and, in Obico, set the
   printer's failure action (it defaults to *pause the printer and notify me*).

To upgrade, `docker compose pull flashforge_agent && docker compose up -d flashforge_agent`.
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

## Deployed at

- Host `HV-Unraid` (10.0.0.2), service `flashforge_agent` in `/mnt/user/appdata/obico-server/docker-compose.yml`,
  secrets in that directory's `.env`. Camera re-server on `http://10.0.0.2:8081/cameras/0/stream`.
- Image `docker.io/hananv/flashforge-obico` is built on the Unraid host from a `git archive` of this repo
  unpacked into `/mnt/user/appdata/flashforge-obico/build` (the Mac has no Docker daemon running):

  ```bash
  git archive HEAD | ssh root@HV-Unraid 'tar -x -C /mnt/user/appdata/flashforge-obico/build && \
    cd /mnt/user/appdata/flashforge-obico/build && \
    docker build -t docker.io/hananv/flashforge-obico:latest . && docker push docker.io/hananv/flashforge-obico:latest'
  ssh root@HV-Unraid 'cd /mnt/user/appdata/obico-server && docker compose pull flashforge_agent && docker compose up -d flashforge_agent'
  ```
- Obico printer "Creator 5 Pro" (id 2), created from the Django shell, failure action *pause*.
