"""A run against reference tops or crowns."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .canopy import Grid
from .delineate import Rule

# Tops match within this (m). Crowns match at this share of their union.
MATCH_M, MATCH_IOU = 2.0, 0.5


def read_reference(path: Path, crs, grid: Grid, rasterio) -> tuple[np.ndarray, str]:
    """(the reference trees that lie on the raster, in the run's coordinate system; "tops" for points, "crowns" for
    polygons)."""
    import shapely
    from pyogrio.raw import read
    from rasterio.warp import transform as warp_points

    meta, _, wkb, _ = read(path, columns=[])
    shapes = shapely.from_wkb(wkb)
    shapes = shapes[~shapely.is_missing(shapes) & ~shapely.is_empty(shapes)]
    if crs is not None and meta["crs"] and rasterio.crs.CRS.from_user_input(meta["crs"]) != crs:
        xy = shapely.get_coordinates(shapes)
        x, y = warp_points(meta["crs"], crs, xy[:, 0].tolist(), xy[:, 1].tolist())
        shapes = shapely.set_coordinates(shapes, np.column_stack([x, y]))
    kinds = set(shapely.get_type_id(shapes).tolist())
    if not kinds or not (kinds <= {0} or kinds <= {3, 6}):
        raise SystemExit(f"{path} holds neither points alone (tree tops) nor polygons alone (crowns)")
    at = shapely.get_coordinates(shapely.point_on_surface(shapes))
    on = (at[:, 0] >= grid.west) & (at[:, 0] <= grid.west + grid.cols * grid.pixel) & (at[:, 1] <= grid.north) & (at[:, 1] >= grid.north - grid.rows * grid.pixel)
    return shapes[on], "tops" if kinds <= {0} else "crowns"


def against(reference: np.ndarray, kind: str, table: dict, polygons: np.ndarray) -> dict:
    """The trees found against reference trees. Points: a found top within MATCH_M of a reference point, nearest
    pairs first and one to one; and under `trunks` the same of the found trunks, which is the reading where the
    reference is a register of trunks. Crowns: a found crown and a reference crown that overlap by MATCH_IOU of
    their union or more (two crowns can each match one other at most, from a half up)."""
    import shapely

    found = len(table["tree_id"])
    out = {"read_as": kind, "reference_trees": len(reference), "trees_found": found}

    def rates(matched: int) -> dict:
        recall, precision = matched / max(len(reference), 1), matched / max(found, 1)
        return {"matched": matched, "recall": round(recall, 3), "precision": round(precision, 3), "f1": round(2 * recall * precision / max(recall + precision, 1e-9), 3)}

    if kind == "tops":
        from scipy.spatial import cKDTree

        ref = shapely.get_coordinates(reference)

        def paired(x, y) -> dict:
            pairs = cKDTree(np.column_stack([x, y])).sparse_distance_matrix(cKDTree(ref), MATCH_M, output_type="ndarray") if found and len(ref) else []
            mine, theirs, apart = np.zeros(found, bool), np.zeros(len(ref), bool), []
            for i, j, far in sorted(pairs, key=lambda pair: pair[2]):
                if not mine[i] and not theirs[j]:
                    mine[i] = theirs[j] = True
                    apart.append(far)
            return rates(int(theirs.sum())) | {"median_distance_m": round(float(np.median(apart)), 2) if apart else None}

        return out | {"within_m": MATCH_M} | paired(table["top_x"], table["top_y"]) | {"trunks": paired(table["stem_x"], table["stem_y"])}
    i, j = shapely.STRtree(polygons).query(reference, predicate="intersects") if found and len(reference) else (np.zeros(0, int), np.zeros(0, int))
    both = shapely.area(shapely.intersection(reference[i], polygons[j]))
    iou = both / np.maximum(shapely.area(reference[i]) + shapely.area(polygons[j]) - both, 1e-12)
    best = np.zeros(len(reference))
    np.maximum.at(best, i, iou)
    return out | {"at_iou": MATCH_IOU, "mean_best_iou": round(float(best.mean()), 3) if len(best) else None} | rates(int((best >= MATCH_IOU).sum()))


def summary(chm, numbers, table: dict, grid: Grid, rule: Rule) -> dict:
    """What a run found, in numbers: the trees, the canopy they stand in, their heights and crowns."""
    held = np.isfinite(chm)
    canopy = held & (chm >= rule.min_height)
    hectares = float(held.sum()) * grid.pixel ** 2 / 1e4

    def spread(values) -> dict:
        return dict(zip(("p5", "p25", "p50", "p75", "p95", "max"), np.round(np.percentile(values, [5, 25, 50, 75, 95, 100]), 2).tolist(), strict=True)) if len(values) else {}

    return {"trees": len(table["tree_id"]), "area_with_data_ha": round(hectares, 2), "trees_per_ha": round(len(table["tree_id"]) / max(hectares, 1e-9), 1),
            "canopy_cover": round(float(canopy.sum()) / max(int(held.sum()), 1), 3), "canopy_in_crowns": round(float((numbers > 0).sum()) / max(int(canopy.sum()), 1), 3),
            "height_m": spread(table["height_m"]), "crown_diameter_m": spread(table["crown_diameter_m"]), "crowns_at_edge": int(table["at_edge"].sum()),
            "trunk_from_top_m": spread(np.hypot(table["stem_x"] - table["top_x"], table["stem_y"] - table["top_y"])),
            "slope_deg": spread(table["slope_deg"][np.isfinite(table["slope_deg"])])}
