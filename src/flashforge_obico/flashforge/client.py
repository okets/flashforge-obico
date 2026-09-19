"""HTTP calls to the Creator 5 LAN API.

Every request is a POST of a JSON object carrying the credentials in its body. Success is signalled
by a zero result code in the body, not by the HTTP status.
"""
from __future__ import annotations

import base64
import binascii

from typing import Callable

import requests

from .parsing import as_int, as_str, parse_gcode_files

Transport = Callable[[str, dict], dict]
"""(url, json body) -> parsed JSON body. Raises on any transport problem."""

DEFAULT_PORT = 8898
DEFAULT_TIMEOUT_S = 15.0


class FlashforgeError(Exception):
    """The printer refused, or could not be reached. The message never carries credentials."""


def requests_transport(timeout: float = DEFAULT_TIMEOUT_S) -> Transport:
    def post(url: str, body: dict) -> dict:
        response = requests.post(url, json=body, timeout=timeout)
        response.raise_for_status()
        return response.json()

    return post


class FlashforgeClient:
    def __init__(self, host: str, serial: str, check_code: str, *,
                 transport: Transport | None = None, port: int = DEFAULT_PORT):
        self._base = f"http://{host}:{port}"
        self._credentials = {"serialNumber": serial, "checkCode": check_code}
        self._check_code = check_code
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

    def set_light(self, on: bool) -> None:
        self._post("control", {"payload": {"cmd": "lightControl_cmd", "args": {"status": "open" if on else "close"}}})

    def gcode_files(self) -> list[dict]:
        """The files stored on the printer, with the per-file detail the firmware ships alongside."""
        return parse_gcode_files(self._post("gcodeList", {}))

    def gcode_thumbnail(self, file_name: str) -> bytes | None:
        """The file's own thumbnail PNG, or None when the printer has nothing usable for it.

        Undocumented, like `gcodeListDetail`: `gcodeThumb` answers with the image base64-encoded in
        `imageData`. The magic number is checked because the field is the printer's word, not ours,
        and a page that serves whatever arrives as an image is a page that serves anything.
        """
        data = self._post("gcodeThumb", {"fileName": file_name}).get("imageData")
        if not isinstance(data, str) or not data:
            return None
        try:
            raw = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error):
            return None
        return raw if raw.startswith(b"\x89PNG\r\n\x1a\n") else None

    def print_gcode(self, file_name: str, *, leveling: bool = False,
                    material_mappings: list | None = None) -> None:
        """Start a stored file. Every field is sent because the firmware reads them all."""
        mappings = material_mappings or []
        self._post("printGcode", {
            "fileName": file_name,
            "levelingBeforePrint": leveling,
            "flowCalibration": False,
            "useMatlStation": bool(mappings),
            "gcodeToolCnt": len(mappings),
            "materialMappings": mappings,
        })

    def _job_command(self, action: str) -> None:
        self._post("control", {"payload": {"cmd": "jobCtl_cmd", "args": {"jobID": "", "action": action}}})

    def _post(self, endpoint: str, extra: dict) -> dict:
        try:
            body = self._transport(f"{self._base}/{endpoint}", {**self._credentials, **extra})
        except Exception as exc:
            raise FlashforgeError(f"{endpoint}: {type(exc).__name__}: {self._scrub(str(exc))}") from None
        if not isinstance(body, dict):
            raise FlashforgeError(f"{endpoint}: reply is not a JSON object")
        code = as_int(body.get("code", body.get("err")))
        if code is None:
            raise FlashforgeError(f"{endpoint}: reply has no result code")
        if code != 0:
            message = as_str(body.get("message")) or as_str(body.get("msg")) or "no message"
            raise FlashforgeError(f"{endpoint}: printer returned {code}: {self._scrub(message)}")
        return body

    def _scrub(self, text: str) -> str:
        return text.replace(self._check_code, "<redacted>") if self._check_code else text
