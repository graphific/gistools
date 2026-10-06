"""Years and attribute values on a building footprint."""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

WARNING = (
    "Footprints are unverified. A missing footprint is not proof there is no building. "
    "Height left above a roof is not proof of a tree. strict removes overhang; canopy keeps it."
)


def year(value):
    text = str(value)
    if len(text) == 4 and text.isdigit() and 1800 <= int(text) <= 2200:
        return int(text)
    return date.fromisoformat(text).year


def snapshot_year(path, context):
    if "snapshot_year" in context:
        return year(context["snapshot_year"])
    match = re.search(r"__(?:asof-)?(\d{4}-\d{2}-\d{2})__", path.name)
    return year(match[1]) if match else None


def optional_year(value):
    if value is None or str(value).lower() in {"", "none", "nan", "nat"}:
        return None
    text = str(value).removesuffix(".0")
    if re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", text):
        return int(text[:4])
    return None


def field_value(fields, index, key):
    """Read explicit columns first, then retained OSM hstore tags and lifecycle aliases."""
    keys = {"construction_year": ("construction_year", "start_date"),
            "demolition_year": ("demolition_year", "end_date")}.get(key, (key,))
    for name in keys:
        if name in fields and fields[name][index] is not None and str(fields[name][index]).lower() not in {"nan", "nat", ""}:
            return fields[name][index]
        raw = fields.get("other_tags")
        if raw is not None:
            match = re.search(r'"' + re.escape(name) + r'"=>\s*("(?:[^"\\]|\\.)*")', str(raw[index]))
            if match:
                return json.loads(match[1])
    return None


def number(value):
    import math

    if value is None:
        return None
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(m|ft)?\s*", value)
        if match:
            return float(match[1]) * (0.3048 if match[2] == "ft" else 1)
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise ValueError(f"Building height must be numeric metres, received {value!r}") from None
    return result if math.isfinite(result) else None
