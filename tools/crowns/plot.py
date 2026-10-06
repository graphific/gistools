"""The one-page figure written beside a run."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from .canopy import Grid

CURVE_SD = 0.5


def figure(path: Path, chm, numbers, table: dict, polygons, grid: Grid, report: dict, validation: dict | None) -> None:
    """The run on one page: a window of the canopy with its tops, trunks and crowns, the trees a hectare over the
    whole area, the heights, and crown diameter against height."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection

    ink, line, wood = "#222222", "#0072B2", "#D55E00"
    found = report["found"]
    fig, ax = plt.subplots(2, 2, figsize=(13.5, 12.2), constrained_layout=True)
    x, y, tall, wide = table["top_x"], table["top_y"], table["height_m"], table["crown_diameter_m"]
    reach, tallest = report["drawn_to_m"], 5.0 * math.ceil(float(tall.max()) / 5)

    cell = max(20.0, grid.pixel * math.ceil(max(grid.rows, grid.cols) / 400))      # the whole area, in cells of 20 m or more
    count, _, _ = np.histogram2d(grid.north - y, x - grid.west, bins=(max(1, math.ceil(grid.rows * grid.pixel / cell)), max(1, math.ceil(grid.cols * grid.pixel / cell))),
                                 range=((0, math.ceil(grid.rows * grid.pixel / cell) * cell), (0, math.ceil(grid.cols * grid.pixel / cell) * cell)))
    side = min(150.0, grid.rows * grid.pixel, grid.cols * grid.pixel)             # the window: around the fullest cell
    r, c = np.unravel_index(np.argmax(count), count.shape)
    mid_x = min(max(grid.west + (c + 0.5) * cell, grid.west + side / 2), grid.west + grid.cols * grid.pixel - side / 2)
    mid_y = max(min(grid.north - (r + 0.5) * cell, grid.north - side / 2), grid.north - grid.rows * grid.pixel + side / 2)
    r0, c0 = max(int((grid.north - mid_y - side / 2) / grid.pixel), 0), max(int((mid_x - side / 2 - grid.west) / grid.pixel), 0)
    r1, c1 = min(r0 + math.ceil(side / grid.pixel), grid.rows), min(c0 + math.ceil(side / grid.pixel), grid.cols)
    box = (grid.west + c0 * grid.pixel, grid.west + c1 * grid.pixel, grid.north - r1 * grid.pixel, grid.north - r0 * grid.pixel)

    a = ax[0, 0]
    im = a.imshow(chm[r0:r1, c0:c1], extent=box, vmin=0, vmax=reach, cmap="viridis", interpolation="nearest")
    inside = (x >= box[0]) & (x <= box[1]) & (y >= box[2]) & (y <= box[3])
    a.add_collection(LineCollection([np.asarray(g.exterior.coords) for g in polygons[inside]], colors="white", linewidths=0.8))
    a.add_collection(LineCollection(np.stack([np.column_stack([x, y]), np.column_stack([table["stem_x"], table["stem_y"]])], axis=1)[inside], colors=wood, linewidths=0.9))
    a.plot(x[inside], y[inside], ".", color="white", ms=4.5, mec=ink, mew=0.5, label="top")
    a.plot(table["stem_x"][inside], table["stem_y"][inside], ".", color=wood, ms=4.5, mec="white", mew=0.4, label="trunk, expected")
    a.legend(loc="upper right", fontsize=8, framealpha=0.85, borderpad=0.4, handletextpad=0.3)
    a.set_xlim(box[0], box[1])
    a.set_ylim(box[2], box[3])
    a.set_title(f"a window of {side:.0f} m: the canopy, {int(inside.sum()):,} tops, their trunks and crowns", fontsize=10, color=ink)
    fig.colorbar(im, ax=a, shrink=0.75, label="canopy height (m)", pad=0.01)

    a = ax[0, 1]
    per_ha = count / (cell ** 2 / 1e4)
    im = a.imshow(np.where(count > 0, per_ha, np.nan), extent=(grid.west, grid.west + count.shape[1] * cell, grid.north - count.shape[0] * cell, grid.north),
                  cmap="magma_r", interpolation="nearest", vmin=0)
    a.plot([box[0], box[1], box[1], box[0], box[0]], [box[2], box[2], box[3], box[3], box[2]], color=line, lw=1.2)
    a.set_title(f"the whole area: {found['trees']:,} trees, {found['trees_per_ha']:,.0f} a hectare; the window outlined", fontsize=10, color=ink)
    fig.colorbar(im, ax=a, shrink=0.75, label=f"trees a hectare, in cells of {cell:g} m", pad=0.01)
    for a in ax[0]:
        a.set_aspect("equal")
        a.tick_params(labelsize=7)
        a.ticklabel_format(useOffset=False, style="plain")

    a = ax[1, 0]
    if len(tall):
        step = 0.25
        edges = np.arange(0.0, tallest + step, step)
        spread = round(4 * CURVE_SD / step)
        kernel = np.exp(-0.5 * (np.arange(-spread, spread + 1) * step / CURVE_SD) ** 2)
        curve = np.convolve(np.pad(np.histogram(tall, bins=edges)[0].astype("float64"), spread, mode="symmetric"), kernel / kernel.sum(), mode="valid") / step
        curve[edges[:-1] + step < report["settings"]["min_height"]] = np.nan      # no tree is looked for there: the smoothing alone would draw some
        a.plot(edges[:-1] + step / 2, curve, color=line, lw=2)
        a.fill_between(edges[:-1] + step / 2, curve, color=line, alpha=0.12, lw=0)
        middle = float(np.median(tall))
        a.axvline(middle, color=ink, lw=0.8, ls=(0, (4, 3)))
        a.annotate(f"middle tree {middle:.1f} m", (middle, a.get_ylim()[1] * 0.98), xytext=(5, 0), textcoords="offset points", fontsize=9, color=ink, va="top")
    a.set_xlim(0, tallest)
    a.set_ylim(bottom=0)
    a.set_xlabel("tree height (m)")
    a.set_ylabel("trees per metre of height")
    a.set_title("how tall the trees found are", fontsize=10, color=ink)

    a = ax[1, 1]
    if len(tall):
        widest = float(np.percentile(wide, 99.5)) * 1.05
        if len(tall) > 5000:
            fig.colorbar(a.hist2d(tall, wide, bins=(60, 60), range=((0, tallest), (0, widest)), cmap="Blues", cmin=1)[3], ax=a, shrink=0.75, label="trees", pad=0.01)
        else:
            a.plot(tall, wide, ".", color=line, ms=5, alpha=0.45, mew=0)
        bands = np.linspace(0, tallest, 13)
        at = np.digitize(tall, bands) - 1
        mid = [(0.5 * (bands[k] + bands[k + 1]), float(np.median(wide[at == k]))) for k in range(12) if (at == k).sum() >= 10]
        if mid:
            a.plot(*zip(*mid, strict=True), color=ink, lw=1.6, marker="o", ms=3.5)
            a.text(0.02, 0.97, "line: the middle crown of each band of height", transform=a.transAxes, fontsize=9, color=ink, va="top")
        a.set_ylim(0, widest)
    a.set_xlim(0, tallest)
    a.set_xlabel("tree height (m)")
    a.set_ylabel("crown diameter (m)")
    a.set_title("crown diameter against height", fontsize=10, color=ink)
    for a in ax[1]:
        a.spines[["top", "right"]].set_visible(False)
        a.grid(color="#dddddd", lw=0.6)
        a.set_axisbelow(True)
    told = (f"   against {Path(validation['reference']).name}: recall {validation['recall']:.2f}, precision {validation['precision']:.2f}, F1 {validation['f1']:.2f} "
            f"({validation['read_as']})" if validation else "")
    fig.suptitle(f"crowns: {report['input']['what']}{told}", fontsize=11, color=ink)
    fig.savefig(path, dpi=110)
    plt.close(fig)
