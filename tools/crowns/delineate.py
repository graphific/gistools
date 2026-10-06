"""Tops and crowns on one canopy-height array. Lengths are metres."""
from __future__ import annotations

import warnings
from typing import NamedTuple

import numpy as np

# Eight neighbours, and the four that share a side.
NEIGHBOURS = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
SIDES = ((-1, 0), (0, -1), (0, 1), (1, 0))
# On a flat stretch the wider smooth decides which way a pixel climbs.
TIE = 1e-3
LEAST_WINDOW = 3.0
WINDOW_CHUNK = 20_000
# Dalponte: a crown pixel may stand this far above its top.
ABOVE_SEED = 0.05
CROWNS = ("watershed", "dalponte", "silva")
# Slope under a trunk is the terrain smoothed over this radius (m).
SLOPE_OVER = 2.0


class Rule(NamedTuple):
    """How tops are told apart and crowns drawn; lengths in metres, shares of a tree's own height."""
    crowns: str = "watershed"
    min_height: float = 2.0             # a tree lower than this is not looked for; a pixel lower is no canopy
    smooth: float = 0.5                 # the Gaussian the tops are read under (standard deviation)
    window: float = 1.0                 # a top is the highest pixel of a round window this wide
    window_share: float = 0.035         # ... plus this share of its height
    stands: float = 0.0                 # and stands this far above its highest pass to higher canopy
    stands_share: float = 0.0           # ... or this share of its height, whichever is more
    floor: float = 0.5                  # `watershed`: a pixel lower than this share of its tree's height is not its crown
    seed_share: float = 0.45            # `dalponte`: a crown grows over pixels higher than this share of its top
    crown_share: float = 0.55           # ... and than this share of the crown's mean height so far
    reach: float = 10.0                 # ... within this distance of the top, along both axes
    exclusion: float = 0.3              # `silva`: a pixel lower than this share of the highest of its tree is left out
    reach_share: float = 0.6            # ... and one further from the top than this share of that height
    min_area: float = 2.0               # a crown smaller than this (m2) is no tree
    hole: float = 4.0                   # a gap inside one crown up to this large (m2) is the crown's
    stem_share: float = 0.15


class Trees(NamedTuple):
    """`crowns`: the tree's number per pixel, 0 none. One entry a tree, tree k at [k - 1], in the others."""
    crowns: np.ndarray
    row: np.ndarray                     # the top, in pixels from the raster's upper left corner (0.5 is a pixel's middle)
    col: np.ndarray
    height: np.ndarray                  # the raster's highest value at the top (m)
    rise: np.ndarray                    # how far the top stands above its highest pass to higher canopy (m)
    area: np.ndarray                    # the crown (m2)
    mean_height: np.ndarray             # the raster's mean over the crown (m)
    edge: np.ndarray                    # the crown touches the raster's edge or a hole in it: it may be cut there
    stem_row: np.ndarray                # where the trunk is expected to stand, in pixels as the top is
    stem_col: np.ndarray
    ground: np.ndarray                  # the terrain under the trunk (m), NaN without a terrain
    slope: np.ndarray


def surface(chm, pixel: float, smooth_m: float):
    """The surface the tops are read from: `chm` under a Gaussian of `smooth_m` metres, a hole left out of the mean
    it would lower. NaN where `chm` holds none."""
    from scipy.ndimage import gaussian_filter

    held = np.isfinite(chm)
    filled, weight = np.where(held, chm, 0).astype("float64"), held.astype("float64")

    def under(sd):
        return gaussian_filter(filled, sd, mode="nearest") / np.maximum(gaussian_filter(weight, sd, mode="nearest"), 1e-9)

    sd = max(smooth_m / pixel, 0.0)
    out = (under(sd) if sd else filled) + TIE * under(3 * max(sd, 1.0))
    return np.where(held, out, np.nan)


def climb(level, canopy):
    """Every canopy pixel walked up its steepest rise to a top: (the top's number per pixel, 0 off the canopy; the flat
    index of each top's pixel, top k at [k - 1]). A level patch no pixel of which has a higher neighbour is one top."""
    from scipy.ndimage import label

    rows, cols = level.shape
    ground = np.where(canopy, level, -np.inf)
    padded = np.pad(ground, 1, constant_values=-np.inf)
    best, step = np.zeros(level.shape), np.zeros(level.shape, "int64")
    for dr, dc in NEIGHBOURS:
        with np.errstate(invalid="ignore"):
            rise = (padded[1 + dr: 1 + dr + rows, 1 + dc: 1 + dc + cols] - ground) / np.hypot(dr, dc)
        higher = rise > best
        best[higher], step[higher] = rise[higher], dr * cols + dc
    to = (np.arange(rows * cols).reshape(level.shape) + step).ravel()
    while True:                                                  # a pixel's next becomes its next's next: the walk halves each time
        further = to[to]
        if np.array_equal(further, to):
            break
        to = further
    marks, _ = label((step == 0) & canopy, structure=np.ones((3, 3)))
    tops = np.where(canopy, marks.ravel()[to].reshape(level.shape), 0)
    at = np.flatnonzero(marks)
    _, first = np.unique(marks.ravel()[at], return_index=True)
    return tops, at[first]


