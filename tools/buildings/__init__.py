"""Building footprints for chm and crowns.

    from tools.buildings import from_args
    buildings, mask = from_args(["buildings.gpkg"], shape, transform, crs, {"mode": "canopy", "date": "2020"})
"""
from __future__ import annotations

from pathlib import Path

from .cli import arguments, options
from .mask import BuildingMask, adjust_stem
from .mosaic import prepare_canopy, raster_years
from .overture import automatic_buildings, overture_features, overture_files, public_json
from .read import number, optional_year

__all__ = [
    "BuildingMask", "adjust_stem", "arguments", "automatic_buildings", "from_args",
    "number", "optional_year", "options", "overture_features", "overture_files",
    "prepare_canopy", "public_json", "raster_years",
]


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
