import pytest

from flashforge_obico.flashforge.client import FlashforgeClient, FlashforgeError


class FakeTransport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, url, body):
        self.calls.append((url, body))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def make(replies):
    transport = FakeTransport(replies)
    return FlashforgeClient("10.0.0.10", "SN123", "CODE", transport=transport), transport


def test_detail_unwraps_nested_payload(detail):
    client, transport = make([{"code": 0, "message": "Success", "detail": detail}])
    assert client.detail()["status"] == "ready"
    url, body = transport.calls[0]
    assert url == "http://10.0.0.10:8898/detail"
    assert body == {"serialNumber": "SN123", "checkCode": "CODE"}


def test_detail_accepts_top_level_payload(detail):
    detail["code"] = " 0 "
    client, _ = make([detail])
    assert client.detail()["status"] == "ready"


def test_nonzero_code_or_err_is_an_error():
    client, _ = make([{"code": 1, "message": "bad"}, {"err": "2", "msg": "worse"}])
    with pytest.raises(FlashforgeError, match="bad"):
        client.detail()
    with pytest.raises(FlashforgeError, match="worse"):
        client.detail()


def test_reply_without_code_is_an_error():
    client, _ = make([{"status": "ready"}, "not json object"])
    with pytest.raises(FlashforgeError, match="result code"):
        client.detail()
    with pytest.raises(FlashforgeError, match="JSON object"):
        client.detail()


def test_transport_failure_is_wrapped():
    client, _ = make([ConnectionError("boom")])
    with pytest.raises(FlashforgeError, match="boom"):
        client.detail()


@pytest.mark.parametrize("method,action", [("pause", "pause"), ("resume", "continue"), ("cancel", "cancel")])
def test_job_commands(method, action):
    client, transport = make([{"code": 0}])
    getattr(client, method)()
    url, body = transport.calls[0]
    assert url == "http://10.0.0.10:8898/control"
    assert body["payload"] == {"cmd": "jobCtl_cmd", "args": {"jobID": "", "action": action}}
    assert body["serialNumber"] == "SN123" and body["checkCode"] == "CODE"


@pytest.mark.parametrize("on,status", [(True, "open"), (False, "close")])
def test_light_command(on, status):
    client, transport = make([{"code": 0}])
    client.set_light(on)
    assert transport.calls[0][1]["payload"] == {"cmd": "lightControl_cmd", "args": {"status": status}}


def test_error_message_never_contains_the_check_code():
    client, _ = make([{"code": 5, "message": "refused"}, ValueError("body was {'checkCode': 'CODE'}")])
    with pytest.raises(FlashforgeError) as first:
        client.detail()
    assert "CODE" not in str(first.value)
    with pytest.raises(FlashforgeError) as second:
        client.detail()
    assert "CODE" not in str(second.value)