def passes(tops, level, count: int):
    """The highest pass between every two tops whose pixels touch: (a, b, height), a < b. A pass is the lower pixel
    of two neighbours that climb to different tops; the highest such pair is where the two canopies join."""
    keys, heights = [], []
    for a, b, ha, hb in ((tops[:, :-1], tops[:, 1:], level[:, :-1], level[:, 1:]), (tops[:-1], tops[1:], level[:-1], level[1:]),
                         (tops[:-1, :-1], tops[1:, 1:], level[:-1, :-1], level[1:, 1:]), (tops[:-1, 1:], tops[1:, :-1], level[:-1, 1:], level[1:, :-1])):
        touch = (a != b) & (a > 0) & (b > 0)
        low, high = np.minimum(a[touch], b[touch]).astype("int64"), np.maximum(a[touch], b[touch]).astype("int64")
        keys.append(low * (count + 1) + high)
        heights.append(np.minimum(ha[touch], hb[touch]))
    key, height = np.concatenate(keys), np.concatenate(heights)
    order = np.argsort(key, kind="stable")
    pair, start = np.unique(key[order], return_index=True)
    if not len(pair):
        return pair, pair, np.zeros(0)
    return pair // (count + 1), pair % (count + 1), np.maximum.reduceat(height[order], start)


def rises(height, a, b, saddle):
    """How far each top stands above the highest pass that leads from it to higher canopy (its prominence); its whole
    height where no pass does. `height` holds one entry a top, top k at [k]; [0] is unused. The passes are taken from
    the highest down and every one joins two canopies: the top of the lower of the two ends there."""
    count = len(height) - 1
    order = np.argsort(-saddle, kind="stable")
    canopy, peak, peak_top, rise = list(range(count + 1)), height.tolist(), list(range(count + 1)), height.tolist()
    for p, q, s in zip(a[order].tolist(), b[order].tolist(), saddle[order].tolist(), strict=True):
        x, y = root(canopy, p), root(canopy, q)
        if x == y:
            continue
        if peak[x] < peak[y]:
            x, y = y, x
        rise[peak_top[y]] = peak[y] - s
        canopy[y] = x
    return np.array(rise)


def root(of: list, i: int) -> int:
    """The canopy `i` has been joined to, the chain to it shortened on the way."""
    while of[i] != i:
        of[i] = of[of[i]]
        i = of[i]
    return i


def join(height, a, b, saddle, marked):
    """Which marked top carries the canopy each top lies in (0: none). The passes are taken from the highest down, as
    water would rise over the canopy turned upside down: two canopies become one at their pass unless both hold a
    marked top already. A canopy that never met a marked top is no tree."""
    count = len(height) - 1
    order = np.argsort(-saddle, kind="stable")
    canopy, carrier = list(range(count + 1)), np.where(marked, np.arange(count + 1), 0).tolist()
    for p, q in zip(a[order].tolist(), b[order].tolist(), strict=True):
        x, y = root(canopy, p), root(canopy, q)
        if x == y or (carrier[x] and carrier[y]):
            continue
        carrier[x] = carrier[x] or carrier[y]
        canopy[y] = x
    out = np.array([carrier[root(canopy, k)] for k in range(count + 1)])
    out[0] = 0
    return out


def window_tops(level, canopy, at, pixel: float, rule: Rule):
    """One flag a top: it is the highest pixel of the round window `rule.window` + `rule.window_share` of its height
    wide around it (Popescu & Wynne 2004), never smaller than LEAST_WINDOW pixels: with both at 0, every top."""
    cols = level.shape[1]
    r, c = np.divmod(at, cols)
    own = level.ravel()[at]
    radius = np.maximum((rule.window + rule.window_share * own) / pixel, LEAST_WINDOW) / 2
    reach = int(np.floor(radius.max())) if len(at) else 1
    dr, dc = (v.ravel() for v in np.mgrid[-reach: reach + 1, -reach: reach + 1])
    far = np.hypot(dr, dc)
    dr, dc, far = dr[far > 0], dc[far > 0], far[far > 0]
    padded = np.pad(np.where(canopy, level, -np.inf), reach, constant_values=-np.inf)
    out = np.zeros(len(at), bool)
    for i in range(0, len(at), WINDOW_CHUNK):
        part = slice(i, i + WINDOW_CHUNK)
        around = padded[(r[part, None] + reach + dr), (c[part, None] + reach + dc)]
        out[part] = (np.where(far <= radius[part, None], around, -np.inf) <= own[part, None]).all(axis=1)
    return out


