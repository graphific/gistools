"""Terrain from two public 30 m models. A file is read with canopy.on_grid."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from .canopy import Grid, on_grid

# Both are 30 m on EGM2008. The fetched terrain is their mean: a slope, not a survey height.
DEM_URL = "https://copernicus-dem-30m.s3.amazonaws.com/{name}/{name}.tif"
DEM_NAME = "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM"
BARE_URL = "https://s3.opengeohub.org/global/dtm/v1.2/{name}.tif"
BARE_NAME = "gedtm_rf_m_30m_s_20060101_20151231_go_epsg.4326.3855_v1.2"
DEM_TEXT, BARE_TEXT = "Copernicus DEM GLO-30", "GEDTM30"
GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "VSI_CACHE": "TRUE",
    "GDAL_HTTP_MAX_RETRY": "4",
    "GDAL_HTTP_RETRY_DELAY": "2",
}


def dem_tiles(west: float, south: float, east: float, north: float) -> list[str]:
    """The names of the Copernicus tiles, one a degree and named by its south-west corner, that touch a box in degrees."""
    return [DEM_NAME.format(ns="N" if lat >= 0 else "S", lat=abs(lat), ew="E" if lon >= 0 else "W", lon=abs(lon))
            for lat in range(math.floor(south), math.floor(north) + 1) for lon in range(math.floor(west), math.floor(east) + 1)]


def fetched_terrain(grid: Grid, crs, directory, rasterio, say):
    """(the terrain on the grid, what it is) from the two public 30 m models, read in windows from their own servers
    or from `directory`, a folder or address that holds the files under their own names. Where both are read the
    terrain is their mean (see DEM_URL); where one is, it is the terrain. (None, why) where the grid names no
    coordinate system or neither can be read: the run goes on without."""
    from rasterio.warp import transform_bounds

    if crs is None:
        return None, "none: the input names no coordinate system, so no terrain could be looked up"

    def source(name: str, public: str) -> str:
        if directory is None:
            return "/vsicurl/" + public.format(name=name)
        return f"/vsicurl/{str(directory).rstrip('/')}/{name}.tif" if "://" in str(directory) else str(Path(directory) / f"{name}.tif")

    def read(name: str, public: str):
        try:
            with rasterio.Env(**GDAL_ENV):
                return on_grid(source(name, public), grid, crs, rasterio)
        except rasterio.errors.RasterioIOError as e:                 # a degree of open sea has no file; a line that is down is said
            say(f"terrain: {name} could not be read ({str(e).splitlines()[0][:120]})")
            return None

    box = transform_bounds(crs, "EPSG:4326", grid.west, grid.north - grid.rows * grid.pixel, grid.west + grid.cols * grid.pixel, grid.north, densify_pts=21)
    say(f"terrain: reading {BARE_TEXT} and {DEM_TEXT} for the area")
    bare, top = read(BARE_NAME, BARE_URL), np.full((grid.rows, grid.cols), np.nan, "float32")
    for name in dem_tiles(*box):
        if (part := read(name, DEM_URL)) is not None:
            top = np.where(np.isfinite(top), top, part)
    held = [text for text, band in ((BARE_TEXT, bare), (DEM_TEXT, top)) if band is not None and np.isfinite(band).any()]
    if not held:
        return None, f"none: neither {BARE_TEXT} nor {DEM_TEXT} could be read for this area"
    if len(held) == 1:
        return (bare if held[0] == BARE_TEXT else top), f"{held[0]} (30 m), fetched"
    joined = np.where(np.isfinite(bare) & np.isfinite(top), (bare + top) / 2, np.where(np.isfinite(bare), bare, top))
    return joined.astype("float32"), f"the mean of {BARE_TEXT} and {DEM_TEXT} (both 30 m), fetched"
