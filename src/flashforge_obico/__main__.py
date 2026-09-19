"""Command line: `flashforge-obico run` (default) or `flashforge-obico link <code>`."""
from __future__ import annotations

import argparse
import importlib.resources
import json
import urllib.parse
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
from .flashforge.client import FlashforgeClient
from .logging_utils import configure_logging
from .obico.server import ObicoError, ObicoServer, verify_link_code

_logger = logging.getLogger("flashforge_obico")


def run(config: Config) -> int:
    _logger.info("flashforge-obico %s starting: printer %s, Obico %s", VERSION, config.ff_host, config.obico_url)
    printer = FlashforgeClient(config.ff_host, config.ff_serial, config.ff_check_code)
    # Configured cameras start now; the printer's own camera is discovered by the agent from the
    # first successful poll, so a printer that is still off at boot is not a problem.
    cameras = [MjpegSource(camera.stream_url, name=camera.name) for camera in config.cameras]
    reserver = CameraReserver(cameras, config.reserve_port) if config.reserve_port > 0 else None
    holder: dict[str, Agent] = {}
    obico = ObicoServer(config.obico_url, config.obico_auth_token,
                        on_message=lambda message: holder["agent"].handle_server_message(message))
    agent = holder["agent"] = Agent(config, printer, obico, cameras)
    agent.reserver = reserver
    if reserver:
        _mount_console(reserver, agent)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    for camera in cameras:
        camera.start()
    if reserver:
        reserver.start()
    obico.start()
    try:
        return agent.run(stop)
    finally:
        obico.stop()
        for camera in agent.cameras:
            camera.stop()
        if reserver:
            reserver.stop()


def _mount_console(server: CameraReserver, agent: Agent) -> None:
    """The phone page: the console HTML at /, its status at /api/status, pause/resume at /api/job,
    the stored files at /api/files and starting one at /api/print.

    LAN-only by design (see README), and still no login. Pause and the light stay harmless, but
    /api/print starts a real job on a real machine, so the guard in Agent.start_print carries the
    weight the absent login would: idle machine only, and only a name the printer itself listed."""
    page = importlib.resources.files("flashforge_obico.console").joinpath("page.html").read_bytes()
    server.add_route("GET", r"^/$", lambda match, body: (200, "text/html; charset=utf-8", page))
    server.add_route("GET", r"^/api/status$",
                     lambda match, body: (200, "application/json", json.dumps(agent.console_status()).encode()))

    def job(match, body: bytes):
        try:
            action = json.loads(body or b"{}").get("action")
        except ValueError:
            action = None
        if not isinstance(action, str) or not agent.request_command(action):
            return 400, "application/json", b'{"error": "action must be pause or resume"}'
        return 202, "application/json", json.dumps({"accepted": action}).encode()

    server.add_route("POST", r"^/api/job$", job)

    def light(match, body: bytes):
        try:
            on = json.loads(body or b"{}").get("on")
        except ValueError:
            on = None
        if not isinstance(on, bool):
            return 400, "application/json", b'{"error": "on must be true or false"}'
        ok = agent.set_light(on)
        return (200 if ok else 502), "application/json", json.dumps({"light_on": on if ok else None}).encode()

    server.add_route("POST", r"^/api/light$", light)

    server.add_route("GET", r"^/api/files$",
                     lambda match, body: (200, "application/json", json.dumps({"files": agent.files()}).encode()))

    def thumbnail(match, body: bytes):
        name = urllib.parse.unquote(match.group(1))
        png = agent.thumbnail(name)
        if png is None:
            return 404, "application/json", b'{"error": "no thumbnail"}'
        return 200, "image/png", png

    server.add_route("GET", r"^/api/files/(.+)/thumbnail$", thumbnail)

    def start_print(match, body: bytes):
        try:
            name = json.loads(body or b"{}").get("file")
        except ValueError:
            name = None
        if not isinstance(name, str):
            return 400, "application/json", b'{"error": "file must be a name from /api/files"}'
        refusal = agent.start_print(name)
        if refusal is None:
            return 202, "application/json", json.dumps({"started": name}).encode()
        return 409, "application/json", json.dumps({"error": refusal.value}).encode()

    server.add_route("POST", r"^/api/print$", start_print)


def link(config: Config, code: str) -> int:
    try:
        token = verify_link_code(config.obico_url, code)
    except (ObicoError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("Linked. Add this line to the Obico .env and restart the agent:")
    print(f"OBICO_AUTH_TOKEN={token}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flashforge-obico",
                                     description="Obico agent for the FlashForge Creator 5 / 5 Pro")
    parser.add_argument("--version", action="version", version=VERSION)
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