def grown(level, canopy, seeds, pixel: float, rule: Rule):
    """Crowns grown from the tops (Dalponte & Coomes 2016, as lidR's `dalponte2016` grows them): a pixel beside a
    crown joins it when it is higher than `seed_share` of the top and `crown_share` of the crown's mean so far, no
    higher than the top by more than ABOVE_SEED, and within `reach` of the top along both axes. `seeds` is the flat
    index of each top; crown k is grown from seeds[k - 1]."""
    rows, cols = level.shape
    region = np.zeros(level.shape, "int32")
    region.ravel()[seeds] = np.arange(1, len(seeds) + 1)
    top = np.concatenate([[0.0], level.ravel()[seeds]])
    top_r, top_c = (np.concatenate([[0], v]) for v in np.divmod(seeds, cols))
    total, n = top.copy(), np.ones(len(seeds) + 1)
    reach = rule.reach / pixel
    r, c = np.indices(level.shape)
    while True:
        before, more = region.copy(), False
        for dr, dc in SIDES:
            beside = np.zeros_like(before)
            beside[max(dr, 0): rows + min(dr, 0), max(dc, 0): cols + min(dc, 0)] = before[max(-dr, 0): rows + min(-dr, 0), max(-dc, 0): cols + min(-dc, 0)]
            open_ = np.flatnonzero((region == 0) & (beside > 0) & canopy)
            crown, h = beside.ravel()[open_], level.ravel()[open_]
            take = ((h > top[crown] * rule.seed_share) & (h > total[crown] / n[crown] * rule.crown_share) & (h <= top[crown] * (1 + ABOVE_SEED))
                    & (np.abs(r.ravel()[open_] - top_r[crown]) < reach) & (np.abs(c.ravel()[open_] - top_c[crown]) < reach))
            region.ravel()[open_[take]] = crown[take]
            np.add.at(total, crown[take], h[take])
            np.add.at(n, crown[take], 1)
            more |= bool(take.any())
        if not more:
            return region


def nearest(level, canopy, seeds, pixel: float, rule: Rule):
    """Crowns as the pixels nearest each top (Silva et al. 2016, as lidR's `silva2016`): of those, the ones no lower
    than `exclusion` of the highest pixel nearest that top and no further from the top than `reach_share` of it."""
    from scipy.ndimage import distance_transform_edt, maximum

    away = np.ones(level.shape, bool)
    away.ravel()[seeds] = False
    far, (r, c) = distance_transform_edt(away, return_indices=True)
    number = np.zeros(level.size, "int32")
    number[seeds] = np.arange(1, len(seeds) + 1)
    region = np.where(canopy, number[r * level.shape[1] + c], 0)
    highest = np.concatenate([[0.0], np.atleast_1d(maximum(np.where(canopy, level, 0), region, index=np.arange(1, len(seeds) + 1)))])
    region[(level < rule.exclusion * highest[region]) | (far * pixel > rule.reach_share * highest[region])] = 0
    return region


