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


def _tool(entry) -> dict | None:
    """One entry of `gcodeToolDatas`. None when it carries no material worth showing."""
    if not isinstance(entry, dict):
        return None
    material = as_str(entry.get("materialName"))
    color = as_str(entry.get("materialColor"))
    if not material and not color:
        return None
    return {
        "tool_id": as_int(entry.get("toolId")),
        "slot_id": as_int(entry.get("slotId")),
        "material": material,
        "color": color,
        "weight_g": as_float(entry.get("filamentWeight")),
    }


def parse_gcode_files(body) -> list[dict]:
    """The stored files, from a `gcodeList` reply.

    The firmware answers with two parallel lists: `gcodeList` of bare names, and an undocumented
    `gcodeListDetail` of objects carrying the print time, the filament weight and a per-tool
    material. Either can be absent -- older firmware returns only the names -- so the names drive
    the order and the detail is merged in where it matches. Anything unreadable is skipped rather
    than failing the call, which is how the rest of this module treats the printer's output.
    """
    if not isinstance(body, dict):
        return []

    detail_by_name: dict[str, dict] = {}
    detail = body.get("gcodeListDetail")
    if isinstance(detail, list):
        for entry in detail:
            if not isinstance(entry, dict):
                continue
            name = as_str(entry.get("gcodeFileName"))
            if name:
                detail_by_name.setdefault(name, entry)

    names = [name for name in as_str_list(body.get("gcodeList")) if name]
    for name in detail_by_name:  # detail-only firmware still yields its files, in reply order
        if name not in names:
            names.append(name)

    files = []
    for name in names:
        entry = detail_by_name.get(name, {})
        tools = [tool for tool in (_tool(item) for item in entry.get("gcodeToolDatas", []) or []) if tool]
        files.append({
            "name": name,
            "printing_time_s": as_int(entry.get("printingTime")),
            "filament_weight_g": as_float(entry.get("totalFilamentWeight")),
            "tool_count": as_int(entry.get("gcodeToolCnt")),
            "uses_material_station": bool(entry.get("useMatlStation")),
            "tools": tools,
        })
    return files
