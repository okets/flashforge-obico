import pytest

from flashforge_obico.config import CameraConfig, ConfigError, load_config

BASE = {"FF_HOST": "10.0.0.10", "FF_SERIAL": "SN", "FF_CHECK_CODE": "CC", "OBICO_AUTH_TOKEN": "TK",
        "PUBLIC_HOST": "10.0.0.2"}


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
    env = dict(BASE)
    del env[missing]
    with pytest.raises(ConfigError, match=missing):
        load_config(env)


def test_token_optional_for_link_and_public_host_optional_when_reserver_off():
    env = dict(BASE)
    del env["OBICO_AUTH_TOKEN"]
    del env["PUBLIC_HOST"]
    env["RESERVE_PORT"] = "0"
    c = load_config(env, require_token=False)
    assert c.obico_auth_token == "" and c.reserve_port == 0


def test_bad_port():
    with pytest.raises(ConfigError, match="RESERVE_PORT"):
        load_config({**BASE, "RESERVE_PORT": "eighty"})


def test_obico_url_trailing_slash_stripped():
    assert load_config({**BASE, "OBICO_URL": "http://x:3334/"}).obico_url == "http://x:3334"
