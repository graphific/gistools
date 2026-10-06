"""Command line for the canopy-height tool."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from .fetch import META_URL
from .qgis import flag_name
from .table import APART, ETH_YEAR, FINE_READINGS, FINE_TEXT, FLAG_NAMES, HEIGHT_BANDS, READINGS, SOURCE_BANDS


def write_readme(out: Path, command: str, report: dict, validation: dict | None) -> None:
    """Short note next to the maps: files from this run, checks, citations."""
    told, files, debug = report["provenance"], report["sources"], report["debug"]
    dates = ", ".join(f"{day} ({share:.0%})" for day, share in told["meta_image_dates"].items()) or "not read"
    shares = ", ".join(f"{label} {told['source_share'][key]:.1%}" for key, label in (("both", "both"), ("meta_alone", "Meta only"), ("eth_alone", "ETH only")))
    flagged = "; ".join(f"{text} {share:.1%}" for text, share in zip(FLAG_NAMES.values(), told["flags_share"].values(), strict=True))
    tall = report["reference_accuracy_by_measured_height"]["blend"][-1]
    lines = [
        f"# Canopy height, {report['bbox']}", "",
        f"{datetime.now(timezone.utc):%Y-%m-%d %H:%MZ}. `chm {command}`",
        f"{report['area_km2']:.1f} km2, {report['grid']['cell_m']:.0f} m cells, {report['grid']['crs']}.",
        "Method: `docs/canopy-height.md`.", "",
        "## Files", "",
        "| file | contents |", "|---|---|",
        "| `chm_10m.tif` | `height_m` in metres. NaN = no height |",
        *(f"| `{name}` | 1 m `height_m` on Meta's grid. NaN = no pixel |" for name in report["fine_files"]),
    ]
    if debug:
        lines += [
            "| `chm_10m_source.tif` | source, dates, flags |",
            *(f"| `{name}` | same source bands, 10 m values repeated |" for name in report["fine_source_files"]),
            "",
            "Debug bands. On a 1 m file, every band except `height_m` is the 10 m cell value.",
            "",
            "| band | unit | holds |", "|---|---|---|",
            *(f"| `{name}` | {unit or '-'} | {text} |" for name, unit, _kind, text in HEIGHT_BANDS),
            "",
            "| band | holds |", "|---|---|",
            *(f"| `{name}` | {text} |" for name, (_empty, text) in SOURCE_BANDS.items()),
            "",
            "| flags | meaning |", "|---|---|",
            *(f"| {v} | {flag_name(v)} |" for v in range(2 * APART)),
            "",
        ]
    else:
        lines += ["", "`--debug` adds inputs, weight, expected error, source, dates and flags on both the 10 m and 1 m maps.", ""]
    lines += [
        f"1 m `height_m`: {FINE_TEXT}. Where the cell top is 0 the pixel stays as read.",
        "Drag `chm_layers.qlr` into QGIS. The 10 m `height_m` layer is on. Keep the folder together.",
        "",
        "## This run", "",
        f"- ETH {ETH_YEAR}: {len(files['eth'])} tile(s). Meta {report['reference']['meta']}: {len(files['meta'])} tile(s).",
        f"- Cells with a height: {shares}.",
        f"- Meta image dates, share of cells where Meta is used: {dates}.",
        f"- Flags: {flagged}.",
        f"- Expected MAE {report['expected']['mae_m']} m. {report['expected']['note']}",
        f"- Reference sites, canopy >= {tall['lo']:.0f} m: median error {tall['median']:+.0f} m.",
        *(["- `--recent`: where the years differ, the height is the later map alone."] if report["prefer"] == "recent" else []),
        *(f"- Warning: {text}." for text in report["warnings"]),
        "",
    ]
    if validation:
        lines += [
            "## LiDAR check", "",
            f"`{Path(validation['reference']).name}`, max pixel per 10 m cell, {validation['cells']:,} cells. Error = reading minus reference.",
            "",
            "| reading | cells | MAE (m) | RMSE (m) | bias (m) |", "|---|---|---|---|---|",
            *(f"| {READINGS[key]} | {v['n']:,} | {v['mae']:.2f} | {v['rmse']:.2f} | {v['bias'] + 0:+.2f} |" for key, v in validation["readings"].items() if v["n"]),
            "",
        ]
        if validation.get("fine"):
            lines += [
                f"1 m file, {validation['fine']['pixels']:,} pixels in those cells. Reference is the mean of its pixels inside each 1 m pixel.",
                "",
                "| reading | MAE (m) | RMSE (m) | bias (m) |", "|---|---|---|---|",
                *(f"| {FINE_READINGS[key]} | {v['mae']:.2f} | {v['rmse']:.2f} | {v['bias'] + 0:+.2f} |" for key, v in validation["fine"]["readings"].items() if v["n"]),
                *(f"| 10 m cell mean, from {FINE_READINGS[key]} | {v['mae']:.2f} | {v['rmse']:.2f} | {v['bias'] + 0:+.2f} |"
                  for key, v in validation["fine"]["cell_means"].items() if v["n"]),
                "",
            ]
    lines += [
        "## Cite", "",
        "- Lang, Jetz, Schindler, Wegner (2023). doi:10.3929/ethz-b-000609802. CC BY 4.0.",
        "- Meta / World Resources Institute canopy height. Tolan et al. (2024) for v1. CC BY 4.0.",
        "- Calibration tables in `tools/chm/data/`. See `docs/canopy-height.md`.",
        "",
    ]
    (out / "README.md").write_text("\n".join(lines))



def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Calibrated canopy height at 10 m and 1 m, from ETH, Meta, and a LiDAR lookup table.")
    from ..buildings import arguments
    arguments(ap)
    where = ap.add_mutually_exclusive_group(required=True)
    where.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"), help="degrees")
    where.add_argument("--geojson", type=Path, help="a polygon or a collection; the map is cut to its polygons")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--buildings", type=Path, nargs="+", help="auto fetches Overture over this AOI; otherwise supply local building polygons")
    ap.add_argument("--meta", choices=sorted(META_URL), default="v2")
    ap.add_argument("--region", default="pooled", help="a reference place whose own lookups are used instead of the pooled ones")
    ap.add_argument("--debug", action="store_true",
                    help="also write diagnostic bands (inputs, weight, error, source, dates, flags) on the 10 m and 1 m maps")
    ap.add_argument("--validate", type=Path, help="a LiDAR canopy-height raster of the area, in metres, on a projected grid")
    ap.add_argument("--void-zero", action="store_true",
                    help="with --validate: a cell whose every LiDAR pixel is 0 is a hole in that raster, not bare ground")
    ap.add_argument("--no-dates", action="store_true", help="leave the dates of Meta's images unread (some 15 MB a tile): years are then 0")
    ap.add_argument("--recent", action="store_true",
                    help="where both maps hold a value, take the calibrated reading of the later image alone instead of the blend")
    ap.add_argument("--eth-dir", help="a folder or URL prefix that holds ETH's tiles under their own names, read instead of ETH's server")
    ap.add_argument("--meta-dir", help="a folder or URL prefix that holds Meta's tiles as chm/<quadkey>.tif and metadata/<quadkey>.geojson")
    ap.add_argument("--cache", type=Path, help="a folder that keeps the dates of Meta's images between runs (tens of megabytes a tile)")
    ap.add_argument("--no-plot", action="store_true")
    ap.add_argument("--max-km2", type=float, default=2500.0)
    a = ap.parse_args(argv)
    if a.recent and a.no_dates:
        ap.error("--recent chooses by the dates of Meta's images, which --no-dates leaves unread")
    from . import run
    run(bbox=a.bbox, geojson=a.geojson, out=a.out, meta=a.meta, region=a.region, debug=a.debug,
        validate=a.validate, void_zero=a.void_zero, no_dates=a.no_dates, recent=a.recent,
        eth_dir=a.eth_dir, meta_dir=a.meta_dir, cache=a.cache, no_plot=a.no_plot, max_km2=a.max_km2,
        command=" ".join(sys.argv[1:] if argv is None else argv), buildings=a.buildings, building_mode=a.building_mode, date=a.date, building_context=a.building_context, building_time_policy=a.building_time_policy)
    return 0
