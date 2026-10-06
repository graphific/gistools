"""Cloud-optimised 10 m GeoTIFF, tiled 1 m GeoTIFF, and replacing an earlier run."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .grid import Grid
from .reduce import cells_within, window_over
from .table import CELL_NOTE, FINE_TEXT, HEIGHT_BANDS, SOURCE_BANDS

OWN_RASTERS = ("chm_10m.tif", "chm_10m_source.tif", "chm_1m_*.tif")
ASIDE = ".replaced"
OWN_FILES = (
    *OWN_RASTERS,
    "chm_10m.qml", "chm_10m_source.qml", "chm_1m_*.qml", "chm_layers.qlr",
    "README.md", "report.json", "report.png", "validation.json",
)


def make_way(out: Path) -> list[Path]:
    """The rasters of an earlier run in `out`, moved aside so that this run can write under their names; their names
    are returned. GDAL deletes a file before it creates one of that name, and on a network share a file another
    program has open is not gone when deleted: its name stays, taken, until that program closes it, and creating it
    fails with "No such file or directory". A file moved aside frees its name at once and stays whole for the program
    that reads it. Where even that is refused the run stops before it reads anything."""
    aside = []
    for path in sorted(p for pattern in OWN_RASTERS for p in out.glob(pattern)):
        try:
            path.rename(path.with_name(path.name + ASIDE))
        except OSError as e:
            restore(aside)
            raise SystemExit(f"{path} of an earlier run cannot be replaced ({e.strerror}): another program has it open. Close it there (in QGIS, remove "
                             "the layer or close the project) and run again, or give another --out") from None
        aside.append(path)
    return aside


def restore(aside: list[Path]) -> None:
    """The earlier run's rasters back under their names, over whatever a run that did not end left there."""
    for path in aside:
        path.with_name(path.name + ASIDE).replace(path)


def clear_away(out: Path, aside: list[Path], written: set[str]) -> None:
    """After a run that ended: the earlier run's rasters go, and with them every file of this tool's that the run did
    not write, so the folder holds one run and its README names every file of it. A file of another name is not
    touched."""
    for path in aside:
        path.with_name(path.name + ASIDE).unlink()
    for path in sorted(p for pattern in OWN_FILES for p in out.glob(pattern)):
        if path.name not in written:
            path.unlink()

def write_tif(path: Path, grid: Grid, bands: dict, tags: dict, dtype: str = "float32", about: dict | None = None) -> None:
    """The bands as one cloud-optimised GeoTIFF with overviews. float32 has NaN for no data and averaged overviews; a
    whole-number type has no nodata value, each band's own being in the tags, and overviews of the nearest cell.
    `about` is {band: (unit, what it holds)}: written into the file as the band's unit and its DESCRIPTION, with the
    statistics of the cells that hold a value, so that a viewer names and stretches a band without a second file."""
    from rasterio.io import MemoryFile
    from rasterio.shutil import copy

    whole = dtype != "float32"
    profile = {"driver": "GTiff", "height": grid.height, "width": grid.width, "count": len(bands), "dtype": dtype, "crs": grid.crs,
               "transform": grid.transform, "nodata": None if whole else float("nan")}
    with MemoryFile() as memory:                     # the COG driver only copies, so the bands are named in memory first
        with memory.open(**profile) as dst:
            for i, (name, a) in enumerate(bands.items(), 1):
                dst.write(a.astype(dtype), i)
                dst.set_band_description(i, name)
                if about:
                    unit, text = about[name]
                    held = a[np.isfinite(a)]
                    dst.set_band_unit(i, unit)
                    dst.update_tags(i, DESCRIPTION=text, **({"STATISTICS_MINIMUM": float(held.min()), "STATISTICS_MAXIMUM": float(held.max()),
                                                           "STATISTICS_MEAN": float(held.mean()), "STATISTICS_STDDEV": float(held.std())} if held.size else {}))
            dst.update_tags(**tags)
        with memory.open() as src:
            copy(src, path, driver="COG", compress="deflate", predictor=2 if whole else 3,
                 overview_resampling="nearest" if whole else "average")


def fine_factor(meta_top, height, outside=None) -> np.ndarray:
    """What Meta's pixels in each cell are multiplied by for the 1 m file: the cell's calibrated height over its own
    top. 1 where Meta's top is 0 or the cell holds too few pixels to have one, so those pixels keep their value: there
    is no crown to scale. NaN in the cells `outside` the polygons asked for, which leaves their pixels out."""
    factor = np.where(meta_top > 0, height / np.maximum(meta_top, 1e-6), 1.0)
    return factor if outside is None else np.where(outside, np.nan, factor)


