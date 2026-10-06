"""Building constraints from supplied footprints or a pinned public Overture subset."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path

WARNING = ("Footprint completeness, registration and height accuracy are unverified. Missing footprints do not "
           "prove absence of buildings. Above-roof height is not proof of vegetation. Strict mode also removes "
           "overhanging canopy; canopy mode retains unresolved heights. No new canopy geometry is inferred.")


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
                                 "snapshot_basis": "declared context" if "snapshot_year" in self.context else "filename date token; publication proxy"})

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
        return {"sources": self.sources, "rule": "strict footprint exclusion" if self.options.get("mode") == "strict" else "dated roof-compatible heights excluded; overhang retained",
                "excluded_pixels": int(mask.sum()), "grid_pixels": int(mask.size), "warning": WARNING,
                "options": self.options, "context": self.context, "comparison_years": sorted(self.years),
                "requested_date": self.options.get("date"), "observation_years": sorted(self.observed_years),
                "temporal_rule": "year-level; recent snapshot presence is an assumption, lifecycle boundaries override it; requested date does not redate imagery",
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
    return None  # Approximate OSM dates remain unparsed evidence, never invented exact years.


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


def raster_years(path):
    """Acquisition metadata only; never infer observation time from a file's modification clock."""
    import numpy as np
    import rasterio

    years = set()
    with rasterio.open(path) as src:
        if src.tags().get("acquisition_dates_complete", "true").lower() == "false":
            return []
        if src.tags().get("acquisition_years"):
            years.update(year(v) for v in json.loads(src.tags()["acquisition_years"]))
        for key in ("acquisition_year", "acquisition_date"):
            if src.tags().get(key):
                years.add(year(src.tags()[key]))
        for index, name in enumerate(src.descriptions, 1):
            if name not in {"year", "meta_year", "eth_year"}:
                continue
            for _, window in src.block_windows(index):
                years.update(int(v) for v in np.unique(src.read(index, window=window)) if np.isfinite(v) and 1800 <= v <= 2200)
    report_path = Path(path).parent / "report.json"
    if not years and report_path.exists():
        report = json.loads(report_path.read_text())
        if Path(path).name in ["chm_10m.tif", *report.get("fine_files", [])]:
            provenance = report.get("provenance", {})
            if provenance.get("meta_cells_undated", 0) > 0:
                return []
            years.update(int(str(d)[:4]) for d in provenance.get("meta_image_dates", {}))
            if provenance.get("eth_year"):
                years.add(int(provenance["eth_year"]))
    return sorted(years)


def arguments(parser):
    parser.add_argument("--building-mode", choices=("canopy", "strict"), default=None,
                        help="canopy: dated roof-height evidence, retain possible overhang; strict: remove every footprint")
    parser.add_argument("--date", help="requested year, ISO date or today; does not change the CHM acquisition date")
    parser.add_argument("--building-context", type=Path, help="JSON with footprint snapshot year, attribute mapping and explicit tolerances")
    parser.add_argument("--building-time-policy", choices=("recent", "evidence"), default=None,
                        help="recent: assume mapped buildings present near snapshot year; evidence: require lifecycle evidence")


def options(args):
    target = getattr(args, "date", None)
    return {"mode": getattr(args, "building_mode", None) or "canopy",
            "date": datetime.now().astimezone().date().isoformat() if target == "today" else target,
            "context": getattr(args, "building_context", None),
            "cache": str((args.out / "inputs").resolve()),
            "temporal_policy": getattr(args, "building_time_policy", None) or "recent"}


