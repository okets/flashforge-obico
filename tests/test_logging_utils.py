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
