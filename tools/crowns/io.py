"""Crowns as vectors and rasters, and the files a run owns."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from .canopy import Grid
from .delineate import delineate

OUTLINES, SIMPLIFY, CUTS = ("simplified", "smooth", "pixels"), 0.75, 2
TILE, OVERLAP_M = 4096, 48.0
NUMBERS, CANOPY = "crown_ids.tif", "canopy_height.tif"
VECTORS = (("tree_tops", "Point"), ("tree_stems", "Point"), ("tree_crowns", "Polygon"))
FORMATS = {"gpkg": ("GPKG", ".gpkg"), "geojson": ("GeoJSON", ".geojson")}
OWN_DATA, ASIDE = (*(name + ending for name, _ in VECTORS for _, ending in FORMATS.values()), NUMBERS, CANOPY), ".replaced"
OWN_FILES = (*OWN_DATA, "tree_tops.qml", "tree_stems.qml", "tree_crowns.qml", "canopy_height.qml",
             "trees.qlr", "README.md", "report.json", "report.png", "validation.json")
FIELDS = (
    ("tree_id", "", "tree number, the same in every file; numbered along rows from the top"),
    ("height_m", "m", "canopy height at the top; with a terrain, top elevation minus ground under the trunk"),
    ("crown_area_m2", "m2", "crown area, counted from the pixels"),
    ("crown_diameter_m", "m", "diameter of a circle of that area"),
    ("crown_mean_height_m", "m", "mean canopy height over the crown"),
    ("prominence_m", "m", "height of the top above the highest pass to taller canopy"),
    ("at_edge", "", "1 if the crown touches the data edge or a hole"),
    ("top_x", "", "top x, in the file CRS"),
    ("top_y", "", "top y"),
    ("stem_x", "", "expected trunk x, under the upper part of the crown"),
    ("stem_y", "", "expected trunk y"),
    ("ground_m", "m", "terrain elevation under the trunk; empty without a terrain"),
    ("slope_deg", "deg", "terrain slope there; empty without a terrain"),
)


def rounded(polygon):
    """`polygon` with every corner cut CUTS times: each replaced by the points a quarter along its two sides. Two
    crowns keep the line they share but for a pixel's fraction where three meet."""
    import shapely

    def cut(ring):
        c = np.asarray(ring.coords)[:-1]
        for _ in range(CUTS):
            after = np.roll(c, -1, axis=0)
            c = np.stack([0.75 * c + 0.25 * after, 0.25 * c + 0.75 * after], axis=1).reshape(-1, 2)
        return np.vstack([c, c[:1]])

    out = shapely.Polygon(cut(polygon.exterior), [cut(ring) for ring in polygon.interiors])
    return out if out.is_valid and not out.is_empty else polygon


def outlines(crowns, transform, pixel: float, kind: str, tops) -> np.ndarray:
    """One polygon a crown, crown k at [k - 1], in the coordinates `transform` gives the pixels. A crown whose top
    (`tops`, one point a crown) a drawn outline would leave outside keeps the plainer outline that holds it."""
    import shapely
    from rasterio import features

    out = np.empty(int(crowns.max()), dtype=object)
    for geometry, number in features.shapes(crowns, mask=crowns > 0, connectivity=4, transform=transform):
        out[int(number) - 1] = shapely.geometry.shape(geometry)
    if kind == "pixels" or not len(out):
        return out
    simple = shapely.coverage_simplify(out, SIMPLIFY * pixel)
    simple = np.where((shapely.get_type_id(simple) == 3) & ~shapely.is_empty(simple) & shapely.contains(simple, tops), simple, out)
    if kind == "simplified":
        return simple
    cut = np.array([rounded(g) for g in simple], dtype=object)
    return np.where(shapely.contains(cut, tops), cut, simple)


