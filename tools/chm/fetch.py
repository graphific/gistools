"""Read ETH and Meta tiles, and Meta image dates."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .grid import Grid, eth_tiles, quadkeys

ETH_URL = (
    "https://libdrive.ethz.ch/index.php/s/cO8or7iOe5dT2Rt/download?path=%2F3deg_cogs&files="
    "ETH_GlobalCanopyHeight_10m_2020_{tile}_Map{sd}.tif"
)
META_URL = {
    "v2": ("https://dataforgood-fb-data.s3.amazonaws.com/forests/v2/global/dinov3_global_chm_v2_ml3/chm/{key}.tif", 10),
    "v1": ("https://dataforgood-fb-data.s3.amazonaws.com/forests/v1/alsgedi_global_v6_float/chm/{key}.tif", 9),
}
GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "VSI_CACHE": "TRUE",
    "GDAL_HTTP_MAX_RETRY": "4",
    "GDAL_HTTP_RETRY_DELAY": "2",
}


def remote(url: str) -> str:
    """The name GDAL reads a file by: in windows over HTTP, or a path as it is."""
    return "/vsicurl/" + url if "://" in url else url


def read_eth(grid: Grid, bbox, sd: bool, rasterio, template: str = ETH_URL) -> tuple[np.ndarray, list[str]]:
    """ETH's height (or its standard deviation) warped onto the grid, bilinear, and the files it came from."""
    from rasterio.errors import RasterioIOError
    from rasterio.warp import Resampling, reproject

    out, read = np.full((grid.height, grid.width), np.nan, "float32"), []
    for tile in eth_tiles(*bbox):
        url = template.format(tile=tile, sd="_SD" if sd else "")
        try:
            src = rasterio.open(remote(url))
        except RasterioIOError:                      # no such tile: sea, or beyond the product's 60 S to 84 N
            continue
        with src:
            part = np.full(out.shape, np.nan, "float32")
            reproject(rasterio.band(src, 1), part, dst_transform=grid.transform, dst_crs=grid.crs, src_nodata=255,
                      dst_nodata=np.nan, resampling=Resampling.bilinear)
        out = np.where(np.isfinite(part), part, out)
        read.append(url)
    return out, read

def open_meta(bbox, version: str, rasterio, template: str | None = None) -> tuple[list, list[str]]:
    """Meta's tiles over the box, opened, and their URLs. A tile the bucket does not hold is sea."""
    from rasterio.errors import RasterioIOError

    url_of, zoom = META_URL[version]
    url_of = template or url_of
    sources, read = [], []
    for key in quadkeys(*bbox, zoom):
        try:
            sources.append(rasterio.open(remote(url_of.format(key=key))))
        except RasterioIOError:
            continue
        read.append(url_of.format(key=key))
    return sources, read


def read_file(url: str, tries: int = 4) -> bytes | None:
    """A file's bytes, from a path or over HTTP; None where there is no such file. Any other failure is tried again
    and then raised: a date that could not be read must not pass for one that is not published."""
    import http.client
    import time
    import urllib.request

    if "://" not in url:
        return Path(url).read_bytes() if Path(url).exists() else None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=300) as response:
                return response.read()
        except (OSError, http.client.HTTPException) as e:
            if getattr(e, "code", None) in (403, 404):
                return None
            if attempt == tries - 1:
                raise
            print(f"  {type(e).__name__} reading {url}; trying again")
            time.sleep(2 * (attempt + 1))
    return None


def read_kept(url: str, cache: Path | None) -> tuple[bytes | None, bool]:
    """`read_file`, and whether the bytes came from `cache`: a folder that keeps what is read over HTTP under the
    URL's own path. A file is kept whole or not at all: it takes its name only once it is written."""
    kept = cache / url.split("://", 1)[1].lstrip("/") if cache and "://" in url else None
    if kept and kept.exists():
        return kept.read_bytes(), True
    raw = read_file(url)
    if kept and raw is not None:
        kept.parent.mkdir(parents=True, exist_ok=True)
        part = kept.with_name(kept.name + ".part")
        part.write_bytes(raw)
        part.replace(kept)
    return raw, False


def image_dates(tile_urls: list[str], grid: Grid, cache: Path | None = None) -> tuple[np.ndarray, list[str]]:
    """Per cell the date, as yyyymmdd, of the image Meta's height is made from, 0 where none is published; and the
    files read. Beside each tile Meta publishes polygons in degrees with one `acq_date` each: a cell takes the polygon
    its centre is in. The files are tens of megabytes and do not change, so `cache` keeps them between runs."""
    from rasterio.features import rasterize
    from rasterio.warp import transform_geom

    date, read = np.zeros((grid.height, grid.width), "uint32"), []
    for url in tile_urls:
        folder, _, name = url.rpartition("/chm/")
        dates_url = f"{folder}/metadata/{name.removesuffix('.tif')}.geojson"
        raw, kept = read_kept(dates_url, cache)
        if raw is None:
            continue
        print(f"  the dates of tile {name.removesuffix('.tif')}: {len(raw) / 1e6:.0f} MB" + (", from the cache" if kept else ""), flush=True)
        shapes = [(transform_geom("EPSG:4326", grid.crs, f["geometry"]), int(f["properties"]["acq_date"].replace("-", "")))
                  for f in json.loads(raw)["features"]]
        if shapes:
            burnt = rasterize(shapes, out_shape=date.shape, transform=grid.transform, fill=0, dtype="uint32")
            date = np.where(date == 0, burnt, date)
        read.append(dates_url)
    return date, read