def whole_crowns(region, seeds, pixel: float, rule: Rule):
    """`region` with every crown made one piece and renumbered: of a crown's pixels only those joined by their sides
    to its top are kept, a gap one crown encloses is given to it when no larger than `rule.hole`, and a crown under
    `rule.min_area` goes. Returns (the crowns, numbered from 1; for each, the crown of `region` it was)."""
    from scipy.ndimage import label

    rows, cols = region.shape
    lattice = np.zeros((2 * rows + 1, 2 * cols + 1), bool)         # pixels at the odd places, the side between two at the even place between
    lattice[1::2, 1::2] = region > 0
    lattice[1::2, 2:-1:2] = (region[:, :-1] == region[:, 1:]) & (region[:, :-1] > 0)
    lattice[2:-1:2, 1::2] = (region[:-1] == region[1:]) & (region[:-1] > 0)
    piece = label(lattice)[0][1::2, 1::2]
    of_top = np.zeros(len(seeds) + 1, piece.dtype)
    of_top[region.ravel()[seeds]] = piece.ravel()[seeds]            # a top its own crown no longer holds leaves 0: the crown goes
    of_top[0] = 0
    out = np.where((piece == of_top[region]) & (piece > 0), region, 0)

    gap, gaps = label(out == 0, structure=np.ones((3, 3)))
    if gaps:
        size = np.bincount(gap.ravel(), minlength=gaps + 1)
        low, high = np.full(gaps + 1, np.iinfo("int32").max), np.zeros(gaps + 1, "int64")
        for dr, dc in SIDES:
            mine = gap[max(dr, 0): rows + min(dr, 0), max(dc, 0): cols + min(dc, 0)]
            theirs = out[max(-dr, 0): rows + min(-dr, 0), max(-dc, 0): cols + min(-dc, 0)]
            beside = (mine > 0) & (theirs > 0)
            np.minimum.at(low, mine[beside], theirs[beside])
            np.maximum.at(high, mine[beside], theirs[beside])
        open_ = np.zeros(gaps + 1, bool)
        open_[np.unique(np.concatenate([gap[0], gap[-1], gap[:, 0], gap[:, -1]]))] = True
        inside = (low == high) & ~open_ & (size * pixel ** 2 <= rule.hole)
        inside[0] = False
        out = np.where(inside[gap], high[gap], out).astype("int32")

    area = np.bincount(out.ravel(), minlength=len(seeds) + 1) * pixel ** 2
    keep = area >= rule.min_area
    keep[0] = False
    was = np.flatnonzero(keep)
    number = np.zeros(len(seeds) + 1, "int32")
    number[was] = np.arange(1, len(was) + 1)
    return number[out], was


