"""Score a run against a LiDAR canopy-height raster."""
from __future__ import annotations

import math
from itertools import pairwise
from pathlib import Path

import numpy as np

from .grid import CELL_M, Grid
from .reduce import cell_top_and_mean, cells_within
from .table import BANDS, FINE_READINGS


def against(reading, truth) -> dict:
    """One reading against a measured height: n, MAE, RMSE, bias over all cells, by band of the measured height
    (how a stand of that height is read) and by band of the reading (how far off a cell is when the map says so)."""
    ok = np.isfinite(reading) & np.isfinite(truth)
    d, r, t = (reading - truth)[ok], reading[ok], truth[ok]

    def stats(part):
        e = d[part]
        return {"n": int(e.size), "mae": round(float(np.abs(e).mean()), 2), "rmse": round(float(np.sqrt((e ** 2).mean())), 2),
                "bias": round(float(e.mean()), 2), "median": round(float(np.median(e)), 2)}

    def banded(by):
        return {f"{lo}-{hi}": stats(s) for lo, hi in pairwise(BANDS) if (s := (by >= lo) & (by < hi)).sum() >= 50}

    if not d.size:
        return {"n": 0}
    return stats(np.ones(d.size, bool)) | {"by_measured": banded(t), "by_reading": banded(r)}


def against_fine(names: list[str], out: Path, lidar, grid: Grid, factor, counted, rasterio) -> dict:
    """The 1 m files against a LiDAR raster, pixel by pixel on the files' own grid, the raster read as the mean of its
    pixels in each of theirs. Three readings of a pixel: the file's; Meta's as read (the file's over its cell's
    factor); and the mean of the file over the pixel's 10 m cell, which is the file's level with its crowns taken out:
    where the file is no nearer than that, its 1 m detail adds nothing a 10 m map lacks. Pixels in the cells the 10 m
    validation `counted`; n, MAE, RMSE and bias of each reading, over all pixels and by band of the measured height,
    and of the mean of a cell from the file and from Meta's pixels as read against the raster's mean of the cell."""
    from rasterio.enums import Resampling
    from rasterio.vrt import WarpedVRT

    factor, counted, lows = factor.ravel(), counted.ravel(), np.array(BANDS[:-1])
    tally = {reading: np.zeros((len(lows), 4)) for reading in FINE_READINGS}
    sums, least = np.zeros((4, factor.size)), 0.0                 # per cell: pixels, and the sums of the raster, the file and Meta as read
    for name in names:
        with rasterio.open(out / name) as fine:
            level = cell_top_and_mean([fine], grid)[1].ravel()
            least = 0.5 * (CELL_M / abs(fine.transform.a)) ** 2
            with WarpedVRT(lidar, crs=fine.crs, transform=fine.transform, width=fine.width, height=fine.height, resampling=Resampling.average,
                           nodata=np.nan, dtype="float32") as measured:
                for v, (strip, rr, cc), cell in cells_within(fine, grid, rasterio.windows.Window(0, 0, fine.width, fine.height)):
                    truth = measured.read(1, window=strip)[rr, cc]
                    ok = np.isfinite(truth) & (truth > -1) & (truth < 150) & counted[cell] & np.isfinite(level[cell]) & (factor[cell] > 0)
                    truth, at = np.clip(truth[ok], 0, None), cell[ok]
                    band = np.searchsorted(lows, truth, side="right") - 1
                    for reading, values in (("crowns", v[ok]), ("as_read", v[ok] / factor[at]), ("flat", level[at])):
                        d = values - truth
                        tally[reading] += np.stack([np.bincount(band, weights=w, minlength=len(lows)) for w in (np.ones_like(d), np.abs(d), d * d, d)], 1)
                    sums += np.stack([np.bincount(at, weights=w, minlength=factor.size) for w in (np.ones_like(truth), truth, v[ok], v[ok] / factor[at])])

    def told(row) -> dict:
        n, absolute, squared, signed = row
        return {"n": int(n), "mae": round(absolute / n, 2), "rmse": round(math.sqrt(squared / n), 2), "bias": round(signed / n, 2)} if n else {"n": 0}

    whole = sums[0] >= max(least, 1)                              # a cell's mean needs half the pixels a cell expects, as its top does
    means = {reading: sums[i, whole] / sums[0, whole] - sums[1, whole] / sums[0, whole] for reading, i in (("crowns", 2), ("as_read", 3))}
    return {"pixels": int(tally["crowns"][:, 0].sum()),
            "readings": {reading: told(t.sum(0)) | {"by_measured": {f"{lo}-{hi}": told(t[i]) for i, (lo, hi) in enumerate(pairwise(BANDS)) if t[i, 0] >= 50}}
                         for reading, t in tally.items()},
            "cell_means": {reading: told((d.size, np.abs(d).sum(), (d * d).sum(), d.sum())) for reading, d in means.items()}}

