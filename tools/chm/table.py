"""Lookup table, blend, flags, expected error."""
from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

DATA = Path(__file__).resolve().parent / "data"
TABLES = {
    "v2": ("calibration.csv", "accuracy.csv", "meta_v2"),
    "v1": ("calibration-meta_v1.csv", "accuracy-meta_v1.csv", "meta_v1"),
}
BANDS = (0, 2, 5, 8, 12, 16, 20, 25, 35, 999)
# name, unit, draw style, description. Band 1 is always written. The rest need --debug.
HEIGHT_BANDS = (
    ("height_m", "m", "height", "calibrated canopy top"),
    ("height_matched_m", "m", "height", "Meta cell top through the rank-matched lookup"),
    ("expected_abs_error_m", "m", "error", "reference MAE for the reading used, in this height band"),
    ("meta_top_m", "m", "height", "max Meta 1 m pixel in the cell"),
    ("eth_m", "m", "height", "ETH, bilinear onto the 10 m grid"),
    ("eth_sd_m", "m", "error", "ETH standard deviation"),
    ("weight_on_meta", "", "share", "share of height_m taken from Meta, 0 to 1"),
)
BAND_NAMES = tuple(band[0] for band in HEIGHT_BANDS)
NONE, META_ALONE, ETH_ALONE, BOTH = 0, 1, 2, 3
META_ABOVE_TABLE, ETH_ABOVE_TABLE, APART = 1, 2, 4
APART_M = 10.0
ETH_YEAR = 2020
SOURCE_NAMES = {
    NONE: "no height",
    META_ALONE: "Meta alone (ETH holds no value)",
    ETH_ALONE: "ETH alone (Meta holds no value)",
    BOTH: "both maps",
}
FLAG_NAMES = {
    META_ABOVE_TABLE: "Meta above the last point of its lookup",
    ETH_ABOVE_TABLE: "ETH above the last point of its lookup",
    APART: f"the two calibrated readings {APART_M:.0f} m or more apart",
}
FINE_READINGS = {
    "crowns": "the 1 m file",
    "as_read": "Meta's pixel as read",
    "flat": "the mean of the 1 m file over the pixel's 10 m cell",
}
READINGS = {
    "meta_as_read": "Meta as read (`meta_top_m`)",
    "eth_as_read": "ETH as read (`eth_m`)",
    "meta_calibrated": "Meta through its lookup, alone",
    "eth_calibrated": "ETH through its lookup, alone",
    "blend": "`height_m`",
    "meta_matched": "`height_matched_m`",
}
FINE_TEXT = "Meta 1 m pixel times the cell's calibrated height / the cell top"
CELL_NOTE = "10 m cell value, repeated on each 1 m pixel."
SOURCE_BANDS = {
    "source": (NONE, "0 none, 1 Meta only, 2 ETH only, 3 both"),
    "weight_on_meta_pct": (255, "percent of the height from Meta; 255 = no height"),
    "year": (0, "year of the map with weight >= 0.5; 0 = unknown"),
    "meta_year": (0, "year of Meta's image; 0 = none"),
    "meta_month": (0, "month of Meta's image, 1-12; 0 = none"),
    "eth_year": (0, f"{ETH_YEAR} where ETH has a value, else 0"),
    "flags": (0, (
        f"bits: {META_ABOVE_TABLE} Meta above its table, {ETH_ABOVE_TABLE} ETH above its table, "
        f"{APART} the two calibrated values are {APART_M:.0f} m or more apart"
    )),
}


