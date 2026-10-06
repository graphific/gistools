"""Tree tops, trunks and crowns from a canopy-height raster or LiDAR points.

    from tools.crowns import run, delineate, Rule
    run(chm="chm.tif", out="out/")
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from .canopy import SPARSE, Grid, canopy_of_points, on_grid, open_canopy, read_canopy
from .delineate import Rule, delineate
from .io import (
    CANOPY, FIELDS, FORMATS, NUMBERS, clear_away, find_trees, make_way, restore, write_tif, write_vectors,
)
from .plot import figure
from .qgis import write_qgis
from .terrain import fetched_terrain
from .validate import against, read_reference, summary

__all__ = ["Rule", "delineate", "run"]


def run(chm=None, points=None, out=None, *, crowns="watershed", min_height=2.0, smooth=0.5,
        window=(1.0, 0.035), stands=(0.0, 0.0), floor=0.5, min_area=2.0, stem_share=0.15,
        outline="simplified", format="both", dem="auto", dem_dir=None, pixel=None, normalised=False,
        crs=None, validate=None, tile=4096, no_plot=False, command=None):
    """Write tops, trunks and crowns for a canopy raster or LAS/LAZ files. Returns the file names written."""
    if not out or (chm is None) == (points is None):
        raise ValueError("pass out, and either chm or points")
    if chm is not None and (pixel or normalised or crs):
        raise ValueError("pixel, normalised and crs are for points")
    if not 0 <= stem_share <= 1:
        raise ValueError("stem_share is between 0 and 1")
    if dem_dir and dem != "auto":
        raise ValueError("dem_dir is only used with dem='auto'")
    if command is None:
        command = ""
    a = SimpleNamespace(
        chm=Path(chm) if chm else None,
        points=[Path(p) for p in points] if points else None,
        out=Path(out),
        crowns=crowns,
        min_height=min_height,
        smooth=smooth,
        window=tuple(window),
        stands=tuple(stands),
        floor=floor,
        min_area=min_area,
        stem_share=stem_share,
        outline=outline,
        format=format,
        dem=dem if dem in ("auto", "none") else Path(dem),
        dem_dir=dem_dir,
        pixel=pixel,
        normalised=normalised,
        crs=crs,
        validate=Path(validate) if validate else None,
        tile=tile,
        no_plot=no_plot,
    )
    for given in (a.chm, a.dem if isinstance(a.dem, Path) else None, a.validate, *(a.points or ())):
        if given is not None and not given.exists():
            raise ValueError(f"{given} is not there")
    aside: list[Path] = []
    try:
        written = produce(a, command, aside)
    except BaseException:
        restore(aside)
        raise
    clear_away(a.out, aside, written)
    return written


def produce(a, command: str, aside: list[Path]) -> set[str]:
    """Everything a run reads and writes; the names of the files it wrote. `aside` takes the names of an earlier
    run's data files as soon as they are moved, so that the caller can put them back if this run does not end."""
    import rasterio

    def say(text: str) -> None:
        print(text, flush=True)

    rule = Rule(crowns=a.crowns, min_height=a.min_height, smooth=a.smooth, window=a.window[0], window_share=a.window[1], stands=a.stands[0],
                stands_share=a.stands[1], floor=a.floor, min_area=a.min_area, stem_share=a.stem_share)
    formats = tuple(FORMATS) if a.format == "both" else (a.format,)
    warnings, terrain, lie = [], None, None
    if a.chm:
        src, opened, note = open_canopy(a.chm, rasterio)
        with opened:
            chm = read_canopy(src)
            grid, crs = Grid(src.transform.c, src.transform.f, src.transform.a, src.height, src.width), src.crs
        read = {"what": f"the canopy raster {a.chm.name}", "note": note}
    else:
        chm, grid, crs, terrain, told = canopy_of_points(a.points, a.pixel, rasterio.crs.CRS.from_user_input(a.crs) if a.crs else None,
                                                         a.dem if isinstance(a.dem, Path) else None, a.normalised, rasterio, say)
        read = {"what": f"{told['points']:,} points of {', '.join(told['files'][:3])}{' and others' if len(told['files']) > 3 else ''}",
                "note": f"{told['first_returns_per_m2']:g} first returns per m2; heights above {told['heights_above']}"} | told
        if terrain is not None:
            lie = told["heights_above"]
        if told["first_returns_per_m2"] < SPARSE:
            warnings.append(f"{told['first_returns_per_m2']:g} first returns per m2: under {SPARSE:g} a canopy raster blurs single trees")
    if crs is None:
        warnings.append("the input names no coordinate system, and neither do the files written")
    if grid.pixel > 1.5:
        warnings.append(f"a pixel of {grid.pixel:g} m: a crown under about {3 * grid.pixel:g} m across is not told from its neighbours")
    say(f"{read['what']}: {grid.rows:,} x {grid.cols:,} pixels of {grid.pixel:g} m" + (f"; {read['note']}" if read.get("note") else ""))
    if a.dem == "none":
        terrain, lie = None, "none: --dem none"
    elif terrain is None and isinstance(a.dem, Path):
        terrain, lie = on_grid(a.dem, grid, crs, rasterio), a.dem.name
    elif terrain is None:
        terrain, lie = fetched_terrain(grid, crs, a.dem_dir, rasterio, say)
    if terrain is not None and not np.isfinite(terrain).any():
        terrain, lie = None, f"none: {lie} holds nothing over this area"
    if terrain is None and a.dem != "none":
        warnings.append(f"no terrain ({lie.removeprefix('none: ')}): tops and trunks are where the canopy raster has them, which on a slope is downhill of the tree")
    read["terrain"] = lie

    reference = read_reference(a.validate, crs, grid, rasterio) if a.validate else None      # before anything is written: a file that cannot be read stops here
    a.out.mkdir(parents=True, exist_ok=True)
    aside.extend(make_way(a.out))
    say(f"terrain: {lie}")
    numbers, table, polygons = find_trees(chm, grid, rule, a.tile, a.outline, say, terrain)
    found = summary(chm, numbers, table, grid, rule)
    say(f"{found['trees']:,} trees, {found['trees_per_ha']:,.0f} a hectare")
    if not found["trees"]:
        warnings.append(f"no tree found: no canopy of {rule.min_height:g} m or more with a crown of {rule.min_area:g} m2")

    tags = {"made_by": "tools.crowns", "made": f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%MZ}", "command": command}
    write_tif(a.out / CANOPY, grid, crs, chm, "height_m", "m", "the canopy raster the trees were read from, metres above ground", tags)
    write_tif(a.out / NUMBERS, grid, crs, numbers, "tree_id", "", "the number of the tree whose crown a pixel lies in, as in the vector files; 0 none", tags)
    reach = max(10.0, 5.0 * math.ceil(float(np.nanpercentile(chm, 99.5)) / 5)) if np.isfinite(chm).any() else 30.0
    written = {CANOPY, NUMBERS, "README.md", "report.json", *write_vectors(a.out, table, polygons, crs, formats), *write_qgis(a.out, crs, reach, formats)}

    validation = None
    if reference:
        validation = {"reference": str(a.validate)} | against(*reference, table, polygons)
        (a.out / "validation.json").write_text(json.dumps(validation, indent=1))
        written.add("validation.json")
        say(f"against {a.validate.name} ({reference[1]}): recall {validation['recall']}, precision {validation['precision']}, F1 {validation['f1']}")

    report = {"command": command, "input": read, "grid": {"crs": crs.to_string() if crs is not None else None, "pixel_m": grid.pixel, "rows": grid.rows, "cols": grid.cols,
                                                          "west": grid.west, "north": grid.north},
              "settings": rule._asdict(), "outline": a.outline, "formats": list(formats), "found": found, "drawn_to_m": reach, "validation": validation,
              "warnings": warnings,
              "fields": {name: {"unit": unit, "holds": text} for name, unit, text in FIELDS}}
    (a.out / "report.json").write_text(json.dumps(report, indent=1))
    from .cli import write_readme
    write_readme(a.out, command, report, validation)
    if not a.no_plot and found["trees"]:
        figure(a.out / "report.png", chm, numbers, table, polygons, grid, report, validation)
        written.add("report.png")
    for text in warnings:
        say(f"WARNING: {text}")
    say(f"written to {a.out}: {', '.join(sorted(written))}")
    return written
