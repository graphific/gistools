"""Canopy raster from a file or from LAS/LAZ points."""
from __future__ import annotations

import math
import warnings
from typing import NamedTuple

import numpy as np

from .delineate import NEIGHBOURS

# LAS: ground, and classes that are not canopy (building, noise, water, wire, bridge).
GROUND, NOT_CANOPY = 2, (6, 7, 9, 13, 14, 15, 16, 17, 18)
TERRAIN_CELL, TERRAIN_BLOCK, TERRAIN_MARGIN = 1.0, 512, 48
# A return is spread over this radius (m) so a pulse through the crown does not leave a one-cell pit.
SPREAD = 0.2
PIT, PIT_NEIGHBOURS = 1.5, 6
# First returns per m2, then pixel size (m).
PIXEL_BY_DENSITY = ((40.0, 0.25), (6.0, 0.5), (0.0, 1.0))
SPARSE = 2.0
POINT_CHUNK = 5_000_000
MAX_PIXELS = 250_000_000
# A pixel within this of a metre on the ground is used as it is. Else the raster is warped to UTM.
TRUE_SCALE = 0.02


class Grid(NamedTuple):
    """A north-up raster: the upper left corner, the pixel (m), its size."""
    west: float
    north: float
    pixel: float
    rows: int
    cols: int

    def cells(self, x, y):
        """(row, column, inside) of each point."""
        col, row = np.floor((x - self.west) / self.pixel).astype("int64"), np.floor((self.north - y) / self.pixel).astype("int64")
        return row, col, (row >= 0) & (row < self.rows) & (col >= 0) & (col < self.cols)


def pixel_for(first_returns_per_m2: float) -> float:
    """The pixel a canopy is made at from points of this density."""
    return next(pixel for least, pixel in PIXEL_BY_DENSITY if first_returns_per_m2 >= least)


class Heap:
    """What the points leave on a grid: the highest return of every cell, and the ground's returns on TERRAIN_CELL
    cells. Points are added in parts, so a file larger than memory is read in pieces."""

    def __init__(self, grid: Grid):
        self.grid = grid
        self.top = np.full((grid.rows, grid.cols), -np.inf)
        wide = TERRAIN_CELL / grid.pixel
        self.coarse = Grid(grid.west, grid.north, TERRAIN_CELL, int(np.ceil(grid.rows / wide)), int(np.ceil(grid.cols / wide)))
        self.ground_sum, self.ground_n = np.zeros((self.coarse.rows, self.coarse.cols)), np.zeros((self.coarse.rows, self.coarse.cols))
        self.points = self.first = 0

    def add(self, x, y, z, classification, return_number=None):
        """One part of the points. A return of a class that is no canopy is left out; the ground's count as both."""
        self.points += len(x)
        self.first += len(x) if return_number is None else int((return_number == 1).sum())
        ground = classification == GROUND
        row, col, inside = self.coarse.cells(x[ground], y[ground])
        np.add.at(self.ground_sum, (row[inside], col[inside]), z[ground][inside])
        np.add.at(self.ground_n, (row[inside], col[inside]), 1)
        keep = ~np.isin(classification, NOT_CANOPY)
        x, y, z = x[keep], y[keep], z[keep]
        turn = np.arange(8) * np.pi / 4
        for dx, dy in ((0.0, 0.0), *zip(SPREAD * np.cos(turn), SPREAD * np.sin(turn), strict=True)):
            row, col, inside = self.grid.cells(x + dx, y + dy)
            np.maximum.at(self.top, (row[inside], col[inside]), z[inside])

    def canopy(self, terrain=None):
        """(height above ground per cell, NaN where no return fell; the terrain): the highest return less the
        ground under the cell's middle. `terrain`: a raster on the grid to use instead of the ground's returns."""
        if terrain is None:
            terrain = self.terrain()
        chm = np.where(np.isfinite(self.top), np.maximum(self.top - terrain, 0), np.nan)
        return mend(chm), terrain

    def terrain(self):
        """The ground under every cell's middle: a plane through the three nearest ground cells around it (a TIN of
        the ground's cell means), the nearest ground cell's height outside them."""
        from scipy.interpolate import LinearNDInterpolator
        from scipy.ndimage import distance_transform_edt, map_coordinates

        known = self.ground_n > 0
        if not known.any():
            raise SystemExit("the points hold no ground returns (class 2): classify the ground first, give the terrain with --dem FILE, or say "
                             "--normalised when the heights are already above ground")
        mean = np.where(known, self.ground_sum / np.maximum(self.ground_n, 1), np.nan)
        rows, cols = mean.shape
        out = np.full(mean.shape, np.nan)
        for r0 in range(0, rows, TERRAIN_BLOCK):
            for c0 in range(0, cols, TERRAIN_BLOCK):
                core = (slice(r0, min(r0 + TERRAIN_BLOCK, rows)), slice(c0, min(c0 + TERRAIN_BLOCK, cols)))
                if known[core].all():
                    out[core] = mean[core]
                    continue
                r1, c1 = max(r0 - TERRAIN_MARGIN, 0), max(c0 - TERRAIN_MARGIN, 0)
                around = (slice(r1, min(r0 + TERRAIN_BLOCK + TERRAIN_MARGIN, rows)), slice(c1, min(c0 + TERRAIN_BLOCK + TERRAIN_MARGIN, cols)))
                r, c = np.nonzero(known[around])
                if len(r) < 3 or np.ptp(r) == 0 or np.ptp(c) == 0:
                    continue
                plane = LinearNDInterpolator(np.column_stack([r + r1, c + c1]), mean[around][r, c])
                rr, cc = np.mgrid[core]
                out[core] = np.where(known[core], mean[core], plane(rr, cc))
        hole = ~np.isfinite(out)
        if hole.any():
            r, c = distance_transform_edt(~known, return_distances=False, return_indices=True)
            out = np.where(hole, mean[r, c], out)
        wide = TERRAIN_CELL / self.grid.pixel
        rr, cc = np.mgrid[0:self.grid.rows, 0:self.grid.cols]
        return map_coordinates(out, [(rr + 0.5) / wide - 0.5, (cc + 0.5) / wide - 0.5], order=1, mode="nearest")


