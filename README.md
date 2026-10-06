# tools

Raster tools. One folder under `tools/`, one pixi task each. Run the tasks from this directory.

| task | command |
|---|---|
| canopy height | `pixi run chm --bbox W S E N --out DIR` |
| tree crowns | `pixi run crowns --chm canopy.tif --out DIR` |

## Canopy height

`chm` writes a calibrated canopy height at 10 m and at 1 m, from ETH's 2020 map and Meta's 1 m map. Method, accuracy, and limits: [docs/canopy-height.md](docs/canopy-height.md).

```
pixi run chm --bbox 6.22 43.30 6.28 43.345 --out out/
pixi run chm --geojson area.geojson --out out/ --debug
pixi run chm --bbox W S E N --out out/ --validate lidar_chm.tif
```

From Python, in this directory: `from tools.chm import run, apply, load_table`.

| file | contents |
|---|---|
| `chm_10m.tif` | `height_m`, metres, on a 10 m UTM grid. NaN where neither map has a value |
| `chm_1m_<tile>.tif` | same height on Meta's 1 m grid, one file per tile. Each pixel is Meta's pixel times the cell's `height_m` / the cell top |
| `chm_layers.qlr`, `*.qml` | QGIS styles. Drag the `.qlr` in. The 10 m `height_m` layer is on |
| `report.json`, `report.png` | what was read, source shares, image dates, reference error |
| `README.md` | files, warnings, and citations for this run |
| `validation.json` | with `--validate` |

`--debug` adds the diagnostic bands on both resolutions, plus `chm_10m_source.tif` and `chm_1m_<tile>_source.tif`. On the 1 m files those extra bands are the 10 m cell's value, repeated. `report.json` has the summaries either way.

`--meta v1` (default `v2`), `--debug`, `--recent`, `--validate PATH`, `--void-zero`, `--no-dates`, `--cache DIR`, `--eth-dir`, `--meta-dir`, `--region NAME`, `--no-plot`, `--max-km2` (2500).

The 1 m files are large. A run is capped at `--max-km2`. Image dates are the slow download; `--cache` keeps them, `--no-dates` skips them.

Tables live in `tools/chm/data/`.

## Tree crowns

`crowns` writes a top, an expected trunk, and a crown for each tree in a canopy-height raster or a LAS/LAZ cloud. Method, accuracy, and limits: [docs/tree-crowns.md](docs/tree-crowns.md).

```
pixi run crowns --chm canopy.tif --out out/
pixi run crowns --points a.laz b.laz --out out/
pixi run crowns --chm canopy.tif --out out/ --dem none --validate trees.gpkg
```

From Python, in this directory: `from tools.crowns import run, delineate, Rule`.

| file | contents |
|---|---|
| `tree_tops`, `tree_stems`, `tree_crowns` | GeoPackage and GeoJSON. Points at the top and the expected trunk, and the crown polygon |
| `crown_ids.tif` | tree number per pixel, 0 outside a crown |
| `canopy_height.tif` | the canopy the trees were read from |
| `trees.qlr`, `*.qml` | QGIS styles. Drag the `.qlr` in |
| `report.json`, `report.png` | counts and a window of the canopy |
| `README.md` | files and what this run found |
| `validation.json` | with `--validate` |

`--dem auto` uses the points' ground, or the mean of GEDTM30 and Copernicus DEM GLO-30. `--dem none` skips that. `--crowns watershed` (or `dalponte`, `silva`). A run holds the raster in memory, up to 250 million pixels.
