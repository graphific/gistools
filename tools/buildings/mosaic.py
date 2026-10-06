"""Join the 1 m tiles of a chm run into one canopy for crowns."""
from __future__ import annotations

import json
from pathlib import Path

from .overture import file_hash
from .read import year

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
