"""Raster exclusion and stem constraints from building polygons."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .read import WARNING, field_value, number, optional_year, snapshot_year, year

class BuildingMask:
    """Read explicit polygon files once over the requested bounds; rasterize on each output grid."""

    def __init__(self, paths, bounds, crs, options=None):
        import pyogrio
        import shapely
        from pyogrio.raw import read
        from rasterio.warp import transform_bounds, transform_geom

        if crs is None:
            raise ValueError("Building exclusion requires a declared raster CRS")
        self.crs = crs
        self.options = options or {"mode": "strict"}
        if self.options.get("mode", "canopy") not in {"canopy", "strict"}:
            raise ValueError("Building mode must be canopy or strict")
        self.context = {}
        if self.options.get("context"):
            context = Path(self.options["context"]).resolve(strict=True)
            self.context = json.loads(context.read_text())
            self.options = {**self.options, "context": str(context), "context_sha256": hashlib.sha256(context.read_bytes()).hexdigest()}
        self.years = set()
        self.observed_years = set()
        if self.options.get("date"):
            self.years.add(year(self.options["date"]))
        self.observation_known = False
        self.recent_years = self.context.get("recent_years", 2)
        if not isinstance(self.recent_years, int) or not 0 <= self.recent_years <= 10:
            raise ValueError("recent_years must be an integer from 0 to 10")
        for key in ("clearance_m", "max_stem_shift_m"):
            value = float(self.context.get(key, 2.0))
            if not 0 <= value < 100:
                raise ValueError(f"{key} must be finite, nonnegative and below 100 m")
        self.sources, self.geometries = [], []
        self.attributes = []
        self._projected = {}
        for name in paths:
            path = Path(name).resolve(strict=True)
            layers = [str(row[0]) for row in pyogrio.list_layers(path) if row[1] is not None]
            if len(layers) != 1:
                raise ValueError(f"{path}: supply a building file with exactly one spatial layer")
            info = pyogrio.read_info(path, layer=layers[0])
            if not info["crs"]:
                raise ValueError(f"{path}: building footprints have no CRS")
            bbox = transform_bounds(crs, info["crs"], *bounds, densify_pts=21)
            _meta, _ids, wkb, _fields = read(path, layer=layers[0], bbox=bbox)
            fields = dict(zip(_meta["fields"], _fields, strict=True))
            for key in ("height_field", "min_height_field", "construction_year_field", "demolition_year_field"):
                if key in self.context and self.context[key] not in fields:
                    raise ValueError(f"{path}: requested field {self.context[key]} is absent")
            shapes = shapely.from_wkb(wkb)
            if any(g is None or g.is_empty or not g.is_valid or g.geom_type not in {"Polygon", "MultiPolygon"} for g in shapes):
                raise ValueError(f"{path}: selected footprints must be valid nonempty polygons")
            self.geometries.extend(shapely.geometry.shape(transform_geom(info["crs"], crs, g.__geo_interface__)) for g in shapes)
            for i in range(len(shapes)):
                self.attributes.append({key: field_value(fields, i, self.context.get(key, default))
                                        for key, default in (("height_field", "height"), ("min_height_field", "min_height"),
                                                             ("construction_year_field", "construction_year"),
                                                             ("demolition_year_field", "demolition_year"))})
                self.attributes[-1]["snapshot_year"] = snapshot_year(path, self.context)
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            self.sources.append({"path": str(path), "sha256": digest.hexdigest(), "layer": layers[0],
                                 "crs": info["crs"], "selected_features": len(shapes),
                                 "snapshot_year": snapshot_year(path, self.context),
                                 "snapshot_basis": "context" if "snapshot_year" in self.context else "filename date, publication only"})

    def projected(self, crs):
        import shapely
        from rasterio.warp import transform_geom

        key = str(crs)
        if key not in self._projected:
            geometries = [shapely.geometry.shape(transform_geom(self.crs, crs, g.__geo_interface__)) for g in self.geometries]
            self._projected[key] = (geometries, shapely.STRtree(geometries))
        return self._projected[key]

    def mask(self, shape, transform, crs):
        import numpy as np
        import shapely
        from rasterio.features import rasterize
        from rasterio.transform import array_bounds

        geometries, tree = self.projected(crs)
        indices = tree.query(shapely.box(*array_bounds(*shape, transform)), predicate="intersects")
        if not len(indices):
            return np.zeros(shape, dtype=bool)
        return rasterize([(geometries[i], 1) for i in indices], out_shape=shape, transform=transform,
                         all_touched=True, dtype="uint8").astype(bool)

    def clip(self, polygons, crs):
        import shapely

        geometries, tree = self.projected(crs)
        result = []
        for polygon in polygons:
            ids = tree.query(polygon, predicate="intersects")
            result.append(polygon.difference(shapely.union_all([geometries[i] for i in ids])) if len(ids) else polygon)
        return shapely.from_wkb([p.wkb for p in result])

    def report(self, mask):
        return {"sources": self.sources, "rule": "strict: every footprint pixel" if self.options.get("mode") == "strict" else "canopy: roof-height pixels removed, overhang kept",
                "excluded_pixels": int(mask.sum()), "grid_pixels": int(mask.size), "warning": WARNING,
                "options": self.options, "context": self.context, "comparison_years": sorted(self.years),
                "requested_date": self.options.get("date"), "observation_years": sorted(self.observed_years),
                "temporal_rule": "year only; a nearby snapshot counts as present; a lifecycle year inside the range stays unresolved",
                "recent_years": self.recent_years,
                "temporal_policy": self.options.get("temporal_policy", "recent"),
                "observation_date_known": self.observation_known,
                "temporal_status": {state: sum(self.temporal(i) == state for i in range(len(self.attributes)))
                                    for state in ("active", "absent", "unknown")}}

    def observe(self, years):
        if not years:
            years = self.context.get("observation_years", [])
        values = [int(y) for y in years if 1800 <= int(y) <= 2200]
        self.years.update(values)
        self.observed_years.update(values)
        self.observation_known = bool(values)

    def temporal(self, index):
        target = self.options.get("date")
        policy = self.options.get("temporal_policy", "recent")
        years = {year(target)} if target and policy == "recent" else self.years
        if not years or not (self.observation_known or target and policy == "recent"):
            return "unknown"
        attr = self.attributes[index]
        start = optional_year(attr["construction_year_field"])
        end = optional_year(attr["demolition_year_field"])
        if any(value is not None and str(value).lower() not in {"", "nan", "nat", "none"} and optional_year(value) is None
               for value in (attr["construction_year_field"], attr["demolition_year_field"])):
            return "unknown"
        snapshot = attr["snapshot_year"]
        lo, hi = min(years), max(years)
        if start is not None and hi < start or end is not None and lo > end:
            return "absent"
        if start is not None and lo <= start <= hi or end is not None and lo <= end <= hi:
            return "unknown"
        # A year-only construction date cannot establish presence throughout that year.
        if start is not None and start < lo and (end is not None and hi < end or snapshot is not None and hi <= snapshot):
            return "active"
        if policy == "recent" and snapshot is not None and max(abs(lo - snapshot), abs(hi - snapshot)) <= self.recent_years:
            return "active"
        return "unknown"

    def exclusion(self, heights, transform, crs):
        """Strict mask, or only roof-compatible heights supported by contemporaneous attributes."""
        import numpy as np
        from rasterio.features import rasterize

        if self.options.get("mode") == "strict":
            return self.mask(heights.shape, transform, crs)
        import shapely
        from rasterio.transform import array_bounds

        geometries, tree = self.projected(crs)
        removed = np.zeros(heights.shape, dtype=bool)
        ids = tree.query(shapely.box(*array_bounds(*heights.shape, transform)), predicate="intersects")
        for i in ids:
            geometry = geometries[i]
            height = number(self.attributes[i]["height_field"])
            if self.temporal(i) != "active" or height is None or height <= 0:
                continue
            footprint = rasterize([(geometry, 1)], out_shape=heights.shape, transform=transform, all_touched=True, dtype="uint8").astype(bool)
            bottom = number(self.attributes[i]["min_height_field"]) or 0.0
            # An elevated structure can have vegetation below it; a solid building cannot.
            removed |= footprint & (heights <= height + float(self.context.get("clearance_m", 2.0))) & (heights >= bottom)
        return removed

    def constrain_stems(self, table, polygons, labels, canopy, transform, terrain=None):
        """Keep overhang; reject unsupported roof centres instead of inventing trees at building edges."""
        import numpy as np
        import shapely
        from rasterio.features import rasterize

        geometries, tree = self.projected(self.crs)
        accepted = np.ones(len(polygons), dtype=bool)
        status = np.zeros(len(polygons), dtype="int16")
        shift = np.zeros(len(polygons), dtype="float64")
        rejected = []
        building_heights = np.full(len(polygons), np.nan)
        original_x, original_y = table["stem_x"].copy(), table["stem_y"].copy()
        for index, polygon in enumerate(polygons):
            ids = tree.query(polygon, predicate="intersects")
            if not len(ids):
                continue
            active = [i for i in ids if self.temporal(i) == "active"]
            known_heights = [number(self.attributes[i]["height_field"]) for i in active]
            known_heights = [h for h in known_heights if h is not None and h > 0]
            if known_heights:
                building_heights[index] = max(known_heights)
            unknown = [i for i in ids if self.temporal(i) == "unknown"]
            status[index] = 2 if unknown else 1 if active else 0
            stem = shapely.Point(original_x[index], original_y[index])
            solid = [i for i in active if not (number(self.attributes[i]["min_height_field"]) or 0) > 0]
            conflict = [i for i in solid if geometries[i].covers(stem)]
            unresolved = any(geometries[i].covers(stem) for i in unknown)
            reason = "temporal evidence unresolved at estimated stem" if unresolved else None
            if conflict:
                # Candidate location must be supported by the same crown outside solid footprints.
                from affine import Affine
                left, bottom, right, top = polygon.bounds
                c0, r0 = (~transform) * (left, top)
                c1, r1 = (~transform) * (right, bottom)
                r0, c0 = max(0, int(np.floor(r0)) - 1), max(0, int(np.floor(c0)) - 1)
                r1, c1 = min(labels.shape[0], int(np.ceil(r1)) + 1), min(labels.shape[1], int(np.ceil(c1)) + 1)
                cut = np.s_[r0:r1, c0:c1]
                forbidden = rasterize([(geometries[i], 1) for i in solid], out_shape=labels[cut].shape,
                                      transform=transform * Affine.translation(c0, r0), all_touched=True, dtype="uint8").astype(bool)
                supported = (labels[cut] == table["tree_id"][index]) & ~forbidden & np.isfinite(canopy[cut])
                rr, cc = np.nonzero(supported)
                rr, cc = rr + r0, cc + c0
                if not len(rr):
                    reason = "no same-crown support outside building"
                else:
                    values = canopy[rr, cc]
                    weights = np.maximum(values - (np.max(values) * 0.85), 0)
                    if not weights.sum():
                        weights = np.ones_like(values)
                    mean_r, mean_c = np.average(rr + 0.5, weights=weights), np.average(cc + 0.5, weights=weights)
                    at = np.argmin((rr + 0.5 - mean_r) ** 2 + (cc + 0.5 - mean_c) ** 2)
                    x, y = transform * (float(cc[at]) + 0.5, float(rr[at]) + 0.5)
                    distance = np.hypot(x - original_x[index], y - original_y[index])
                    if distance > float(self.context.get("max_stem_shift_m", 2.0)):
                        reason = "supported outside estimate exceeds declared shift limit"
                    elif not unresolved:
                        ground = terrain[rr[at], cc[at]] if terrain is not None else None
                        adjust_stem(table, index, (x, y), ground)
                        shift[index], status[index] = distance, 3
            if reason:
                accepted[index] = False
                rejected.append({"tree_id": int(table["tree_id"][index]), "reason": reason,
                                 "stem_x": float(original_x[index]), "stem_y": float(original_y[index]),
                                 "top_x": float(table["top_x"][index]), "top_y": float(table["top_y"][index]),
                                 "height_m": float(table["height_m"][index])})
        table["building_status"] = status
        table["stem_shift_m"] = shift
        table["building_height_m"] = building_heights
        table["unconstrained_stem_x"], table["unconstrained_stem_y"] = original_x, original_y
        keep_ids = table["tree_id"][accepted]
        lookup = np.zeros(int(labels.max()) + 1, dtype="uint32")
        lookup[keep_ids] = np.arange(1, len(keep_ids) + 1)
        labels = lookup[labels]
        table = {key: value[accepted] for key, value in table.items()}
        table["tree_id"] = np.arange(1, len(keep_ids) + 1, dtype="int32")
        return table, polygons[accepted], labels, {"rejected_candidates": rejected,
                "status_codes": {0: "no contemporaneous mapped overlap", 1: "overlap retained; not proof of vegetation",
                                 2: "overlap temporal evidence unresolved", 3: "constrained stem estimate; not measured"}}


def adjust_stem(table, index, position, ground):
    """Update geometry-dependent attributes; the old terrain slope no longer describes the stem."""
    import numpy as np

    x, y = position
    table["stem_x"][index], table["stem_y"][index] = x, y
    if "trunk_from_top_m" in table:
        table["trunk_from_top_m"][index] = np.hypot(x - table["top_x"][index], y - table["top_y"][index])
    if "slope_deg" in table:
        table["slope_deg"][index] = np.nan
    if ground is None:
        return
    table["height_m"][index] += table["ground_m"][index] - ground
    table["ground_m"][index] = ground
