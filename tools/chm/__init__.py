"""Calibrated canopy height from ETH and Meta.

    from tools.chm import run, apply, load_table
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from .cog import fine_factor, make_way, restore, write_fine, write_tif
from .fetch import ETH_URL, GDAL_ENV, image_dates, open_meta, read_eth
from .grid import CELL_M, make_grid, read_bbox
from .plot import figure
from .qgis import drawn, write_qgis
from .reduce import cell_top_and_mean
from .table import (
    BAND_NAMES, BANDS, ETH_YEAR, FINE_READINGS, HEIGHT_BANDS, SOURCE_BANDS,
    apply, band_table, expected_error, load_table, provenance, summary, told_of,
)
from .validate import against, against_fine

__all__ = ["apply", "load_table", "run"]


def run(bbox=None, geojson=None, out=None, *, meta="v2", region="pooled", debug=False, validate=None,
        void_zero=False, no_dates=False, recent=False, eth_dir=None, meta_dir=None, cache=None,
        no_plot=False, max_km2=2500.0, command=None, buildings=None, building_mode=None, date=None, building_context=None, building_time_policy=None):
    """Write the 10 m and 1 m maps for a box or a GeoJSON. Returns the file names written."""
    if out is None or (bbox is None) == (geojson is None):
        raise ValueError("pass out, and either bbox or geojson")
    if command is None:
        command = ""
    a = SimpleNamespace(
        bbox=tuple(bbox) if bbox is not None else None,
        geojson=Path(geojson) if geojson else None,
        out=Path(out),
        meta=meta,
        region=region,
        debug=debug,
        validate=Path(validate) if validate else None,
        void_zero=void_zero,
        no_dates=no_dates,
        recent=recent,
        eth_dir=eth_dir,
        meta_dir=meta_dir,
        cache=Path(cache) if cache else None,
        no_plot=no_plot,
        buildings=[Path(p) for p in buildings] if buildings else None,
        building_mode=building_mode, date=date, building_context=building_context, building_time_policy=building_time_policy,
        max_km2=max_km2,
    )
    aside: list[Path] = []
    try:
        written = produce(a, command, aside)
    except BaseException:
        restore(aside)
        raise
    from .cog import clear_away
    clear_away(a.out, aside, written)
    return written


def produce(a, command: str, aside: list[Path]) -> set[str]:
    """Everything a run reads and writes; the names of the files it wrote. `aside` takes the names of an earlier
    run's rasters as soon as they are moved, so that the caller can put them back if this run does not end."""
    import rasterio
    from ..building_mask import from_args, options

    if a.validate:
        with rasterio.open(a.validate) as lidar:
            if not lidar.crs or lidar.crs.is_geographic:
                raise SystemExit("--validate needs a raster on a projected grid in metres")
    bbox, polygons = (tuple(a.bbox), []) if a.bbox else read_bbox(a.geojson)
    grid = make_grid(*bbox)
    area = grid.height * grid.width * CELL_M ** 2 / 1e6
    if area > a.max_km2:
        raise SystemExit(f"the box is {area:,.0f} km2, over --max-km2 {a.max_km2:,.0f}: ask for less, or raise the limit knowingly")
    buildings, excluded = from_args(a.buildings, (grid.height, grid.width), grid.transform, grid.crs, options(a))
    ref = load_table(a.meta, a.region)
    outside = None
    if polygons:
        from rasterio.features import geometry_mask
        from rasterio.warp import transform_geom
        outside = geometry_mask([transform_geom("EPSG:4326", grid.crs, g) for g in polygons], (grid.height, grid.width), grid.transform)
    tags = {"sources": f"ETH Global Canopy Height {ETH_YEAR}; Meta/WRI canopy height {a.meta}", "reference_table": f"{ref['files'][0]} region {ref['region']}",
            "units": "metres"}
    a.out.mkdir(parents=True, exist_ok=True)
    aside.extend(make_way(a.out))
    print(f"grid: {grid.width} x {grid.height} cells of {CELL_M:.0f} m in {grid.crs.to_string()}, {area:,.1f} km2", flush=True)
    with rasterio.Env(**GDAL_ENV):
        eth_from = f"{a.eth_dir.rstrip('/')}/{ETH_URL.rpartition('files=')[2]}" if a.eth_dir else ETH_URL
        eth, eth_files = read_eth(grid, bbox, False, rasterio, eth_from)
        eth_sd, sd_files = read_eth(grid, bbox, True, rasterio, eth_from)
        print(f"ETH: {len(eth_files)} tile(s), {np.isfinite(eth).mean():.1%} of cells read", flush=True)
        sources, meta_files = open_meta(bbox, a.meta, rasterio, f"{a.meta_dir.rstrip('/')}/chm/{{key}}.tif" if a.meta_dir else None)
        print(f"Meta {a.meta}: reading {len(meta_files)} tile(s)" + ("" if a.no_dates else " and the dates of their images, which are tens of megabytes a tile "
              "(--cache DIR keeps them, --no-dates leaves them out)") + " ...", flush=True)
        date, date_files = (np.zeros((grid.height, grid.width), "uint32"), []) if a.no_dates else image_dates(meta_files, grid, a.cache)
        meta_top, _ = cell_top_and_mean(sources, grid)
        print(f"Meta {a.meta}: {len(meta_files)} tile(s), {np.isfinite(meta_top).mean():.1%} of cells read")
        if not (np.isfinite(eth).any() or np.isfinite(meta_top).any()):
            raise SystemExit("neither map holds a pixel over this box: nothing to calibrate")
        got = apply(meta_top, eth, ref, date, recent=a.recent)
        observed_years = [int(v) for v in np.unique(date[date > 0] // 10000)]
        if np.isfinite(eth).any():
            observed_years.append(ETH_YEAR)
        tags["acquisition_years"] = json.dumps(sorted(set(observed_years)))
        tags["acquisition_dates_complete"] = str(not ((date == 0) & np.isfinite(meta_top)).any())
        if buildings is not None:
            buildings.observe(observed_years)
            if ((date == 0) & np.isfinite(meta_top) & (got["weight"] > 0)).any():
                buildings.observation_known = False
            excluded = buildings.exclusion(got["height"], grid.transform, grid.crs)
        factor = fine_factor(meta_top, got["height"], outside)
        planes = dict(zip(BAND_NAMES, (got["height"], got["height_matched"], expected_error(got["height"], ref, got["weight"]), meta_top, eth, eth_sd,
                                       got["weight"]), strict=True))
        source = provenance(meta_top, eth, got, date, ref)
        if outside is not None:
            planes = {k: np.where(outside, np.nan, v) for k, v in planes.items()}
            source = {k: np.where(outside, SOURCE_BANDS[k][0], v).astype(v.dtype) for k, v in source.items()}
            date = np.where(outside, 0, date)
        fine, fine_source = write_fine(sources, meta_files, grid, factor, a.out, tags, rasterio,
                                       planes if a.debug else None, source if a.debug else None, buildings)
        for src in sources:
            src.close()
    if buildings is not None:
        outside = excluded if outside is None else outside | excluded
        planes = {k: np.where(outside, np.nan, v) for k, v in planes.items()}
        source = {k: np.where(outside, SOURCE_BANDS[k][0], v).astype(v.dtype) for k, v in source.items()}
        date = np.where(outside, 0, date)
    height_bands = planes if a.debug else {"height_m": planes["height_m"]}
    write_tif(a.out / "chm_10m.tif", grid, height_bands, tags,
              about={name: (unit, text) for name, unit, _kind, text in HEIGHT_BANDS if name in height_bands})
    if a.debug:
        write_tif(a.out / "chm_10m_source.tif", grid, source,
                  {k: v for k, v in tags.items() if k != "units"} | {name: text for name, (_, text) in SOURCE_BANDS.items()},
                  dtype="uint16", about={name: ("", text) for name, (_, text) in SOURCE_BANDS.items()})
    qgis = write_qgis(a.out, drawn(planes, source, fine, fine_source, a.debug), rasterio)

    table = band_table(ref, "blend", "by_value")
    h = planes["height_m"][np.isfinite(planes["height_m"])]
    for r in table:
        r["cells_in_area"] = int(((h >= r["lo"]) & (h < r["hi"])).sum())
    expected = planes["expected_abs_error_m"][np.isfinite(planes["expected_abs_error_m"])]
    # a cell of BANDS[1] m or more whose top Meta reads as 0: the 1 m file has no crown there to carry the height
    left_as_read = float(((h >= BANDS[1]) & ~(planes["meta_top_m"][np.isfinite(planes["height_m"])] > 0)).sum() / max(h.size, 1))
    beyond = {"meta": float((planes["meta_top_m"] > ref["meta"][0][-1]).mean()), "eth": float((planes["eth_m"] > ref["eth"][0][-1]).mean())}
    warnings = [f"{share:.1%} of cells read {name} above {ref[name][0][-1]:.0f} m, the last point of its lookup: they are read with that point's correction"
                for name, share in beyond.items() if share > 0.01]
    if fine and left_as_read > 0.01:
        warnings.append(f"{left_as_read:.1%} of cells at or above {BANDS[1]:.0f} m have Meta top 0, so those 1 m pixels stay 0")
    told = told_of(source, date)
    if not a.no_dates and told["meta_cells_undated"] > 0.01:
        warnings.append(f"Meta publishes no image date for {told['meta_cells_undated']:.1%} of the cells it is read on: their year is 0")
    print("source: " + ", ".join(f"{name.replace('_', ' ')} {share:.1%}" for name, share in told["source_share"].items()) + " of cells"
          + (f"; Meta's images {min(told['meta_image_dates'])} to {max(told['meta_image_dates'])}, ETH's {ETH_YEAR}" if told["meta_image_dates"] else ""))
    report = {"bbox": [round(v, 5) for v in bbox], "area_km2": round(area, 2),
              "grid": {"crs": grid.crs.to_string(), "cell_m": CELL_M, "width": grid.width, "height": grid.height},
              "sources": {"eth": eth_files, "eth_sd": sd_files, "meta": meta_files, "meta_image_dates": date_files},
              "provenance": {"file": (["chm_10m_source.tif", *fine_source] if a.debug else []),
                             "bands": {name: text for name, (_, text) in SOURCE_BANDS.items()}} | told,
              "reference": {"meta": a.meta, "region": ref["region"], "files": ref["files"]},
              "cells_read": {"eth": float(np.isfinite(planes["eth_m"]).mean()), "meta": float(np.isfinite(planes["meta_top_m"]).mean())},
              "heights_m": {k: summary(planes[k]) for k in ("meta_top_m", "eth_m", "height_m", "height_matched_m", "eth_sd_m")},
              "expected": {"mae_m": round(float(expected.mean()), 2) if expected.size else 0.0,
                           "note": ("Mean of expected_abs_error_m. "
                                    + ("Leave-one-site-out on the reference sites." if ref["accuracy_fit"] == "unseen"
                                       else "Held-out windows of one table fit on every site.")
                                    + " Not measured in this area."),
                           "by_height_read": table},
              "reference_accuracy_by_measured_height": {reading: band_table(ref, reading, "by_lidar")
                                                        for reading in (f"{ref['meta_name']} raw", "eth raw", "blend", f"{ref['meta_name']} matched")},
              "debug": a.debug, "bands_written": list(height_bands),
              "fine_files": fine, "fine_source_files": fine_source,
              "fine_cells_left_as_read": round(left_as_read, 4) if fine else None, "qgis_files": qgis,
              "prefer": "recent" if a.recent else "blend", "warnings": warnings}
    if buildings is not None:
        report["building_exclusion"] = buildings.report(excluded)
        report["warnings"].append(report["building_exclusion"]["warning"])
        report["warnings"].append("Fine pixels outside footprints retain the original 10 m calibration, which may include roof-contaminated inputs.")
    validation = None
    if a.validate:
        with rasterio.open(a.validate) as lidar:
            truth, _ = cell_top_and_mean([lidar], grid)
        if a.void_zero:
            truth[truth == 0] = np.nan
        if outside is not None:
            truth[outside] = np.nan
        readings = {"meta_as_read": planes["meta_top_m"], "eth_as_read": planes["eth_m"], "meta_calibrated": got["meta_calibrated"],
                    "eth_calibrated": got["eth_calibrated"], "blend": planes["height_m"], "meta_matched": planes["height_matched_m"]}
        validation = {"reference": str(a.validate), "cells": int(np.isfinite(truth).sum()), "readings": {k: against(v, truth) for k, v in readings.items()}}
        print("against " + a.validate.name + ": " + "; ".join(f"{k} MAE {v['mae']} RMSE {v['rmse']} bias {v['bias']:+}" for k, v in validation["readings"].items() if v["n"]))
        if fine:
            with rasterio.open(a.validate) as lidar:
                validation["fine"] = against_fine(fine, a.out, lidar, grid, factor, np.isfinite(truth), rasterio)
            print(f"at 1 m, {validation['fine']['pixels']:,} pixels: " + "; ".join(
                f"{FINE_READINGS[k]} MAE {v['mae']} bias {v['bias']:+}" for k, v in validation["fine"]["readings"].items() if v["n"]))
            print("a 10 m cell's mean: " + "; ".join(
                f"from {FINE_READINGS[k]} MAE {v['mae']} bias {v['bias']:+}" for k, v in validation["fine"]["cell_means"].items() if v["n"]))
        (a.out / "validation.json").write_text(json.dumps(validation, indent=1))
    (a.out / "report.json").write_text(json.dumps(report, indent=1))
    from .cli import write_readme
    write_readme(a.out, command, report, validation)
    for line in warnings:
        print("WARNING: " + line)
    print(f"expected MAE {report['expected']['mae_m']} m (reference sites); {len(fine)} 1 m tile(s); written to {a.out}")
    if not a.no_plot:
        figure(a.out / "report.png", planes, source, ref, report, validation)
    written = {"chm_10m.tif", "report.json", "README.md", *fine, *fine_source, *qgis}
    if a.debug:
        written.add("chm_10m_source.tif")
    if validation:
        written.add("validation.json")
    if not a.no_plot:
        written.add("report.png")
    return written
