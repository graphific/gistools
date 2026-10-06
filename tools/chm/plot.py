"""One-page figure for a run."""
from __future__ import annotations

import math
from pathlib import Path
from itertools import pairwise

import numpy as np

from .table import APART, APART_M, BANDS, ETH_ABOVE_TABLE, ETH_YEAR, META_ABOVE_TABLE, NONE, band_table

CURVE_STEP, CURVE_SD = 0.25, 1.0


def height_curve(values, top: float) -> tuple[np.ndarray, np.ndarray]:
    """Where a set of heights lies, as a smooth line: (height, cells per metre of height) from 0 to `top`. The heights
    are counted in bins of CURVE_STEP and spread by a Gaussian of CURVE_SD, wide enough to hide that two of the maps
    hold whole metres. What the spreading would put below 0 or above `top` is folded back in, so the area under the
    line is the number of cells that are `top` or lower."""
    edges = np.arange(0.0, top + CURVE_STEP, CURVE_STEP)
    counts = np.histogram(np.clip(values[np.isfinite(values)], 0, None), bins=edges)[0].astype("float64")
    reach = round(4 * CURVE_SD / CURVE_STEP)
    kernel = np.exp(-0.5 * (np.arange(-reach, reach + 1) * CURVE_STEP / CURVE_SD) ** 2)
    spread = np.convolve(np.pad(counts, reach, mode="symmetric"), kernel / kernel.sum(), mode="valid")
    return edges[:-1] + CURVE_STEP / 2, spread / CURVE_STEP