def load_table(version: str = "v2", region: str = "pooled", directory: Path | None = None) -> dict:
    """Lookups for one region (default `pooled`) and the accuracy rows for all sites."""
    if directory is None:
        directory = DATA
    curves_file, accuracy_file, meta = TABLES[version]
    curves: dict = {}
    with (directory / curves_file).open(newline="") as f:
        for row in csv.DictReader(f):
            if row["region"] == region:
                curves.setdefault((row["kind"], row["model"]), []).append((float(row["x_m"]), float(row["y"])))
    if not curves:
        raise SystemExit(f"{curves_file} holds no region named {region!r}")
    ref = {name: tuple(np.array(c) for c in zip(*curves[key], strict=True))
           for name, key in (("meta", ("calibrated", meta)), ("eth", ("calibrated", "eth")), ("matched", ("matched", meta)), ("weight", ("weight", meta)))}
    with (directory / accuracy_file).open(newline="") as f:
        rows = [row for row in csv.DictReader(f) if row["region"] == "all"]
    by = "unseen" if any(row["fit"] == "unseen" for row in rows) else "pooled"
    ref["accuracy"] = [row for row in rows if row["fit"] == by]
    return ref | {"region": region, "meta_name": meta, "files": [curves_file, accuracy_file], "accuracy_fit": by}


def lookup(x, xs, ys):
    """A lookup's value: linear between its points and flat below the first. Above the last, the last correction is
    kept and not the last height, so a canopy taller than any in the table reads a metre taller for each metre the
    map says."""
    return np.where(x > xs[-1], ys[-1] + (x - xs[-1]), np.interp(x, xs, ys))


def calibrate(meta_top, eth, ref: dict) -> dict:
    """The readings of a set of cells: each model through its lookup, the blend, the matched lookup and the weight
    used. Where one model is missing the other's calibrated value stands alone."""
    mc, ec = lookup(meta_top, *ref["meta"]), lookup(eth, *ref["eth"])
    w = np.interp((mc + ec) / 2, *ref["weight"])
    both = np.isfinite(mc) & np.isfinite(ec)
    return {"meta_calibrated": mc, "eth_calibrated": ec,
            "height": np.where(both, w * mc + (1 - w) * ec, np.where(np.isfinite(mc), mc, ec)),
            "height_matched": lookup(meta_top, *ref["matched"]),
            "weight": np.where(both, w, np.where(np.isfinite(mc), 1.0, np.where(np.isfinite(ec), 0.0, np.nan)))}


def most_recent(got: dict, date) -> dict:
    """The readings with one change: where both maps hold a value and Meta's image is dated to another year than
    ETH's, the height is the calibrated reading of the later image alone, and the weight says which. Where Meta's
    date is not known, or is of ETH's year, the blend stands."""
    year = date // 10000
    dated = np.isfinite(got["meta_calibrated"]) & np.isfinite(got["eth_calibrated"]) & (year > 0) & (year != ETH_YEAR)
    later = np.where(year > ETH_YEAR, got["meta_calibrated"], got["eth_calibrated"])
    return got | {"height": np.where(dated, later, got["height"]), "weight": np.where(dated, (year > ETH_YEAR).astype("float64"), got["weight"])}


