"""Per-cell maximum and mean from a finer raster."""
from __future__ import annotations

import numpy as np

from .grid import CELL_M, Grid

# Rows read at a time, and the spacing of exact coordinates. The rest are interpolated.
STRIP, LATTICE = 512, 16


def to_grid(src, grid: Grid, win):
    """A function from pixel coordinates inside `win` of `src` to coordinates on the grid. Exact every LATTICE pixels
    and bilinear between: a projection bends by a few thousandths of a millimetre over that distance, so a pixel is
    put in another cell than an exact mapping would only when its centre lies that near a cell's edge."""
    from rasterio.warp import transform as warp_points

    t = src.window_transform(win)
    cols = np.unique(np.r_[np.arange(0, win.width, LATTICE), win.width]).astype("float64")
    rows = np.unique(np.r_[np.arange(0, win.height, LATTICE), win.height]).astype("float64")
    px, py = np.meshgrid(t.c + cols * t.a, t.f + rows * t.e)
    gx, gy = (np.array(v).reshape(px.shape) for v in warp_points(src.crs, grid.crs, px.ravel().tolist(), py.ravel().tolist()))

    def at(col, row):
        j = np.clip(np.searchsorted(cols, col, side="right") - 1, 0, len(cols) - 2)
        i = np.clip(np.searchsorted(rows, row, side="right") - 1, 0, len(rows) - 2)
        fx, fy = (col - cols[j]) / (cols[j + 1] - cols[j]), (row - rows[i]) / (rows[i + 1] - rows[i])
        return tuple((1 - fy) * ((1 - fx) * g[i, j] + fx * g[i, j + 1]) + fy * ((1 - fx) * g[i + 1, j] + fx * g[i + 1, j + 1])
                     for g in (gx, gy))
    return at


def cells_within(src, grid: Grid, win):
    """Per strip of `win`: (the source pixels' values, their place in the strip, the grid cell each one's centre falls
    in). Pixels outside 0-150 m, the file's nodata and those off the grid are left out."""
    from rasterio.windows import Window

    at = to_grid(src, grid, win)
    for r0 in range(0, int(win.height), STRIP):
        strip = Window(win.col_off, win.row_off + r0, win.width, min(STRIP, win.height - r0))
        a = src.read(1, window=strip).astype("float32")
        ok = np.isfinite(a) & (a > -1) & (a < 150)
        if src.nodata is not None and np.isfinite(src.nodata):
            ok &= a != src.nodata
        rr, cc = np.nonzero(ok)
        x, y = at(cc + 0.5, r0 + rr + 0.5)
        col = np.floor((x - grid.transform.c) / grid.transform.a).astype("int64")
        row = np.floor((y - grid.transform.f) / grid.transform.e).astype("int64")
        on = (row >= 0) & (row < grid.height) & (col >= 0) & (col < grid.width)
        yield np.clip(a[rr[on], cc[on]], 0, None), (strip, rr[on], cc[on]), row[on] * grid.width + col[on]


def window_over(src, grid: Grid):
    """The part of `src` over the grid, or None when they do not meet."""
    import rasterio.errors
    from rasterio.warp import transform_bounds
    from rasterio.windows import Window, from_bounds

    b = transform_bounds(grid.crs, src.crs, *grid.bounds, densify_pts=21)
    try:
        return from_bounds(*b, transform=src.transform).round_offsets().round_lengths().intersection(Window(0, 0, src.width, src.height))
    except rasterio.errors.WindowError:
        return None


def cell_top_and_mean(sources: list, grid: Grid) -> tuple[np.ndarray, np.ndarray]:
    """Per grid cell the highest and the mean of the source pixels whose centres fall in it, over every source; NaN
    where a cell holds under half the pixels its size expects."""
    n = grid.height * grid.width
    top, total, count, expected = np.full(n, -np.inf, "float32"), np.zeros(n), np.zeros(n), 0.0
    for src in sources:
        win = window_over(src, grid)
        if win is None or win.width < 1 or win.height < 1:
            continue
        expected = (CELL_M / abs(src.transform.a)) ** 2
        for v, _, cell in cells_within(src, grid, win):
            total += np.bincount(cell, weights=v, minlength=n)
            count += np.bincount(cell, minlength=n)
            order = np.argsort(cell, kind="stable")
            held, starts = np.unique(cell[order], return_index=True)
            top[held] = np.maximum(top[held], np.maximum.reduceat(v[order], starts))
    enough = (count >= 0.5 * expected) & (count > 0)
    shape = (grid.height, grid.width)
    return (np.where(enough, top, np.nan).reshape(shape).astype("float32"),
            np.where(enough, total / np.maximum(count, 1), np.nan).reshape(shape).astype("float32"))

