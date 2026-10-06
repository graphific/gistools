"""10 m UTM grid, ETH tile names, Meta quadkeys."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

CELL_M = 10.0


@dataclass(frozen=True)
class Grid:
    """The 10 m grid everything is put on."""
    crs: object
    transform: object
    height: int
    width: int

    @property
    def bounds(self):
        t = self.transform
        return t.c, t.f + t.e * self.height, t.c + t.a * self.width, t.f


def eth_tiles(w, s, e, n) -> list[str]:
    """ETH's 3-degree tiles over a box, named by their south-west corner."""
    return [f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}{'E' if lon >= 0 else 'W'}{abs(lon):03d}"
            for lat in range(math.floor(s / 3) * 3, math.ceil(n / 3) * 3, 3)
            for lon in range(math.floor(w / 3) * 3, math.ceil(e / 3) * 3, 3)]


def _tile(lon, lat, zoom) -> tuple[int, int]:
    k = 2 ** zoom
    x = int((lon + 180) / 360 * k)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * k)
    return min(max(x, 0), k - 1), min(max(y, 0), k - 1)


def quadkeys(w, s, e, n, zoom) -> list[str]:
    """The web-mercator tiles over a box at `zoom`, as the quadkeys Meta names its files by."""
    (x0, y1), (x1, y0) = _tile(w, s, zoom), _tile(e, n, zoom)
    return ["".join(str(((x >> i) & 1) + 2 * ((y >> i) & 1)) for i in range(zoom - 1, -1, -1))
            for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]


def make_grid(w, s, e, n) -> Grid:
    """A 10 m grid in the area's UTM zone, its edges on multiples of 10 m, covering the box."""
    from affine import Affine
    from rasterio.crs import CRS
    from rasterio.warp import transform_bounds

    zone = int((((w + e) / 2 + 180) % 360) / 6) + 1
    crs = CRS.from_epsg((32600 if (s + n) / 2 >= 0 else 32700) + zone)
    x0, y0, x1, y1 = transform_bounds("EPSG:4326", crs, w, s, e, n, densify_pts=21)
    left, top = math.floor(x0 / CELL_M) * CELL_M, math.ceil(y1 / CELL_M) * CELL_M
    return Grid(crs, Affine(CELL_M, 0, left, 0, -CELL_M, top), math.ceil((top - y0) / CELL_M), math.ceil((x1 - left) / CELL_M))


def read_bbox(path: Path) -> tuple[tuple[float, float, float, float], list[dict]]:
    """The box of a GeoJSON file's coordinates (degrees), and its polygons, which the map is cut to."""
    doc = json.loads(path.read_text())
    geoms = [f["geometry"] for f in doc["features"]] if doc.get("type") == "FeatureCollection" else [doc.get("geometry", doc)]

    def points(c):
        return [c] if isinstance(c[0], (int, float)) else [p for part in c for p in points(part)]

    pts = [p for g in geoms for p in points(g["coordinates"])]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys)), [g for g in geoms if g["type"] in ("Polygon", "MultiPolygon")]

