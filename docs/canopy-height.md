# Calibrated canopy height from ETH and Meta

A 10 m canopy-top map, and a 1 m map that uses the same heights with Meta's crown shapes. Both come from two public global maps and a lookup table fit to airborne LiDAR. No account, no local LiDAR required to run it.

The numbers below are leave-one-site-out on the reference set (`fit=unseen` in `tools/chm/data/accuracy.csv`). They are not an error bar for a new area. Per-site rows are in the csv.

## Inputs

| map | grid | year | file |
|---|---|---|---|
| ETH Global Canopy Height, and its standard deviation | 10 m | 2020 | `ETH_GlobalCanopyHeight_10m_2020_<N42E006>_Map.tif` and `_Map_SD.tif`, 3-degree tiles named by the south-west corner. `https://libdrive.ethz.ch/index.php/s/cO8or7iOe5dT2Rt/download?path=%2F3deg_cogs&files=` |
| Meta / WRI canopy height v2 (default) | 1 m | image date per tile | `https://dataforgood-fb-data.s3.amazonaws.com/forests/v2/global/dinov3_global_chm_v2_ml3/chm/<quadkey>.tif`, zoom-10 quadkeys. Dates in `.../metadata/<quadkey>.geojson` |
| Meta / WRI canopy height v1 (`--meta v1`) | 1 m | image date per tile, 2009-2020 | `https://dataforgood-fb-data.s3.amazonaws.com/forests/v1/alsgedi_global_v6_float/chm/<quadkey>.tif`, zoom-9 quadkeys |

ETH: Lang, Jetz, Schindler, Wegner 2023, doi:10.3929/ethz-b-000609802. CC BY 4.0. Earth Engine: `users/nlang/ETH_GlobalCanopyHeight_2020_10m_v1` and `users/nlang/ETH_GlobalCanopyHeightSD_2020_10m_v1`.

Meta v1: Tolan et al. 2024. CC BY 4.0. Earth Engine: `projects/sat-io/open-datasets/facebook/meta-canopy-height`. No Earth Engine asset is known for v2.

Tiles are read in windows over HTTP. `--eth-dir` and `--meta-dir` point at a local copy or a mirror. Meta's date files are tens of megabytes per tile. `--cache DIR` stores them. `--no-dates` leaves the years at 0.

## 10 m grid

UTM zone of the box centre. Cell size 10 m. Edges fall on multiples of 10 m. `--geojson` masks the output to the polygons.

For each cell:

- `M` is the maximum Meta pixel whose centre falls in the cell. A cell with fewer than half the pixels its area should hold is left empty.
- `E` is ETH, bilinear.
- ETH's standard deviation is stored with `--debug`. It is not an input to the height.

`M` is a canopy top, the same statistic as a LiDAR maximum. It is not a mean height.

## Lookup

Knots are in `tools/chm/data/calibration.csv` (v1: `calibration-meta_v1.csv`). Default region is `pooled`. `--region NAME` uses that place's own rows.

Between knots the lookup is linear. Below the first knot it is flat. Above the last knot the last offset is kept:

```
lookup(x) = y_n + (x - x_n)    if x > x_n
```

A metre of map height above the table stays a metre of output. The knots stop near 34 m (Meta) and 33 m (ETH).

A `calibrated` row is the median LiDAR canopy top where the map reported that height. A `matched` row is a rank match, which leaves tall stands taller and costs a bit of per-cell error. A `weight` row is the Meta weight with the lowest absolute error in that height band.

## Blend

```
Mc = lookup_meta(M)
Ec = lookup_eth(E)
w  = lookup_weight((Mc + Ec) / 2)
H  = w * Mc + (1 - w) * Ec
```

Where one map is missing, `H` is the other calibrated value and `w` is 0 or 1. The weight is flat past both ends of its table.

On the pooled v2 table, Meta reads low (a cell it calls 1 m is calibrated to about 4.5 m) and ETH reads high through the middle heights (near 0 until ETH itself says about 9 m, then it climbs). The blend puts most of the weight on Meta, about 0.65 to 0.90 depending on height.

`height_matched_m` is Meta through the rank-matched lookup. It is a debug band.

## 1 m map

Always written, one GeoTIFF per Meta tile, on that tile's grid (Web Mercator), windowed to the area, with overviews.

```
p' = p * H / M     if M > 0
p' = p             if M = 0, or the cell has no top
```

`p` is Meta's pixel. The crown shapes stay Meta's. The level is the calibrated 10 m top. Where `M` is 0 there is no crown to scale, so a tall `H` does not appear in the 1 m file. The run warns when that happens on cells of 2 m or more.

This is not a finer measurement. The lookups and the accuracy figures are for the 10 m top. Scoring the 1 m file against LiDAR (`--validate`) reads the LiDAR as the mean inside each 1 m pixel. Flattening every crown to its cell mean is usually as close, which is the check that the 1 m detail is shape, not new height information.

## Debug bands

`--debug` writes the same extra bands on the 10 m map and on each 1 m tile.

