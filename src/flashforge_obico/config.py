"""Settings, read from environment variables so the compose file is the whole configuration."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

DEFAULT_OBICO_URL = "http://web:3334"
DEFAULT_RESERVE_PORT = 8081


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
    """Empty means: use the camera the printer reports in `detail`, named "Printer"."""
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


def _port(env: Mapping[str, str]) -> int:
    raw = env.get("RESERVE_PORT", str(DEFAULT_RESERVE_PORT)).strip()
    try:
        return int(raw)
    except ValueError:
        raise ConfigError("RESERVE_PORT must be an integer") from None


def load_config(env: Mapping[str, str], *, require_token: bool = True) -> Config:
    reserve_port = _port(env)
    return Config(
        ff_host=_required(env, "FF_HOST"),
        ff_serial=_required(env, "FF_SERIAL"),
        ff_check_code=_required(env, "FF_CHECK_CODE"),
        obico_url=(env.get("OBICO_URL", "").strip() or DEFAULT_OBICO_URL).rstrip("/"),
        obico_auth_token=_required(env, "OBICO_AUTH_TOKEN") if require_token else env.get("OBICO_AUTH_TOKEN", "").strip(),
        cameras=_cameras(env),
        reserve_port=reserve_port,
        public_host=_required(env, "PUBLIC_HOST") if reserve_port > 0 else env.get("PUBLIC_HOST", "").strip(),
        log_level=env.get("LOG_LEVEL", "").strip().upper() or "INFO",
    )
