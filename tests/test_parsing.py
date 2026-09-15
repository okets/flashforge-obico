import pytest

from flashforge_obico.flashforge.parsing import as_float, as_int, as_str, as_str_list


@pytest.mark.parametrize("value,expected", [
    (5, 5), (True, 1), (False, 0), (5.0, 5), (" 7 ", 7), ("-3", -3), ("+4", 4),
    ("5.0", None), ("+-5", None), ("", None), (None, None), ([], None), (5.5, None), ("abc", None),
])
def test_as_int(value, expected):
    assert as_int(value) == expected


@pytest.mark.parametrize("value,expected", [
    (1, 1.0), (0.5, 0.5), ("0.25", 0.25), (True, None), ("x", None), (None, None),
])
def test_as_float(value, expected):
    assert as_float(value) == expected


def test_as_str():
    assert as_str("ready") == "ready"
    assert as_str(None) == ""
    assert as_str(3) == ""


def test_as_str_list():
    assert as_str_list(["a", 1, None]) == ["a"]
    assert as_str_list("nope") == []
