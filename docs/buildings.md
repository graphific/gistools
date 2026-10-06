# Buildings

`--buildings` keeps roofs out of the canopy height and keeps an estimated trunk off a solid building. It does not detect buildings, and it does not prove that a remaining height is a tree.

```
pixi run chm --bbox W S E N --out out/ --buildings auto --date 2020
pixi run crowns --chm out/ --out crowns/
pixi run crowns --chm canopy.tif --out crowns/ --buildings buildings.gpkg --date 2020
```

From Python, `chm.run` and `crowns.run` take `buildings`, `date`, `building_context`, `building_mode`, and `building_time_policy`.

## Modes

| mode | chm | crowns |
|---|---|---|
| `canopy` (default) | pixels at or below the mapped roof height, plus `clearance_m` (default 2), become nodata. Taller pixels stay | a stem inside a solid building moves to the same crown outside the footprint, at most `max_stem_shift_m` (default 2). Otherwise that tree is left out of the layers and listed in `report.json` |
| `strict` | every pixel the footprint touches becomes nodata | crowns are clipped to the footprint |

Excluded pixels are nodata, not zero. Filling holes does not put them back. `min_height` above 0 is an elevated structure: heights below it are left alone, and the stem is not treated as indoor.

## Date

`--date` is a year, `YYYY-MM-DD`, or `today`. `today` is the local calendar date. Image dates are read from the raster and are not rewritten.

Comparison is by year. A construction or demolition year that falls inside the range stays unresolved. `--building-time-policy recent` (default) treats a building as present when the requested year is within `recent_years` (default 2) of the file's snapshot year. `--building-time-policy evidence` requires construction and demolition years that cover the target and every known image year. Missing image dates then block exclusion.

The snapshot year comes from the context file, or from `__YYYY-MM-DD__` or `__asof-YYYY-MM-DD__` in the filename. That token is the publication date. OSM `start_date` and `end_date` are read from columns or `other_tags`. A value such as `~1990` stays unresolved.

## Context

```json
{"snapshot_year": 2020, "height_field": "height", "min_height_field": "min_height",
 "construction_year_field": "construction_year", "demolition_year_field": "demolition_year",
 "clearance_m": 2, "max_stem_shift_m": 2, "recent_years": 2}
```

Field names are optional and default to the keys above. Naming a field that is not in the file is an error. Heights are metres above local ground. A trailing `m` or `ft` is accepted.

## Auto and a chm folder

`--buildings auto` downloads the Overture buildings that intersect the area, writes them under `out/inputs/`, and records the release, bounds, and sha256 in `inputs/buildings.json`. A second run reuses that file. A different area, or a file whose hash changed, stops the run.

`crowns --chm DIR` reads every 1 m tile listed in that run's `report.json`, inherits its building files and settings, and mosaics the tiles under the crown output's `inputs/` when there is more than one. The two output directories must differ. The 250-million-pixel limit still applies.

## Fields

On the crown vectors, in canopy mode:

| field | holds |
|---|---|
| `building_status` | 0 none, 1 overlap kept, 2 date unresolved, 3 stem moved |
| `building_height_m` | tallest supplied height among the overlapping buildings |
| `stem_shift_m` | how far the stem moved; 0 if it did not |
| `unconstrained_stem_x`, `unconstrained_stem_y` | the stem before the move |

`report.json` has `building_exclusion`: sources and hashes, pixels removed, and any stem the run refused.
