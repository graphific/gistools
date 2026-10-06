"""Command line. `python -m tools.crowns` or `pixi run crowns`."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from .delineate import CROWNS, Rule
from .io import CANOPY, FIELDS, FORMATS, NUMBERS, OUTLINES, TILE, clear_away, restore
from .terrain import BARE_TEXT, DEM_TEXT


def write_readme(out: Path, command: str, report: dict, validation: dict | None) -> None:
    """README.md beside the files: what each holds, how to open them, how to read a tree, what was read and found."""
    found, rule, grid = report["found"], report["settings"], report["grid"]
    tall, wide, moved, steep = found["height_m"], found["crown_diameter_m"], found["trunk_from_top_m"], found["slope_deg"]

    def files(layer: str) -> str:
        return ", ".join(f"`{layer}{FORMATS[fmt][1]}`" for fmt in report["formats"])

    lines = [
        f"# Tree tops, trunks and crowns, {found['trees']:,} trees", "",
        (f"Made {datetime.now(timezone.utc):%Y-%m-%d %H:%MZ} by `crowns {command}`. {found['area_with_data_ha']:,.1f} ha of canopy raster at "
         f"{grid['pixel_m']:g} m a pixel in {grid['crs'] or 'no named coordinate system'}. Method and accuracy: docs/tree-crowns.md."), "",
        "## Files", "", "| file | what it is |", "|---|---|",
        f"| {files('tree_tops')} | one point a tree, at its top (layer `tree_tops`) |",
        f"| {files('tree_stems')} | one point a tree, where its trunk is expected to stand (layer `tree_stems`) |",
        f"| {files('tree_crowns')} | one polygon a tree, its crown (layer `tree_crowns`) |",
        f"| `{NUMBERS}` | the tree's number per pixel, 0 where there is no crown: the crowns before their outlines were drawn |",
        f"| `{CANOPY}` | the canopy raster the trees were read from, metres above ground, NaN where there is none |",
        "| `trees.qlr` | QGIS: the tops and the trunks over the crowns over the canopy raster, each with its legend |",
        "| `tree_tops.qml`, `tree_stems.qml`, `tree_crowns.qml`, `canopy_height.qml` | QGIS: the style of each file, applied when the file is opened |",
        "| `report.json`, `report.png` | the numbers of this run and the figure of them |",
        *(["| `validation.json` | the trees found against the reference trees given |"] if validation else []), "",
        "## In QGIS", "",
        ("Drag `trees.qlr` into the map: a group opens with the tops as white dots, the trunks as brown dots, the crowns coloured by tree height and the "
         "canopy raster under them. A file opened by itself takes its `.qml`. The files must stay together: the `.qlr` names them by their place beside it."), "",
        *([("The GeoJSON files are in longitude and latitude (WGS 84), as that format's standard asks; the GeoPackages and the rasters are in "
            f"{grid['crs']}. `top_x`, `top_y`, `stem_x` and `stem_y` are in {grid['crs']} in every file."), ""] if "geojson" in report["formats"] and grid["crs"] else []),
        "## Reading a tree", "",
        "- A top is a high point of the canopy raster; a crown is the canopy that rises to it, down to a share of the top's height.",
        ("- A trunk is not seen from above: its point is where the trunk is expected to stand, under the middle of the upper "
         f"{rule['stem_share']:.0%} of the crown's depth. A tree that leans, or whose crown grew to one side, stands elsewhere."),
        ("- On a slope a canopy raster tilts every crown: its downhill side reads taller than it stands and its highest pixel lies downhill of the tree. "
         "With a terrain the crown is put back on its ground before the top and the trunk are read, and `height_m` is the top's elevation less the "
         "ground's under the trunk."),
        ("- Only trees whose crown is seen from above are found. A tree under another's crown holds no pixel of the raster and is not here; in a "
         "closed stand that is most of the lower layer."),
        f"- A crown narrower than about three pixels ({3 * grid['pixel_m']:g} m here) is not told from its neighbours.",
        ("- In stands of broadleaves with wide, lumpy crowns one tree is often split in two or more, and two whose crowns run together are one here. "
         "`prominence_m` says how clearly a top stands apart: a low value beside a taller neighbour is the first to doubt."),
        "- `at_edge` marks a crown cut by the edge of the data.", "",
        "## What a tree carries", "", "| field | unit | what it holds |", "|---|---|---|",
        *(f"| `{name}` | {unit or '-'} | {text} |" for name, unit, text in FIELDS), "",
        f"The same fields are in every vector file, joined by `tree_id`, which is also the value of `{NUMBERS}`.", "",
        "## This run", "",
        f"- Read: {report['input']['what']}." + (f" {report['input']['note'].capitalize()}." if report["input"].get("note") else ""),
        f"- Terrain: {report['input']['terrain']}." + (f" Half the trees stand on slopes between {steep['p25']:g} and {steep['p75']:g} degrees, one in twenty on "
                                                        f"{steep['p95']:g} or more." if steep else ""),
        (f"- Tops: the canopy smoothed by {rule['smooth']:g} m, a top the highest pixel of a window {rule['window']:g} m plus {rule['window_share']:.1%} of its height "
         f"wide" + (f", standing {rule['stands']:g} m or {rule['stands_share']:.1%} of its height above its pass" if rule["stands"] or rule["stands_share"] else "")
         + f"; no tree under {rule['min_height']:g} m."),
        f"- Crowns: `{rule['crowns']}`; none under {rule['min_area']:g} m2; outlines `{report['outline']}`.",
        (f"- Found: {found['trees']:,} trees, {found['trees_per_ha']:,.0f} a hectare; canopy over {found['canopy_cover']:.0%} of the area, "
         f"{found['canopy_in_crowns']:.0%} of it inside a crown; {found['crowns_at_edge']:,} crowns at an edge."),
        *([(f"- Heights: half the trees between {tall['p25']:g} and {tall['p75']:g} m, the middle one {tall['p50']:g} m, the tallest {tall['max']:g} m. "
            f"Crown diameters: half between {wide['p25']:g} and {wide['p75']:g} m, the middle one {wide['p50']:g} m. "
            f"Trunks: the middle one {moved['p50']:g} m from its top, one in twenty {moved['p95']:g} m or more.")] if tall else []),
        *(f"- WARNING: {text}." for text in report["warnings"]), "",
        *((["## Measured here", "",
            (f"Against `{Path(validation['reference']).name}`, read as {validation['read_as']}: {validation['reference_trees']:,} reference trees on the raster, "
             + (f"a found top within {validation['within_m']:g} m of a reference top, nearest pairs first and one to one." if validation["read_as"] == "tops" else
                f"a found crown and a reference crown that overlap by {validation['at_iou']:g} of their union or more.")), "",
            "| read | reference trees | trees found | matched | recall | precision | F1 | middle distance (m) |", "|---|---|---|---|---|---|---|---|",
            *((f"| {name} | {validation['reference_trees']:,} | {validation['trees_found']:,} | {v['matched']:,} | {v['recall']:.3f} | {v['precision']:.3f} | "
               f"{v['f1']:.3f} | {v['median_distance_m'] if v.get('median_distance_m') is not None else '-'} |")
              for name, v in (("the tops" if validation["read_as"] == "tops" else "the crowns", validation), *((("the trunks", validation["trunks"]),) if "trunks" in validation else ()))),
            ""]) if validation else []),
        "## Cite", "",
        "- tools.crowns. Methods and reference sets: docs/tree-crowns.md.", ""]
    (out / "README.md").write_text("\n".join(lines))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Tree tops, trunks and crowns from a canopy-height raster or LiDAR points.")
    from ..building_mask import arguments
    arguments(ap)
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--chm", type=Path, help="a canopy-height raster in metres above ground")
    source.add_argument("--points", type=Path, nargs="+", help="LAS or LAZ files of one area, their ground classified (class 2)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--buildings", type=Path, nargs="+", help="local building polygons for roof-height screening and crown constraints")
    rule = Rule()
    ap.add_argument("--crowns", choices=CROWNS, default=rule.crowns, help="how a crown is drawn around its top")
    ap.add_argument("--min-height", type=float, default=rule.min_height, help="no tree lower than this (m)")
    ap.add_argument("--smooth", type=float, default=rule.smooth, help="the smoothing the tops are read under (m, a Gaussian's standard deviation; 0 none)")
    ap.add_argument("--window", nargs=2, type=float, default=(rule.window, rule.window_share), metavar=("M", "SHARE"),
                    help="a top is the highest pixel of a round window M metres plus SHARE of its height wide")
    ap.add_argument("--stands", nargs=2, type=float, default=(rule.stands, rule.stands_share), metavar=("M", "SHARE"),
                    help="and stands M metres or SHARE of its height, whichever is more, above its highest pass to higher canopy")
    ap.add_argument("--floor", type=float, default=rule.floor, help="watershed: a pixel lower than this share of its top is not its crown")
    ap.add_argument("--min-area", type=float, default=rule.min_area, help="no crown smaller than this (m2)")
    ap.add_argument("--stem-share", type=float, default=rule.stem_share,
                    help="the trunk is put under the middle of this share of the crown's depth, from the top down (0: under the top)")
    ap.add_argument("--outline", choices=OUTLINES, default=OUTLINES[0], help="how the crowns' polygons follow the pixels")
    ap.add_argument("--format", choices=(*FORMATS, "both"), default="both", help="the vector files: GeoPackage, GeoJSON (longitude and latitude) or both")
    ap.add_argument("--dem", default="auto", metavar="auto|none|FILE",
                    help=f"the terrain the trees stand on. auto: the points' own ground, else {BARE_TEXT} and {DEM_TEXT} fetched for the area; none; or a "
                         "raster of elevations. With --points a FILE is also what the heights are taken above")
    ap.add_argument("--dem-dir", help=f"auto: a folder or address that holds the {BARE_TEXT} and {DEM_TEXT} files under their own names, read instead of the public ones")
    ap.add_argument("--pixel", type=float, help="--points: the canopy's pixel (m); chosen from the density without it")
    ap.add_argument("--normalised", action="store_true", help="--points: the heights are above ground already")
    ap.add_argument("--crs", help="--points: the coordinate system, where the files do not name it (EPSG:25832)")
    ap.add_argument("--validate", type=Path, help="reference trees of the area: points (tops) or polygons (crowns), any vector file")
    ap.add_argument("--tile", type=int, default=TILE, help="the raster is worked in tiles this many pixels wide")
    ap.add_argument("--no-plot", action="store_true")
    a = ap.parse_args(argv)
    if a.chm and (a.pixel or a.normalised or a.crs):
        ap.error("--pixel, --normalised and --crs are for --points")
    if not 0 <= a.stem_share <= 1:
        ap.error("--stem-share is a share of the crown's depth, between 0 and 1")
    a.dem = a.dem if a.dem in ("auto", "none") else Path(a.dem)
    if a.dem_dir and a.dem != "auto":
        ap.error("--dem-dir says where --dem auto finds its files")
    for given in (a.chm, a.dem, a.validate, *(a.points or ())):
        if isinstance(given, Path) and not given.exists():
            ap.error(f"{given} is not there")
    aside: list[Path] = []
    try:
        from . import produce
        written = produce(a, " ".join(sys.argv[1:] if argv is None else argv), aside)
    except BaseException:
        restore(aside)
        raise
    clear_away(a.out, aside, written)
    return 0