def delineate(chm, pixel: float, rule: Rule | None = None, terrain=None) -> Trees:
    """Trees in a canopy-height array (metres above ground, NaN empty). `pixel` is metres.

    A top is a high point that wins its window and stands far enough above its pass; either test is off at 0.
    `watershed` joins canopies over their highest pass. `dalponte` and `silva` grow from the tops.
    With `terrain`, each crown is set back on the ground before the top and the trunk are read."""
    rule = rule or Rule()
    level = surface(chm, pixel, rule.smooth)
    canopy = np.isfinite(level) & (level >= rule.min_height)
    tops, at = climb(level, canopy)
    count, cols = len(at), chm.shape[1]
    height = np.concatenate([[0.0], level.ravel()[at]])
    a, b, saddle = passes(tops, level, count)
    rise = rises(height, a, b, saddle)
    marked = np.concatenate([[False], window_tops(level, canopy, at, pixel, rule)]) & (rise >= np.maximum(rule.stands, rule.stands_share * height))
    held = np.isfinite(chm)
    raw = np.where(held, chm, -np.inf)                               # a crown's extent is read off the raster as given: smoothing widens it
    around = np.pad(raw, 1, constant_values=-np.inf)
    seeds = np.concatenate([[0], at])                                # a crown is numbered by the top that carries it
    tall = np.concatenate([[0.0], np.max([around[at // cols + 1 + dr, at % cols + 1 + dc] for dr, dc in ((0, 0), *NEIGHBOURS)], axis=0)]) if count else np.zeros(1)
    if rule.crowns == "watershed":
        region = join(height, a, b, saddle, marked)[tops]
        region[raw < np.maximum(rule.min_height, rule.floor * tall[region])] = 0
    else:
        chosen = np.flatnonzero(marked)
        region = np.concatenate([[0], chosen])[(grown if rule.crowns == "dalponte" else nearest)(raw, raw >= rule.min_height, at[chosen - 1], pixel, rule)]
    crowns, was = whole_crowns(region, seeds[1:], pixel, rule)
    row, col = np.divmod(seeds[was], cols)
    n = len(was)
    area = np.bincount(crowns.ravel(), minlength=n + 1)[1:]
    total = np.bincount(crowns.ravel(), weights=np.where(held, chm, 0).ravel(), minlength=n + 1)[1:]
    cut = np.zeros(n + 1, bool)
    off = np.pad(~held, 1, constant_values=True)
    for dr, dc in SIDES:
        cut[crowns[off[1 + dr: 1 + dr + chm.shape[0], 1 + dc: 1 + dc + chm.shape[1]]]] = True
    top_row, top_col, stem_row, stem_col, tall, ground, slope = trunks(crowns, level, raw, terrain, row, col, pixel, rule)
    return Trees(crowns, top_row, top_col, tall, rise[was], area * pixel ** 2, total / np.maximum(area, 1), cut[1:], stem_row, stem_col, ground, slope)


def trunks(crowns, level, raw, terrain, row, col, pixel: float, rule: Rule):
    """Top, expected trunk, height, ground and slope. Rows and columns are pixels from the raster corner.

    A canopy height is above the ground under each pixel, so on a slope the highest pixel sits downhill of the tree.
    With a terrain the crown is put back on that ground, smoothed the same way as the canopy, and the top is the
    highest pixel of the sum. The trunk is the weighted middle of the upper `stem_share` of the crown (0: under the
    top). Height is that top minus the ground under the trunk."""
    from scipy.ndimage import (
        distance_transform_edt,
        gaussian_filter,
        map_coordinates,
        maximum_position,
    )
    from scipy.ndimage import maximum as highest
    from scipy.ndimage import minimum as lowest

    n, (rows, cols) = len(row), crowns.shape
    if not n:
        return (np.zeros(0),) * 7
    index = np.arange(1, n + 1)
    lie = None
    if terrain is not None and np.isfinite(terrain).any():
        known = np.isfinite(terrain)
        if not known.all():                                          # a pixel the terrain does not hold takes the nearest that it does
            near = distance_transform_edt(~known, return_distances=False, return_indices=True)
            terrain = terrain[tuple(near)]
        lie = terrain.astype("float64")
        soft = gaussian_filter(lie, rule.smooth / pixel, mode="nearest") if rule.smooth > 0 else lie
        stood = np.where(crowns > 0, np.nan_to_num(level, nan=-np.inf) + soft, -np.inf)
        at = np.array(maximum_position(stood, crowns, index)).reshape(-1, 2)
        row, col = at[:, 0], at[:, 1]
    surface_ = np.nan_to_num(level, nan=0.0) + (soft if lie is not None else 0.0)
    top_row, top_col = row + 0.5 + shift(surface_, row, col, 0), col + 0.5 + shift(surface_, row, col, 1)
    inside = crowns > 0
    top = np.atleast_1d(highest(np.where(inside, surface_, -np.inf), crowns, index))
    low = np.atleast_1d(lowest(np.where(inside, surface_, np.inf), crowns, index))
    start = np.concatenate([[np.inf], top - rule.stem_share * (top - low)])
    weight = np.where(inside, np.clip(surface_ - start[crowns], 0, None), 0.0)
    rr, cc = np.indices(crowns.shape)
    heft = np.bincount(crowns.ravel(), weights=weight.ravel(), minlength=n + 1)[1:]
    with np.errstate(invalid="ignore", divide="ignore"):
        stem_row = np.where(heft > 0, np.bincount(crowns.ravel(), weights=(weight * (rr + 0.5)).ravel(), minlength=n + 1)[1:] / heft, top_row)
        stem_col = np.where(heft > 0, np.bincount(crowns.ravel(), weights=(weight * (cc + 0.5)).ravel(), minlength=n + 1)[1:] / heft, top_col)
    off = crowns[np.clip(stem_row.astype("int64"), 0, rows - 1), np.clip(stem_col.astype("int64"), 0, cols - 1)] != index
    stem_row, stem_col = np.where(off, top_row, stem_row), np.where(off, top_col, stem_col)    # a middle that falls outside its crown: the top instead
    around = np.pad(raw + (lie if lie is not None else 0.0), 1, constant_values=-np.inf)
    tall = np.max([around[row + 1 + dr, col + 1 + dc] for dr, dc in ((0, 0), *NEIGHBOURS)], axis=0)
    if lie is None:
        return top_row, top_col, stem_row, stem_col, tall, np.full(n, np.nan), np.full(n, np.nan)
    down, across = (gaussian_filter(way, SLOPE_OVER / pixel, mode="nearest") for way in np.gradient(lie, pixel))
    steep = np.degrees(np.arctan(np.hypot(down, across)))
    here = [stem_row - 0.5, stem_col - 0.5]
    ground = map_coordinates(lie, here, order=1, mode="nearest")
    return top_row, top_col, stem_row, stem_col, tall - ground, ground, map_coordinates(steep, here, order=1, mode="nearest")


def shift(level, row, col, axis: int):
    """How far from its pixel's middle a top lies along one axis, in pixels within a half: the summit of the parabola
    through the pixel and its two neighbours."""
    padded = np.pad(level, 1, mode="edge")
    step = (1, 0) if axis == 0 else (0, 1)
    before, here, after = (np.nan_to_num(padded[row + 1 + k * step[0], col + 1 + k * step[1]], nan=-np.inf) for k in (-1, 0, 1))
    with np.errstate(invalid="ignore", divide="ignore"):
        bend = before - 2 * here + after
        out = np.where(np.isfinite(bend) & (bend < 0), (before - after) / (2 * bend), 0.0)
    return np.clip(out, -0.49, 0.49)
