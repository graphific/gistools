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

## Building-aware heights and crowns

**Buildings and dates.** `--buildings /absolute/path/buildings.gpkg --date 2020 --building-context context.json`
reads local Overture, OSM or national polygons (multiple files or GeoJSON also work), for supplied files. `pyogrio`
and `shapely` are pinned dependencies. Default `--building-mode canopy` excludes only roof-compatible heights
with compatible temporal evidence or a declared recent-presence assumption; heights above the roof remain possible overhang, not confirmed vegetation.
Unknown building height or incompatible dates leave CHM heights unresolved. `--building-mode strict` is the
explicit blanket-mask alternative, which removes genuine overhang too. Excluded pixels are nodata, never zero.
Fine files use their own grid, not an expanded 10 m mask. Outside pixels retain the original 10 m calibration;
masking does not repair roof contamination in that calibration or recover missing crown geometry.

The requested `--date` accepts a year or ISO date and never relabels the imagery. Meta/ETH acquisition years are
read separately and preserved in raster tags. Checks are **year-level**: lifecycle boundary years remain unresolved.
Default `--building-time-policy recent` assumes a mapped building exists within `recent_years` (default 2) of
its snapshot year. An explicit target date governs this assumption even with older imagery: this screens an
old CHM for current buildings, without claiming current canopy measurements. Construction/demolition evidence
overrides the assumption. `--building-time-policy evidence` requires lifecycle support for the target and all
known acquisition years; incomplete acquisition metadata prevents exclusion under that policy.

Example context (replace the values and fields with the supplied file's actual evidence):

```json
{"snapshot_year": 2020, "height_field": "height", "min_height_field": "min_height",
 "construction_year_field": "construction_year", "demolition_year_field": "demolition_year",
 "clearance_m": 2, "max_stem_shift_m": 2, "recent_years": 2}
```

Field mappings are optional; default names are those above. An explicitly requested missing field fails.
Heights must be metres above local ground, not absolute roof elevations, floor counts or roof-section height.
`min_height` permits vegetation under an elevated structure. The clearance is a declared screening tolerance,
not a validated classifier threshold. A supplied `snapshot_year` takes precedence; otherwise the tool reads a
standard `__YYYY-MM-DD__` or `__asof-YYYY-MM-DD__` filename token as a **publication proxy**, not a construction
date. No filesystem clock is used. Undated files require a context for the recent assumption. OSM `start_date`
and `end_date` are read from columns or retained `other_tags`; approximate dates remain unresolved.
Reports record source/context hashes, temporal decisions, coverage limitations and masked-cell counts.

An estimated stem inside a contemporaneous solid building requires support from the **same crown** outside
the footprint. A nearby outside estimate is allowed only within `max_stem_shift_m` (default 2 m), is explicitly
labelled, and retains its original coordinates and displacement. A deep roof-centre candidate without that
support is withheld from the accepted tree layers and recorded in `report.json`, including its coordinates and
reason. An unknown-date footprint at the stem also yields an unresolved candidate, not an automatic relocation.
An elevated structure with positive `min_height` is not treated as solid ground occupancy. This is a constrained
estimate, never a measured trunk. Canopy overhang remains in crowns; broad crowns are not clipped at building walls.

Vector attributes add `building_status`, `building_height_m`, `stem_shift_m` and `unconstrained_stem_x/y`.
Status codes: 0 no contemporaneous mapped overlap, 1 overlap retained, 2 overlap time unresolved, 3 constrained
stem estimate. No code proves vegetation or independent accuracy. `building_height_m` is the maximum supplied
height of overlapping accepted buildings, not a measured roof surface. Adjusted stems recompute top-to-stem
distance and terrain-relative height; their old slope is cleared rather than reused at a different point.
`--building-mode strict` retains
the blanket exclusion option: nodata in touched pixels and clipped crowns, including real overhang. Neither mode
lets hole filling restore excluded pixels. Missing files/CRS or invalid selected polygons fail.

The Python `chm.run` and `crowns.run` accept `buildings`, `date`, `building_context`,
`building_mode` and `building_time_policy`. `pixi run -e test test` runs the controlled
roof/tree, dates, geometry, overhang and output checks. Set TMPDIR and pytest basetemp
to NAS scratch on BioTerra. These controls establish code behavior, not urban accuracy.

## Automatic buildings and a two-command workflow

```bash
pixi run chm --geojson /mnt/p/amsterdam_bos.geojson --out /mnt/p/BIOTERRA/work/results/amsterdam-bos/chm --buildings auto --date today --debug
pixi run crowns --chm /mnt/p/BIOTERRA/work/results/amsterdam-bos/chm --out /mnt/p/BIOTERRA/work/results/amsterdam-bos/crowns
```

`--buildings auto` fetches only the AOI's Overture subset, pinning its release, bounds
and checksum in the output's `inputs/`. A rerun reuses the receipt and refuses changed
files or another AOI. DuckDB extensions and query spill also stay there. `today` is the
local calendar date, recorded as ISO; source imagery keeps its actual acquisition dates.

A CHM folder input makes crowns combine all its fine tiles before detection, inheriting
the verified footprint files, context and date/settings unless explicitly overridden.
The intermediate mosaic is disk-backed under the crown output's `inputs/`; its source
years describe all tiles. The 250-million-pixel crown reader limit still applies.
Keep the CHM and crown output directories separate. A single raster remains supported.

CHM screening removes roof-compatible heights; crown constraints separately test stem
locations. Both stages use footprints because canopy above roofs may remain while an
estimated stem inside a solid building is implausible. This does not prove vegetation.