def find_trees(chm, grid: Grid, rule: Rule, tile: int, outline: str, say, terrain=None) -> tuple[np.ndarray, dict, np.ndarray]:
    """Every tree of a canopy raster, read in tiles: (the tree's number per pixel; one array a field of FIELDS, tree
    k at [k - 1]; the crowns' polygons in the same order). A tile is read with OVERLAP_M around it and gives the
    trees whose top lies in it, so a tree is found once and the same whatever the tiles; trees are numbered along
    the rows by their top."""
    import shapely
    from affine import Affine

    numbers = np.zeros(chm.shape, "uint32")
    rows, cols = chm.shape
    halo = math.ceil(OVERLAP_M / grid.pixel)
    keys, parts, shapes = [], [], []
    tiles = [(r, c) for r in range(0, rows, tile) for c in range(0, cols, tile)]
    for k, (r0, c0) in enumerate(tiles, 1):
        r1, c1 = min(r0 + tile, rows), min(c0 + tile, cols)
        ra, ca, rb, cb = max(r0 - halo, 0), max(c0 - halo, 0), min(r1 + halo, rows), min(c1 + halo, cols)
        trees = delineate(chm[ra:rb, ca:cb], grid.pixel, rule, None if terrain is None else terrain[ra:rb, ca:cb])
        row, col = trees.row + ra, trees.col + ca
        key = np.floor(row).astype("int64") * cols + np.floor(col).astype("int64")
        numbers[r0:r1, c0:c1] = np.concatenate([[0], key + 1]).astype("uint32")[trees.crowns[r0 - ra: r1 - ra, c0 - ca: c1 - ca]]
        mine = (row >= r0) & (row < r1) & (col >= c0) & (col < c1)
        x, y = grid.west + col * grid.pixel, grid.north - row * grid.pixel
        polygons = outlines(trees.crowns, Affine(grid.pixel, 0, grid.west + ca * grid.pixel, 0, -grid.pixel, grid.north - ra * grid.pixel), grid.pixel, outline,
                            shapely.points(x, y))
        keys.append(key[mine])
        shapes.append(polygons[mine])
        parts.append(np.column_stack([trees.height, trees.area, 2 * np.sqrt(trees.area / np.pi), trees.mean_height, trees.rise, trees.edge, x, y,
                                      grid.west + (trees.stem_col + ca) * grid.pixel, grid.north - (trees.stem_row + ra) * grid.pixel, trees.ground, trees.slope])[mine])
        if len(tiles) > 1:
            say(f"tile {k} of {len(tiles)}: {int(mine.sum()):,} trees")
    key, values, polygons = np.concatenate(keys), np.vstack(parts).reshape(-1, len(FIELDS) - 1), np.concatenate(shapes)
    order = np.argsort(key, kind="stable")
    key, values, polygons = key[order], values[order], polygons[order]
    for r in range(0, rows, 2048):                                  # a top's pixel becomes the tree's number; a crown no tile kept goes
        held = numbers[r: r + 2048].astype("int64") - 1
        at = np.minimum(np.searchsorted(key, held), max(len(key) - 1, 0))
        numbers[r: r + 2048] = np.where((held >= 0) & (key[at] == held), at + 1, 0) if len(key) else 0
    table = {"tree_id": np.arange(1, len(key) + 1, dtype="int32")}
    for name, column in zip([f[0] for f in FIELDS[1:]], values.T, strict=True):
        table[name] = column.astype("int16") if name == "at_edge" else column if name.endswith(("_x", "_y")) else np.round(column, 2)   # a rounded top can leave its crown
    return numbers, table, polygons


