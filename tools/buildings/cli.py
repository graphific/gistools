"""Flags shared by chm and crowns."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

def arguments(parser):
    parser.add_argument("--building-mode", choices=("canopy", "strict"), default=None,
                        help="canopy removes roof-height pixels and keeps overhang; strict clears every footprint pixel")
    parser.add_argument("--date", help="year, ISO date, or today. Does not change the image date")
    parser.add_argument("--building-context", type=Path, help="JSON: snapshot year, field names, clearance_m, max_stem_shift_m")
    parser.add_argument("--building-time-policy", choices=("recent", "evidence"), default=None,
                        help="recent treats a snapshot within recent_years as present; evidence requires construction and demolition years")


def options(args):
    target = getattr(args, "date", None)
    return {"mode": getattr(args, "building_mode", None) or "canopy",
            "date": datetime.now().astimezone().date().isoformat() if target == "today" else target,
            "context": getattr(args, "building_context", None),
            "cache": str((args.out / "inputs").resolve()),
            "temporal_policy": getattr(args, "building_time_policy", None) or "recent"}