def figure(path: Path, planes: dict, source: dict, ref: dict, report: dict, validation: dict | None) -> None:
    """The evidence on one page: the two maps as read and the calibrated one; the lookups with the area's own
    values, the heights before and after, and the error by height band (measured here with --validate, else the
    reference places'); and where each cell's height comes from, the date of Meta's image and the flagged cells."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap, to_rgb
    from matplotlib.patches import Patch

    ink, blue, red, green, orange, purple = "#222222", "#0072B2", "#D55E00", "#009E73", "#E69F00", "#CC79A7"
    fig, ax = plt.subplots(3, 3, figsize=(15.5, 14.4), constrained_layout=True)
    for a in (*ax[0], *ax[2]):
        a.set_xticks([])
        a.set_yticks([])
    reach = float(np.nanpercentile(np.r_[planes["height_m"].ravel(), planes["eth_m"].ravel()], 99.5)) if np.isfinite(planes["height_m"]).any() else 30.0
    recent = report["prefer"] == "recent"
    made = "calibrated, the later image alone" if recent else "calibrated blend"
    for a, key, title in ((ax[0, 0], "meta_top_m", f"Meta {report['reference']['meta']}, highest pixel per 10 m cell, as read"),
                          (ax[0, 1], "eth_m", f"ETH {ETH_YEAR}, as read"), (ax[0, 2], "height_m", made)):
        im = a.imshow(planes[key], vmin=0, vmax=reach, cmap="viridis", interpolation="nearest")
        a.set_title(title, fontsize=10, color=ink)
    fig.colorbar(im, ax=ax[0, :], shrink=0.75, label="canopy height (m)", pad=0.01)

    told = report["provenance"]
    a = ax[2, 0]
    im = a.imshow(planes["weight_on_meta"], vmin=0, vmax=1, cmap=LinearSegmentedColormap.from_list("share", [red, "#e8e8e8", blue]), interpolation="nearest")
    shares = told["source_share"]
    a.set_title(f"where the height comes from\nboth maps {shares['both']:.0%} of cells, Meta alone {shares['meta_alone']:.0%}, ETH alone {shares['eth_alone']:.0%}",
                fontsize=10, color=ink)
    fig.colorbar(im, ax=a, shrink=0.75, label="weight on Meta; the rest is ETH's", pad=0.01)
    a = ax[2, 1]
    when = np.where(source["meta_year"] > 0, source["meta_year"] + (source["meta_month"] - 0.5) / 12, np.nan)
    if told["meta_image_dates"]:
        first, last = math.floor(np.nanmin(when)), math.floor(np.nanmax(when)) + 1
        im = a.imshow(when, vmin=first, vmax=last, cmap=LinearSegmentedColormap.from_list("dates", plt.cm.Purples(np.linspace(0.25, 1, 256))), interpolation="nearest")
        fig.colorbar(im, ax=a, shrink=0.75, label="year", pad=0.01, ticks=range(first, last + 1), format="%d")
        a.set_title(f"date of Meta's image: {min(told['meta_image_dates'])} to {max(told['meta_image_dates'])}\nETH's images are {ETH_YEAR}", fontsize=10, color=ink)
    else:
        a.set_title(f"the dates of Meta's images were not read\nETH's images are {ETH_YEAR}", fontsize=10, color=ink)
    a = ax[2, 2]
    held = source["source"] > NONE
    shown, marks = np.ones((*held.shape, 3)), []
    shown[held] = to_rgb("#e4e4e4")
    for bit, colour, text in ((APART, orange, f"the two calibrated readings {APART_M:.0f} m or more apart"),
                              (META_ABOVE_TABLE | ETH_ABOVE_TABLE, purple, "a map above the last point of its lookup")):
        flagged = (source["flags"] & bit) > 0
        shown[flagged] = to_rgb(colour)
        marks.append(Patch(color=colour, label=f"{text}: {flagged.sum() / max(held.sum(), 1):.1%} of cells"))
    a.imshow(shown, interpolation="nearest")
    a.set_title("cells to read with care", fontsize=10, color=ink)
    a.legend(handles=marks, frameon=False, fontsize=8.5, loc="upper center", bbox_to_anchor=(0.5, -0.01))

    a = ax[1, 0]
    top = max(ref["meta"][0][-1], ref["eth"][0][-1])
    a.plot([0, top], [0, top], color="#9a9a9a", lw=1, ls=(0, (4, 3)))
    a.plot(*ref["meta"], color=blue, lw=2, marker="o", ms=4, label="Meta, calibrated")
    a.plot(*ref["matched"], color=green, lw=1.6, ls=(0, (4, 2)), marker="^", ms=4, label="Meta, rank-matched")
    a.plot(*ref["eth"], color=red, lw=2, marker="s", ms=4, label="ETH, calibrated")
    a.set_xlabel("what the model says (m)", fontsize=9)
    a.set_ylabel("height read for it (m)", fontsize=9)
    a.set_title(f"the lookups applied ({ref['region']} table); dashed: no change", fontsize=10, color=ink)
    a.legend(frameon=False, fontsize=8.5)
    a.grid(color="#e6e6e6", lw=0.6)

    a = ax[1, 1]
    drawn = (("meta_top_m", blue, "-", "Meta as read", "Meta"), ("eth_m", red, "-", "ETH as read", "ETH"),
             ("height_m", ink, "-", made, "calibrated"), ("height_matched_m", green, (0, (4, 2)), "Meta rank-matched", "rank-matched"))
    held = {key: planes[key][np.isfinite(planes[key])] for key, *_ in drawn}
    top = 2.0 * math.ceil(max([10.0] + [float(np.percentile(v, 99.5)) for v in held.values() if v.size]) / 2) + 2.0
    body, low = 0.0, []                              # the tallest any curve stands beyond the pile at 0 m; each reading's share in that pile
    for key, colour, style, text, short in drawn:
        v = held[key]
        x, y = height_curve(v, top)
        body = max(body, float(y[x >= 3 * CURVE_SD].max()))
        low.append(f"{short} {(v < BANDS[1]).mean():.0%}" if v.size else f"{short} none")
        a.plot(x, y, color=colour, lw=1.8, ls=style, label=f"{text}: median {np.median(v):.1f} m" if v.size else text)
    a.set_xlabel("canopy height (m)", fontsize=9)
    a.set_ylabel("10 m cells per metre of height", fontsize=9)
    if body > 0 and a.get_ylim()[1] > 2 * body:      # a pile of bare ground would flatten every curve: it runs off the top, and its shares are written
        a.set_ylim(0, 1.75 * body)
        a.set_title("the area's heights before and after", fontsize=10, color=ink, pad=17)
        a.text(0, 1.012, f"cells under {BANDS[1]} m, off the scale: " + ", ".join(low), transform=a.transAxes, fontsize=7.5, color="#555555", va="bottom")
    else:
        a.set_ylim(0, a.get_ylim()[1] * 1.45)        # room for the legend above the tallest curve
        a.set_title("the area's heights before and after", fontsize=10, color=ink)
    a.legend(frameon=False, fontsize=8.5, loc="upper right")
    a.grid(color="#e6e6e6", lw=0.6)

    a = ax[1, 2]
    a.axhline(0, color="#7a7a7a", lw=1)
    centre = {f"{lo}-{hi}": (lo + min(hi, lo + 10)) / 2 for lo, hi in pairwise(BANDS)}
    if validation:
        for key, colour, mark, style, text in (("eth_as_read", red, "s", "-", "ETH as read"), ("meta_as_read", blue, "o", "-", "Meta as read"),
                                               ("blend", ink, "D", "-", made), ("meta_matched", green, "^", (0, (4, 2)), "Meta rank-matched")):
            v = validation["readings"][key]
            pts = [(centre[b], s["median"]) for b, s in v.get("by_measured", {}).items()]
            if pts:
                a.plot(*zip(*pts, strict=True), color=colour, lw=2, ls=style, marker=mark, ms=5, label=f"{text}: RMSE {v['rmse']:.1f} m, MAE {v['mae']:.1f} m")
        a.set_title("measured here: median error by LiDAR height band", fontsize=10, color=ink)
    else:
        alone = ((f"{ref['meta_name']} calibrated", blue, "o", (0, (1, 1)), "Meta calibrated, alone"), ("eth calibrated", red, "s", (0, (1, 1)), "ETH calibrated, alone"))
        for reading, colour, mark, style, text in ((f"{ref['meta_name']} raw", blue, "o", "-", "Meta as read"), ("eth raw", red, "s", "-", "ETH as read"),
                                                   *(alone if recent else (("blend", ink, "D", "-", "calibrated blend"),)),
                                                   (f"{ref['meta_name']} matched", green, "^", (0, (4, 2)), "Meta rank-matched")):
            table = band_table(ref, reading, "by_lidar")
            overall = next(r for r in ref["accuracy"] if r["reading"] == reading and r["frame"] == "all")
            a.plot([centre[r["band"]] for r in table], [r["median"] for r in table], color=colour, lw=2, ls=style, marker=mark, ms=5,
                   label=f"{text}: RMSE {float(overall['rmse']):.1f} m, MAE {float(overall['mae']):.1f} m")
        a.set_title("in the reference places (not this area): median error by LiDAR height band", fontsize=10, color=ink)
    a.set_xlabel("measured canopy height (m)", fontsize=9)
    a.set_ylabel("median of model minus measured (m)", fontsize=9)
    low, high = a.get_ylim()
    a.set_ylim(low, high + 0.42 * (high - low))      # and for this one above the highest line
    a.legend(frameon=False, fontsize=8.5, loc="upper right")
    a.grid(color="#e6e6e6", lw=0.6)
    fig.suptitle(f"Calibrated canopy height, {report['area_km2']:.1f} km2 at {report['bbox']}; expected mean absolute error "
                 f"{report['expected']['mae_m']:.1f} m by the reference places", fontsize=11, color=ink, x=0.01, ha="left")
    fig.savefig(path, dpi=110, facecolor="white")
    plt.close(fig)