def write_tif(path: Path, grid: Grid, crs, band, name: str, unit: str, text: str, tags: dict) -> None:
    """One band as a cloud-optimised GeoTIFF with overviews, its name, unit and what it holds written into it. A band
    of heights is float32 with NaN for none and averaged overviews; one of numbers keeps its type, 0 for none."""
    from affine import Affine
    from rasterio.io import MemoryFile
    from rasterio.shutil import copy

    whole = band.dtype.kind in "iu"
    profile = {"driver": "GTiff", "height": grid.rows, "width": grid.cols, "count": 1, "dtype": band.dtype.name, "crs": crs,
               "transform": Affine(grid.pixel, 0, grid.west, 0, -grid.pixel, grid.north), "nodata": 0 if whole else float("nan")}
    with MemoryFile() as memory:                                    # the COG driver only copies, so the band is named in memory first
        with memory.open(**profile) as dst:
            dst.write(band, 1)
            dst.set_band_description(1, name)
            dst.set_band_unit(1, unit)
            held = band[np.isfinite(band)] if not whole else band[band > 0]
            dst.update_tags(1, DESCRIPTION=text, **({"STATISTICS_MINIMUM": float(held.min()), "STATISTICS_MAXIMUM": float(held.max()),
                                                    "STATISTICS_MEAN": float(held.mean()), "STATISTICS_STDDEV": float(held.std())} if held.size else {}))
            dst.update_tags(**tags)
        with memory.open() as src:
            copy(src, path, driver="COG", compress="deflate", predictor=2 if whole else 3, overview_resampling="nearest" if whole else "average")


def write_vectors(out: Path, table: dict, polygons, crs, formats) -> list[str]:
    """The three layers (tops, trunks, crowns), each with FIELDS a tree, in every format asked: the names written.
    A GeoPackage keeps the run's coordinate system; GeoJSON is in longitude and latitude where the run names one."""
    import shapely
    from pyogrio.raw import write

    shapes = {"tree_tops": shapely.points(table["top_x"], table["top_y"]), "tree_stems": shapely.points(table["stem_x"], table["stem_y"]), "tree_crowns": polygons}
    names = [name for name, _, _ in FIELDS]
    empty = [~np.isfinite(table[name]) if table[name].dtype.kind == "f" else None for name in names]      # a value the run does not know is empty, not NaN
    written = []
    for layer, kind in VECTORS:
        for fmt in formats:
            driver, ending = FORMATS[fmt]
            extra = {"layer_options": {"RFC7946": "YES", "COORDINATE_PRECISION": "7"}} if fmt == "geojson" and crs is not None else {}
            write(out / (layer + ending), shapely.to_wkb(shapes[layer]), field_data=[np.nan_to_num(table[name]) if table[name].dtype.kind == "f" else table[name] for name in names],
                  fields=names, field_mask=empty, geometry_type=kind, crs=crs.to_wkt() if crs is not None else None, layer=layer, driver=driver, **extra)
            written.append(layer + ending)
    return written


def make_way(out: Path) -> list[Path]:
    """Move an earlier run's data files aside and return their names.

    GDAL deletes a file before creating one of that name. A file QGIS has open keeps the name, so the rename
    frees it. A GeoPackage goes with its `-wal` and `-shm`. If a rename is refused, the earlier files come back."""
    aside = []
    for path in sorted(p for name in OWN_DATA for p in (out / name, out / f"{name}-wal", out / f"{name}-shm") if p.exists()):
        try:
            path.rename(path.with_name(path.name + ASIDE))
        except OSError as e:
            restore(aside)
            raise SystemExit(f"{path} of an earlier run cannot be replaced ({e.strerror}): another program has it open. Close it there (in QGIS, remove "
                             "the layer or close the project) and run again, or give another --out") from None
        aside.append(path)
    return aside


def restore(aside: list[Path]) -> None:
    """The earlier run's files back under their names, over whatever a run that did not end left there."""
    for path in aside:
        path.with_name(path.name + ASIDE).replace(path)


def clear_away(out: Path, aside: list[Path], written: set[str]) -> None:
    """After a run that ended: the earlier run's data files go, and with them every file of this tool's that the run
    did not write, so the folder holds one run and its README names every file of it. A file of another name is not
    touched."""
    for path in aside:
        path.with_name(path.name + ASIDE).unlink()
    for path in sorted(out / name for name in OWN_FILES if (out / name).exists()):
        if path.name not in written:
            path.unlink()