def mend(chm):
    """`chm` with its pits filled: a cell PIT below the middle value of its eight neighbours, PIT_NEIGHBOURS of them
    that far above it, takes that middle value; so does a cell no return fell in with that many neighbours that
    hold one."""
    rows, cols = chm.shape
    padded = np.pad(chm, 1, constant_values=np.nan)
    around = np.stack([padded[1 + dr: 1 + dr + rows, 1 + dc: 1 + dc + cols] for dr, dc in NEIGHBOURS])
    held = np.isfinite(around)
    with np.errstate(invalid="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)            # a pixel with no neighbour that holds a return has no middle value: NaN, as wanted
        middle = np.nanmedian(around, axis=0)
        above = (around > chm + PIT).sum(axis=0)
    pit = np.isfinite(chm) & (above >= PIT_NEIGHBOURS) & (middle - chm > PIT)
    empty = ~np.isfinite(chm) & (held.sum(axis=0) >= PIT_NEIGHBOURS)
    return np.where(pit | empty, middle, chm)


def ground_scale(src) -> tuple[float, float] | None:
    """Metres on the ground for one unit of the raster's coordinate system, along x and along y at its middle; None
    where it names no system."""
    from rasterio.warp import transform as warp_points

    if src.crs is None:
        return None
    (x0, y0), (x1, _), (_, y2) = (src.transform * at for at in ((src.width / 2, src.height / 2), (src.width / 2 + 1, src.height / 2), (src.width / 2, src.height / 2 + 1)))
    lon, lat = warp_points(src.crs, "EPSG:4326", [x0, x1, x0], [y0, y0, y2])

    def metres(i: int) -> float:
        half = (math.sin(math.radians(lat[i] - lat[0]) / 2) ** 2
                + math.cos(math.radians(lat[0])) * math.cos(math.radians(lat[i])) * math.sin(math.radians(lon[i] - lon[0]) / 2) ** 2)
        return 2 * 6_371_008.8 * math.asin(math.sqrt(half))

    return metres(1) / abs(x1 - x0), metres(2) / abs(y2 - y0)


def open_canopy(path: Path, rasterio):
    """(the raster as a north-up grid of square pixels in metres, the file it is opened from, what was done to it or
    None). A raster in degrees, in feet or in web mercator, or one that is turned, is warped to the UTM zone of its
    middle at its own size on the ground: every length the trees are told apart by is in metres."""
    from rasterio.enums import Resampling
    from rasterio.vrt import WarpedVRT
    from rasterio.warp import calculate_default_transform
    from rasterio.warp import transform as warp_points

    src = rasterio.open(path)
    t, scale = src.transform, ground_scale(src)
    plain = t.b == 0 and t.d == 0 and t.e < 0 and math.isclose(t.a, -t.e, rel_tol=1e-6)
    if scale is None:
        if not plain:
            raise SystemExit(f"{path} names no coordinate system and is not a north-up grid of square pixels: its pixels cannot be given a size")
        return src, src, "it names no coordinate system: a pixel is taken as its own size in metres"
    if plain and all(abs(v - 1) <= TRUE_SCALE for v in scale):
        return src, src, None
    x, y = t * (src.width / 2, src.height / 2)
    lon, lat = (v[0] for v in warp_points(src.crs, "EPSG:4326", [x], [y]))
    utm = f"EPSG:{(32600 if lat >= 0 else 32700) + int((lon + 180) // 6) % 60 + 1}"
    transform, width, height = calculate_default_transform(src.crs, utm, src.width, src.height, *src.bounds)
    warped = WarpedVRT(src, crs=utm, transform=transform, width=width, height=height, resampling=Resampling.bilinear, nodata=float("nan"), dtype="float32")
    return warped, src, f"warped from {src.crs.to_string()} to {utm} at {transform.a:.2f} m a pixel, its size on the ground"


def read_canopy(src) -> np.ndarray:
    """The raster's first band as heights: NaN where it holds none, 0 where it reads below the ground."""
    if src.width * src.height > MAX_PIXELS:
        raise SystemExit(f"the raster is {src.width * src.height / 1e6:.0f} million pixels, more than the {MAX_PIXELS / 1e6:.0f} million this tool holds at once: cut it into parts")
    band = src.read(1, masked=True)
    out = np.where(np.ma.getmaskarray(band), np.nan, np.ma.getdata(band)).astype("float32")
    out[~np.isfinite(out)] = np.nan
    return np.where(out < 0, 0, out)


def canopy_of_points(paths: list[Path], pixel: float | None, crs, terrain: Path | None, normalised: bool, rasterio, say):
    """(the canopy above ground, its grid, its coordinate system or None, the terrain it stands on or None, what was
    read) from LAS or LAZ files. The pixel is chosen from the first returns per m2 of the files' bounding boxes
    unless given. Heights are taken above the ground returns, above `terrain` (a raster) where given, or as they
    are (`normalised`: no terrain is then known from the points)."""
    import laspy

    headers = []
    for path in paths:
        with laspy.open(path) as f:
            headers.append(f.header)
    low, high = np.min([h.mins for h in headers], axis=0), np.max([h.maxs for h in headers], axis=0)
    first = sum(int(h.number_of_points_by_return[0]) or h.point_count for h in headers)
    density = first / max(sum((h.maxs[0] - h.mins[0]) * (h.maxs[1] - h.mins[1]) for h in headers), 1e-9)
    pixel = pixel or pixel_for(density)
    west, north = math.floor(low[0] / pixel) * pixel, math.ceil(high[1] / pixel) * pixel
    grid = Grid(west, north, pixel, math.ceil((north - low[1]) / pixel), math.ceil((high[0] - west) / pixel))
    if grid.rows * grid.cols > MAX_PIXELS:
        raise SystemExit(f"the points cover {grid.rows * grid.cols / 1e6:.0f} million pixels of {pixel} m, more than the {MAX_PIXELS / 1e6:.0f} million this tool "
                         "holds at once: give fewer files, or a larger --pixel")
    if crs is None and (found := headers[0].parse_crs()) is not None:
        crs = rasterio.crs.CRS.from_wkt(found.to_wkt())
    if crs is not None and crs.is_geographic:
        raise SystemExit("the points are in degrees: project them first, the trees are told apart by lengths in metres")
    heap = Heap(grid)
    for path in paths:
        say(f"reading {path.name}")
        with laspy.open(path) as f:
            for part in f.chunk_iterator(POINT_CHUNK):
                heap.add(np.asarray(part.x), np.asarray(part.y), np.asarray(part.z), np.asarray(part.classification), np.asarray(part.return_number))
    if normalised:
        ground = np.zeros((grid.rows, grid.cols))
    elif terrain is not None:
        ground = on_grid(terrain, grid, crs, rasterio).astype("float64")
    else:
        ground = None
    chm, ground = heap.canopy(ground)
    return chm.astype("float32"), grid, crs, None if normalised else ground.astype("float32"), {
        "files": [p.name for p in paths], "points": heap.points, "first_returns_per_m2": round(density, 2),
        "heights_above": "the heights as given" if normalised else terrain.name if terrain else "the ground returns (class 2)"}


def on_grid(path, grid: Grid, crs, rasterio) -> np.ndarray:
    """A raster's first band read onto the grid, between its own pixels (bilinear); NaN where it holds none."""
    from affine import Affine
    from rasterio.enums import Resampling
    from rasterio.vrt import WarpedVRT

    with rasterio.open(path) as src, WarpedVRT(src, crs=crs or src.crs, transform=Affine(grid.pixel, 0, grid.west, 0, -grid.pixel, grid.north), width=grid.cols,
                                               height=grid.rows, resampling=Resampling.bilinear, nodata=float("nan"), dtype="float32") as onto:
        return onto.read(1)
