"""Permissive readers for the Creator 5 LAN API.

The firmware types the same field differently across revisions: a number on one, a boolean on
another, a space-padded decimal string on a third. A value that is none of the accepted shapes
reads as None, so the caller loses one field and never the whole poll.
"""
from __future__ import annotations


def as_int(value) -> int | None:
    if isinstance(value, bool):  # bool before int: bool subclasses int
        return 1 if value else 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip(), 10)
        except ValueError:
            return None
    return None


def as_float(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def as_str(value) -> str:
    return value if isinstance(value, str) else ""


def as_str_list(value) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []
