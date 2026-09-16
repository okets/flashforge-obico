"""Command line: `flashforge-obico run` (default) or `flashforge-obico link <code>`."""
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