On the 1 m grid, `height_m` is `p'`. Every other band is the 10 m cell's value, copied onto each pixel in that cell. Those bands are constant inside a cell. They are there so a crown can be read next to the inputs that set its height.

`chm_10m.tif` and `chm_1m_<tile>.tif`:

| band | unit | meaning |
|---|---|---|
| `height_m` | m | `H` at 10 m; `p'` at 1 m |
| `height_matched_m` | m | rank-matched Meta |
| `expected_abs_error_m` | m | reference MAE of the reading used, in the band this height falls in |
| `meta_top_m` | m | `M` |
| `eth_m` | m | `E` |
| `eth_sd_m` | m | ETH standard deviation |
| `weight_on_meta` |  | `w` |

`chm_10m_source.tif` and `chm_1m_<tile>_source.tif`, uint16. The 1 m file repeats the cell value. Empty codes: `source` 0, `weight_on_meta_pct` 255, years and flags 0.

| band | values |
|---|---|
| `source` | 0 none, 1 Meta only, 2 ETH only, 3 both |
| `weight_on_meta_pct` | `round(100 * w)`; 255 if no height |
| `year` | year of the map with `w >= 0.5` (Meta's image, or 2020); 0 if unknown |
| `meta_year`, `meta_month` | Meta image date; 0 if Meta is missing or publishes no date |
| `eth_year` | 2020 where ETH has a value, else 0 |
| `flags` | bits, added: 1 Meta above its last knot, 2 ETH above its last knot, 4 `abs(Mc - Ec) >= 10` m |

`year` is the dominant map, not a single acquisition date. The two years are the other bands.

Without `--debug` the maps contain `height_m` only. `report.json` still has the source shares, dates, and reference error.

## One date per cell

`--recent` applies where both maps have a value and Meta's image year is not 2020. `H` becomes the calibrated value of the later image, and `w` becomes 0 or 1. Unknown Meta dates keep the blend.

Meta's images in the boxes read so far are mostly 2012 to early 2020, so the option usually selects ETH. On the reference set that costs accuracy: ETH through its lookup is 4.24 m MAE, Meta through its lookup 3.13 m, the blend 2.95 m. Where the calibrated values are 10 m or more apart, ETH alone is 10.7 m off, Meta alone 5.7 m, the blend 4.9 m. The option is for an area where a change after Meta's image is already known. It does not date the change.

## Expected error

`expected_abs_error_m` is the reference MAE in the height band the cell's reading falls in. A cell whose weight is entirely on one map uses that map's calibrated error. A band the reference did not fill takes the nearest band that it did. The band edges are 0, 2, 5, 8, 12, 16, 20, 25, 35 m.

| `H` (m) | 0-2 | 2-5 | 5-8 | 8-12 | 12-16 | 16-20 | 20-25 | 25-35 | 35+ |
|---|---|---|---|---|---|---|---|---|---|
| expected absolute error (m) | 1.3 | 3.5 | 3.2 | 3.4 | 3.6 | 4.2 | 4.4 | 5.2 | 7.5 |

## Reference set

Airborne LiDAR, 40 sites, Europe and North America, 10.6 million 10 m cells. Windows of 0.92 km2, non-overlapping, at most 40 per site. The reference value of a cell is its highest LiDAR pixel. Cells were selected on the reference before either map was read:

- flight year 2015 or later, from the product's own per-pixel year (ETH's images are 2020; Meta's, where dated, mostly 2016-2019)
- not built-up and not water in ESA WorldCover 2021
- no cell that contains a pixel the product flags as a spike
- two flights dropped because their LiDAR reads about half of what both maps say, where the other flights read 1.0 to 1.3 times Meta: Neusiedler See (2015-19) and Monongahela 2016

The v2 accuracy is leave-one-site-out: each site is scored with a table fit without it. The v1 table (`--meta v1`) is four sites only (Var, Zamora-Ourense, central Portugal, Vancouver Island), and its accuracy is held-out windows of one table.

Eight further sites, flown before 2015 or with no recorded year, are in `accuracy.csv` under the shipped table and were not used to fit it.

## Accuracy

All 40 sites, each under the table fit without it:

| reading | MAE (m) | RMSE (m) | bias (m) |
|---|---|---|---|
| Meta v2 cell top, as read | 3.71 | 5.92 | -2.63 |
| ETH, as read | 5.52 | 7.44 | +3.60 |
| Meta v2 through its lookup | 3.13 | 5.33 | -0.59 |
| ETH through its lookup | 4.24 | 6.38 | -0.44 |
| blend | 2.95 | 4.92 | -0.61 |
| Meta v2, rank-matched | 3.23 | 5.42 | +0.01 |

Mean MAE across sites: Meta as read 3.61 m, ETH as read 5.37 m, blend 2.89 m, a blend fit in the site itself 2.57 m. The shipped blend is closer than Meta as read in 34 of 40 sites, than ETH as read in 39, than both in 33.

Median error (m) by LiDAR height, and the blend's MAE:

| LiDAR height (m) | 0-2 | 2-5 | 5-8 | 8-12 | 12-16 | 16-20 | 20-25 | 25-35 | 35+ |
|---|---|---|---|---|---|---|---|---|---|
| Meta v2 as read | -0.1 | -2.4 | -3.0 | -3.3 | -3.3 | -3.4 | -4.1 | -6.9 | -12.0 |
| ETH as read | +4.7 | +6.7 | +5.2 | +3.7 | +2.4 | +2.3 | +2.4 | -0.8 | -6.5 |
| blend | +0.2 | -1.3 | +0.3 | -0.0 | -0.2 | -0.3 | -0.8 | -3.9 | -9.8 |
| Meta v2, rank-matched | +0.7 | -1.5 | +0.3 | -0.1 | -0.2 | -0.1 | -0.1 | -1.9 | -5.6 |
| blend MAE | 1.2 | 3.5 | 3.8 | 3.7 | 3.5 | 3.4 | 3.1 | 4.7 | 11.3 |

Checked again by running this script on four boxes of 17-27 km2 with `--validate`, mostly outside the windows used to fit the table. MAE (m), Meta as read / ETH as read / blend: France 3.00 / 4.28 / 2.34; Portugal 4.05 / 6.76 / 3.37; Spain 4.31 / 5.85 / 3.04; Canada 11.82 / 9.27 / 9.63.

The same four boxes at 1 m, MAE of the scaled file / Meta's pixels as read / the file with each crown flattened to its cell mean: France 2.65 / 2.46 / 2.63; Portugal 3.70 / 3.58 / 3.55; Spain 2.44 / 2.47 / 2.49; Canada 10.63 / 11.15 / 10.23. Use `height_m` for a cell top. For a mean or a sum over an area, compare Meta's pixels as read before using the 1 m file.

## Limits

- The fit has no tropical site, no southern-hemisphere site, and nothing in Asia or Africa. There the lookups are an extrapolation, and `expected_abs_error_m` is the reference error.
- Canopy above about 35 m is read low (median about -10 m in the 35 m+ band). The two rainforests in the set, Vancouver Island and Hoh, are off by 7.5 m and 9.6 m. Flags 1 and 2 mark cells above the last knot.
- The median lookup pulls tall stands down. At 25-35 m the blend's median error is -3.9 m; the rank match is -1.9 m, at 0.3 m more MAE overall. On that band ETH as read is closer than the blend (4.0 m MAE against 4.7 m).
- Meta can read zero under cloud. A cell with Meta at 0 m and ETH at 30 m comes out near 7 m, with flag 4. In the tropics, read flagged cells from `meta_top_m` and `eth_m` (`--debug`).
- Meta's images in the four checked boxes sit 2 to 13 m off the LiDAR crowns, more under the tallest trees. This tool does not shift them. With no local LiDAR there is nothing to register against.
- ETH is 2020. Meta is dated per image. The LiDAR flights are 2015-2025. Growth and felling sit in the error. In the Harz the same cells read 17.6 m in a 2016-18 flight and 2.1 m in a 2024-25 flight.
- Two LiDAR products of one place also differ. In eight French regions the 1 m canopy height and the 2.5 m surface-minus-terrain of the same campaigns are 0.4 to 1.6 m apart (MAE). That is the floor under these numbers.
- Built-up land and water were left out of the fit. The tool still writes a height there.

## What was tried and dropped

- ETH standard deviation as an input: 0.00-0.06 m of MAE. The blend's error does rise with it, so the band is kept.
- Separate lookups by WorldCover class: 0.02 m of MAE across sites.
- Picking a nearby site's table instead of the pooled one. A site's own fit is 2.57 m against 2.89 m unseen, so local LiDAR is worth about 0.3 m. Nothing in the set says which rows an area without LiDAR should take. The per-site rows are still in the csv for `--region`.
- GEDI RH98 as a local reference: a table fit on the footprints recovered 40-50% of the gain in three regions and none in a fourth.

## Files the tool writes

`chm_10m.tif` is a cloud-optimised GeoTIFF with overviews. The 1 m files are tiled GeoTIFFs with overviews on the tile grid. Band name, unit, description, and statistics are in the file.

`chm_layers.qlr` opens each band as a QGIS layer. Heights share one scale, from 0 to the area's tall readings. `report.json` lists the tiles read, the table, height summaries, source shares, image dates, the reference accuracy by band, and warnings.

A second run into the same folder replaces the previous one. Rasters are renamed aside first, so a file QGIS has open does not block the write, and they are put back if the run stops.

## Cite

Lang, N., Jetz, W., Schindler, K., Wegner, J.D. (2023). A high-resolution canopy height model of the Earth. doi:10.3929/ethz-b-000609802. CC BY 4.0.

Tolan, J. et al. (2024). Very high resolution canopy height maps from RGB imagery using self-supervised vision transformer and convolutional decoder trained on aerial lidar. Remote Sensing of Environment. Meta / World Resources Institute canopy height, CC BY 4.0.

Calibration tables: `tools/chm/data/calibration.csv` and `tools/chm/data/accuracy.csv`, shipped with this code.