def from_args(paths, shape, transform, crs, settings=None):
    """Return the resolved footprint object and mask, or two None values when disabled."""
    from rasterio.transform import array_bounds

    if not paths:
        return None, None
    if any(str(p) == "auto" for p in paths):
        if len(paths) != 1 or not settings or not settings.get("cache"):
            raise ValueError("Use --buildings auto alone, with an explicit output directory")
        from rasterio.warp import transform_bounds
        bounds = transform_bounds(crs, "EPSG:4326", *array_bounds(*shape, transform), densify_pts=21)
        paths = [automatic_buildings(bounds, Path(settings["cache"]))]
    buildings = BuildingMask(paths, array_bounds(*shape, transform), crs, settings)
    return buildings, buildings.mask(shape, transform, crs)


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def overture_features(bounds, cache):
    """Window public GeoParquet, retaining heights and provenance; never use ambient AWS credentials."""
    import duckdb
    import shapely

    release = public_json("https://stac.overturemaps.org/catalog.json").get("latest")
    if not isinstance(release, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.\d+", release):
        raise ValueError("Overture did not publish a recognisable latest release; supply a local building file")
    print(f"Buildings: Overture {release}; reading the AOI subset (first download may take several minutes)", flush=True)
    directory = cache / "duckdb-extensions"
    directory.mkdir(parents=True, exist_ok=True)
    urls = overture_files(bounds, release, cache)
    if not urls:
        return release, []
    west, south, east, north = bounds
    with duckdb.connect(config={"extension_directory": str(directory), "threads": 2, "memory_limit": "1GB",
                               "temp_directory": str(cache / "duckdb-scratch")}) as connection:
        connection.execute("INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs")
        connection.execute("SET s3_endpoint='s3.us-west-2.amazonaws.com'; SET s3_url_style='vhost'; SET s3_use_ssl=true")
        connection.execute("SET s3_region='us-west-2'; SET s3_access_key_id=''; SET s3_secret_access_key=''; SET s3_session_token=''")
        rows = connection.execute("""
            SELECT id, height, min_height, roof_height, to_json(sources), ST_AsWKB(geometry)
            FROM read_parquet(?)
            WHERE bbox.xmax >= ? AND bbox.xmin <= ? AND bbox.ymax >= ? AND bbox.ymin <= ?
            ORDER BY id
        """, [urls, west, east, south, north]).fetchall()
    features = [{"type": "Feature", "geometry": shapely.geometry.mapping(shapely.from_wkb(bytes(geometry))),
                 "properties": {"id": identifier, "height": height, "min_height": minimum, "roof_height": roof,
                                "sources": sources, "release": release}}
                for identifier, height, minimum, roof, sources, geometry in rows]
    print(f"Buildings: {len(features)} footprints read", flush=True)
    return release, features


def public_json(url):
    import urllib.request

    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def overture_files(bounds, release, cache):
    """Use every publisher file extent, including boundary intersections, before reading Parquet."""
    from concurrent.futures import ThreadPoolExecutor

    prefix = f"https://stac.overturemaps.org/{release}/buildings/building/"
    collection = public_json(prefix + "collection.json")
    items = [link["href"] for link in collection["links"] if link["rel"] == "item"]
    if not items or len(items) != collection["partition:file_count"] or any(not url.startswith(prefix) for url in items):
        raise ValueError("Overture file index is incomplete or outside the requested release")
    west, south, east, north = bounds
    selected, records = [], []
    asset_prefix = f"https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/release/{release}/theme=buildings/type=building/"
    with ThreadPoolExecutor(max_workers=8) as pool:
        for item in pool.map(public_json, items):
            left, bottom, right, top = item["bbox"]
            url = item["assets"]["aws"]["href"]
            if not url.startswith(asset_prefix) or not url.endswith(".parquet"):
                raise ValueError("Overture asset is outside the requested building release")
            intersects = right >= west and left <= east and top >= south and bottom <= north
            records.append({"id": item["id"], "bbox": item["bbox"], "href": url, "selected": intersects})
            if intersects:
                selected.append(url)
    (cache / f"overture_partitions__{release}.json").write_text(json.dumps(records, indent=2))
    print(f"Buildings: {len(selected)} of {len(items)} publisher files intersect the AOI", flush=True)
    return sorted(set(selected))


def automatic_buildings(bounds, cache):
    """Pin one AOI/release/file receipt per output directory; reruns do not silently refresh the source."""
    cache.mkdir(parents=True, exist_ok=True)
    receipt = cache / "buildings.json"
    bounds = [round(float(v), 9) for v in bounds]
    if receipt.exists():
        record = json.loads(receipt.read_text())
        path = cache / Path(record["file"]).name
        if record["bbox"] != bounds:
            raise ValueError("Cached buildings belong to another AOI; use a new output directory")
        if file_hash(path) != record["sha256"]:
            raise ValueError("Cached building file changed; inspect it before reusing the run")
        print(f"Buildings: reusing verified Overture {record['release']} subset", flush=True)
        return path
    release, features = overture_features(bounds, cache)
    path = cache / f"buildings_overture__{release[:10]}__src.geojson"
    payload = json.dumps({"type": "FeatureCollection", "features": features}, allow_nan=False).encode()
    temporary = path.with_suffix(".part")
    temporary.write_bytes(payload)
    temporary.replace(path)
    receipt.write_text(json.dumps({"bbox": bounds, "release": release, "file": path.name,
                                   "features": len(features), "sha256": file_hash(path)}, indent=2))
    return path


def prepare_canopy(args):
    """Accept a CHM run folder, inheriting its verified buildings and joining every fine tile once."""
    if not args.chm or not args.chm.is_dir():
        return
    directory = args.chm.resolve()
    if directory == args.out.resolve():
        raise ValueError("CHM and crown output directories must differ")
    report = json.loads((directory / "report.json").read_text())
    tiles = [directory / name for name in report.get("fine_files", [])]
    if not tiles or any(not p.is_file() or not p.resolve().is_relative_to(directory) for p in tiles):
        raise ValueError("CHM folder must contain every fine_files raster recorded in report.json")
    evidence = report.get("building_exclusion")
    if evidence and not getattr(args, "buildings", None):
        paths = []
        for source in evidence["sources"]:
            path = Path(source["path"])
            local = directory / "inputs" / path.name
            path = local if local.is_file() else path
            if file_hash(path) != source["sha256"]:
                raise ValueError("CHM building input changed; refusing to inherit a different source")
            paths.append(path)
        args.buildings = paths
        inherited = evidence["options"]
        for name, key in (("date", "date"), ("building_mode", "mode"), ("building_time_policy", "temporal_policy")):
            if getattr(args, name, None) is None:
                setattr(args, name, inherited.get(key))
        if not getattr(args, "building_context", None) and inherited.get("context"):
            context = Path(inherited["context"])
            if file_hash(context) != inherited["context_sha256"]:
                raise ValueError("CHM building context changed")
            args.building_context = context
    args.chm = tiles[0] if len(tiles) == 1 else mosaic_canopy(tiles, args.out / "inputs")


def mosaic_canopy(tiles, cache):
    """Disk-backed merge of band 1; dates describe every contributing tile, not just the first."""
    import math

    import rasterio
    from rasterio.merge import merge

    years, complete, extents, reference = set(), True, [], None
    for path in tiles:
        observed = raster_years(path)
        years.update(observed)
        complete &= bool(observed)
        with rasterio.open(path) as src:
            if reference is None:
                reference = (src.crs, src.res)
            if src.crs != reference[0] or src.res != reference[1] or src.transform.b or src.transform.d:
                raise ValueError("Fine CHM tiles must share a north-up CRS and resolution")
            extents.append(src.bounds)
    left, bottom = min(b.left for b in extents), min(b.bottom for b in extents)
    right, top = max(b.right for b in extents), max(b.top for b in extents)
    pixels = math.ceil((right - left) / reference[1][0]) * math.ceil((top - bottom) / reference[1][1])
    if pixels > 250_000_000:
        raise ValueError("Combined CHM exceeds the crown reader's 250 million pixel limit; use a smaller AOI")
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / "canopy_1m.tif"
    temporary = cache / "canopy_1m.part.tif"
    merge(tiles, indexes=[1], dst_path=temporary, mem_limit=256,
          dst_kwds={"compress": "deflate", "tiled": True, "blockxsize": 256, "blockysize": 256, "BIGTIFF": "IF_SAFER"})
    with rasterio.open(temporary, "r+") as dst:
        dst.update_tags(acquisition_years=json.dumps(sorted(years)), acquisition_dates_complete=str(complete),
                        input_tiles=json.dumps([str(p) for p in tiles]))
        dst.set_band_description(1, "height_m")
        dst.set_band_unit(1, "m")
    temporary.replace(path)
    return path