def write_fine(sources: list, urls: list[str], grid: Grid, factor, out: Path, tags: dict, rasterio,
               planes: dict | None = None, source: dict | None = None, buildings=None) -> tuple[list[str], list[str]]:
    """1 m height, one tiled GeoTIFF per Meta tile, on that tile's grid.

    Band 1 is Meta's pixel times `factor`. With `planes` and `source` (--debug), the other 10 m bands are written
    too: each 1 m pixel gets its 10 m cell's value.
    """
    from rasterio.enums import Resampling

    def overviews(path: Path, width, height, resampling) -> None:
        halvings = [2 ** k for k in range(1, 9) if max(width, height) >> k >= 256]
        if halvings:
            with rasterio.open(path, "r+") as dst:
                dst.build_overviews(halvings, resampling)

    debug = planes is not None
    factor = factor.ravel()
    float_bands = HEIGHT_BANDS if debug else HEIGHT_BANDS[:1]
    cell_value = {name: planes[name].ravel() for name, *_ in float_bands if name != "height_m"} if debug else {}
    source_value = {name: source[name].ravel() for name in SOURCE_BANDS} if source is not None else {}
    written, written_source = [], []
    for src, url in zip(sources, urls, strict=True):
        win = window_over(src, grid)
        if win is None or win.width < 1 or win.height < 1:
            continue
        stem = Path(url).stem
        base = {"driver": "GTiff", "height": int(win.height), "width": int(win.width), "crs": src.crs,
                "transform": src.window_transform(win), "compress": "deflate", "tiled": True}
        path = out / f"chm_1m_{stem}.tif"
        with rasterio.open(path, "w", **base, count=len(float_bands), dtype="float32", nodata=float("nan"), predictor=3) as dst:
            dst.update_tags(**tags)
            for i, (name, unit, _kind, text) in enumerate(float_bands, 1):
                dst.set_band_description(i, name)
                dst.set_band_unit(i, unit)
                dst.update_tags(i, DESCRIPTION=FINE_TEXT if name == "height_m" else f"{text}. {CELL_NOTE}")
            for v, (strip, rr, cc), cell in cells_within(src, grid, win):
                window = rasterio.windows.Window(0, strip.row_off - win.row_off, strip.width, strip.height)
                block = np.full((int(strip.height), int(strip.width)), np.nan, "float32")
                block[rr, cc] = v * factor[cell]
                excluded = buildings.exclusion(block, src.window_transform(strip), src.crs) if buildings is not None else None
                if excluded is not None:
                    block[excluded] = np.nan
                dst.write(block, 1, window=window)
                for i, (name, *_) in enumerate(float_bands, 1):
                    if name == "height_m":
                        continue
                    block.fill(np.nan)
                    block[rr, cc] = cell_value[name][cell]
                    if excluded is not None:
                        block[excluded] = np.nan
                    dst.write(block, i, window=window)
        overviews(path, win.width, win.height, Resampling.average)
        written.append(path.name)
        if not source_value:
            continue
        spath = out / f"chm_1m_{stem}_source.tif"
        with rasterio.open(spath, "w", **base, count=len(SOURCE_BANDS), dtype="uint16", predictor=2) as dst:
            dst.update_tags(**{k: v for k, v in tags.items() if k != "units"})
            for i, (name, (empty, text)) in enumerate(SOURCE_BANDS.items(), 1):
                dst.set_band_description(i, name)
                dst.update_tags(i, DESCRIPTION=f"{text}. {CELL_NOTE}", NODATA=str(empty))
            for _v, (strip, rr, cc), cell in cells_within(src, grid, win):
                heights = np.full((int(strip.height), int(strip.width)), np.nan, "float32")
                heights[rr, cc] = _v * factor[cell]
                excluded = buildings.exclusion(heights, src.window_transform(strip), src.crs) if buildings is not None else None
                window = rasterio.windows.Window(0, strip.row_off - win.row_off, strip.width, strip.height)
                for i, (name, (empty, _text)) in enumerate(SOURCE_BANDS.items(), 1):
                    block = np.full((int(strip.height), int(strip.width)), empty, "uint16")
                    block[rr, cc] = source_value[name][cell]
                    if excluded is not None:
                        block[excluded] = empty
                    dst.write(block, i, window=window)
        overviews(spath, win.width, win.height, Resampling.nearest)
        written_source.append(spath.name)
    return written, written_source