def provenance(meta_top, eth, got: dict, date, ref: dict) -> dict:
    """Where each cell's height comes from, as the whole numbers of SOURCE_BANDS: the maps it is read from, the weight
    on Meta in per cent, the year of the map that carries half the height or more, the year and month of Meta's
    image, ETH's year, and the flags."""
    has_meta, has_eth = np.isfinite(meta_top), np.isfinite(eth)
    weight = np.where(np.isfinite(got["weight"]), got["weight"], -1.0)
    meta_year, eth_year = np.where(has_meta, date // 10000, 0), np.where(has_eth, ETH_YEAR, 0)
    flags = (META_ABOVE_TABLE * (meta_top > ref["meta"][0][-1]) + ETH_ABOVE_TABLE * (eth > ref["eth"][0][-1])
             + APART * (np.abs(got["meta_calibrated"] - got["eth_calibrated"]) >= APART_M))
    planes = (np.select([has_meta & has_eth, has_meta, has_eth], [BOTH, META_ALONE, ETH_ALONE], NONE),
              np.where(weight < 0, SOURCE_BANDS["weight_on_meta_pct"][0], np.rint(weight * 100)),
              np.where(weight >= 0.5, meta_year, eth_year), meta_year, np.where(has_meta, date // 100 % 100, 0), eth_year, flags)
    return {name: a.astype("uint16") for name, a in zip(SOURCE_BANDS, planes, strict=True)}


def told_of(source: dict, date) -> dict:
    """The provenance in numbers: of the cells with a height, the share by source and by flag; of the cells Meta is
    read on, the share by image date and the share with none."""
    held, meta = source["source"] > NONE, np.isin(source["source"], (META_ALONE, BOTH))
    n, n_meta = max(int(held.sum()), 1), max(int(meta.sum()), 1)
    dates, counts = np.unique(date[meta & (date > 0)], return_counts=True)
    return {"source_share": {name: round(float((source["source"] == code).sum() / n), 4)
                             for name, code in (("both", BOTH), ("meta_alone", META_ALONE), ("eth_alone", ETH_ALONE))},
            "flags_share": {name: round(float(((source["flags"] & bit) > 0).sum() / n), 4)
                            for name, bit in (("meta_above_table", META_ABOVE_TABLE), ("eth_above_table", ETH_ABOVE_TABLE), ("apart", APART))},
            "meta_image_dates": {f"{d // 10000}-{d // 100 % 100:02d}-{d % 100:02d}": round(float(c / n_meta), 4) for d, c in zip(dates.tolist(), counts.tolist(), strict=True)},
            "meta_cells_undated": round(float((meta & (date == 0)).sum() / n_meta), 4), "eth_year": ETH_YEAR}


def band_table(ref: dict, reading: str, frame: str) -> list[dict]:
    """The reference accuracy of one reading by band: [{band, lo, hi, n, mae, rmse, bias, median}]."""
    out = []
    for row in ref["accuracy"]:
        if row["reading"] == reading and row["frame"] == frame:
            lo, hi = (float(v) for v in row["band_m"].split("-"))
            out.append({"band": row["band_m"], "lo": lo, "hi": hi, "n": int(row["n"])} | {k: float(row[k]) for k in ("mae", "rmse", "bias", "median")})
    return sorted(out, key=lambda r: r["lo"])


def expected_error(height, ref: dict, weight=None) -> np.ndarray:
    """Per cell, the mean absolute error the reading used left in the reference places where it read a height in
    this cell's band: the blend's, or one map's calibrated reading where the weight puts the whole height on that
    map (no weight given: the blend's). A band the reference did not fill takes the nearest one that it did."""
    weight = np.full(np.shape(height), 0.5) if weight is None else weight
    out = np.full(np.shape(height), np.nan, "float32")
    for reading, used in (("blend", (weight > 0) & (weight < 1)), (f"{ref['meta_name']} calibrated", weight == 1), ("eth calibrated", weight == 0)):
        table = band_table(ref, reading, "by_value")
        edges, mae = np.array([r["lo"] for r in table]), np.array([r["mae"] for r in table])
        at = np.clip(np.searchsorted(edges, height, side="right") - 1, 0, len(edges) - 1)
        out = np.where(used & np.isfinite(height), mae[at], out).astype("float32")
    return out


def summary(values) -> dict:
    """Mean and five percentiles of the cells read."""
    v = values[np.isfinite(values)]
    if not v.size:
        return {"cells": 0}
    return {"cells": int(v.size), "mean": round(float(v.mean()), 2)} | {f"p{q}": round(float(np.percentile(v, q)), 2) for q in (5, 25, 50, 75, 95)}


def apply(meta_top, eth, table, date=None, recent=False):
    """Calibrated heights for arrays on one grid. `recent` keeps the later map where the years differ."""
    got = calibrate(meta_top, eth, table)
    if not recent:
        return got
    if date is None:
        raise ValueError("recent needs Meta image dates")
    return most_recent(got, date)

