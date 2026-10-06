"""Building exclusion on planted roofs and trees, without network or production writes."""
import importlib.util
import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box, mapping

ROOT = Path(os.environ.get("GISTOOLS_ROOT", Path(__file__).resolve().parents[1]))
STANDALONE = not (ROOT / "tools/cli/chm").exists()
sys.path.insert(0, str(ROOT / ("tools" if STANDALONE else "tools/cli")))
from building_mask import from_args


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


if STANDALONE:
    package = types.ModuleType("standalone_tools")
    package.__path__ = [str(ROOT / "tools")]
    sys.modules[package.__name__] = package
    CHM = importlib.import_module("standalone_tools.chm")
    CROWNS = importlib.import_module("standalone_tools.crowns")
    CHM.main = importlib.import_module("standalone_tools.chm.cli").main
    CHM.Grid = importlib.import_module("standalone_tools.chm.grid").Grid
    CROWNS.main = importlib.import_module("standalone_tools.crowns.cli").main
    CROWNS.Grid = importlib.import_module("standalone_tools.crowns.canopy").Grid
else:
    CHM = module("building_chm", ROOT / "tools/cli/chm/calibrated_chm.py")
    CROWNS = module("building_crowns", ROOT / "tools/cli/crowns/tree_crowns.py")
CRS = "EPSG:32631"
TRANSFORM = from_origin(500000, 5000060, 1, 1)


def footprints(path, shapes, crs=CRS):
    path.write_text(json.dumps({"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": crs}},
                               "features": [{"type": "Feature", "properties": {}, "geometry": mapping(g)} for g in shapes]}))
    return path


@pytest.fixture
def scene(tmp_path):
    rr, cc = np.indices((60, 60))
    tree = np.maximum(15 - 2 * np.hypot(rr - 35, cc - 40), 0).astype("float32")
    roof = (rr >= 8) & (rr < 18) & (cc >= 8) & (cc < 18)
    tree[roof] = 20
    raster = tmp_path / "scene.tif"
    with rasterio.open(raster, "w", driver="GTiff", height=60, width=60, count=1,
                       dtype="float32", crs=CRS, transform=TRANSFORM, nodata=np.nan) as dst:
        dst.write(tree, 1)
    shape = box(500008, 5000042, 500018, 5000052)
    file = footprints(tmp_path / "buildings.geojson", [shape])
    return raster, tree, file, shape


def test_identity_reprojection_and_missing_coverage(scene, tmp_path):
    from rasterio.warp import transform_geom
    from shapely.geometry import shape

    _raster, _data, path, polygon = scene
    buildings, mask = from_args([path], (60, 60), TRANSFORM, CRS)
    assert mask[12, 12] and not mask[35, 40]
    geographic = footprints(tmp_path / "geographic.geojson", [shape(transform_geom(CRS, "EPSG:4326", mapping(polygon)))], "EPSG:4326")
    _, other = from_args([geographic], (60, 60), TRANSFORM, CRS)
    assert other[12, 12] and not other[35, 40]
    # A changed, shifted footprint must change the mask, not merely its receipt.
    shifted = footprints(tmp_path / "shifted.geojson", [box(500038, 5000022, 500048, 5000032)])
    changed, moved = from_args([shifted], (60, 60), TRANSFORM, CRS)
    assert moved[35, 40] and not moved[12, 12]
    assert changed.sources[0]["sha256"] != buildings.sources[0]["sha256"]
    empty = footprints(tmp_path / "empty.geojson", [])
    no_buildings, empty_mask = from_args([empty], (60, 60), TRANSFORM, CRS)
    assert not empty_mask.any()
    assert "unverified" in no_buildings.report(empty_mask)["warning"]
    assert from_args(None, (60, 60), TRANSFORM, None) == (None, None)
    with pytest.raises(ValueError, match="CRS"):
        from_args([path], (60, 60), TRANSFORM, None)
    with pytest.raises(FileNotFoundError):
        from_args([tmp_path / "missing.gpkg"], (60, 60), TRANSFORM, CRS)


def test_geopackage_and_bad_geometries(scene, tmp_path):
    import pyogrio
    import shapely
    from pyogrio.raw import write

    _, _, path, polygon = scene
    gpkg = tmp_path / "buildings.gpkg"
    write(gpkg, shapely.to_wkb([polygon]), [], [], crs=CRS, geometry_type="Polygon", driver="GPKG", layer="buildings")
    _, mask = from_args([gpkg], (60, 60), TRANSFORM, CRS)
    _, expected = from_args([path], (60, 60), TRANSFORM, CRS)
    np.testing.assert_array_equal(mask, expected)
    point = footprints(tmp_path / "point.geojson", [polygon.centroid])
    with pytest.raises(ValueError, match="polygons"):
        from_args([point], (60, 60), TRANSFORM, CRS)
    assert pyogrio.read_info(gpkg)["crs"]


def test_crown_roof_removed_tree_retained_and_holes_stay_empty(scene, tmp_path):
    raster, data, path, polygon = scene
    plain = tmp_path / "plain"
    masked = tmp_path / "masked"
    args = ["--chm", str(raster), "--dem", "none", "--no-plot", "--outline", "smooth"]
    CROWNS.main([*args, "--out", str(plain)])
    CROWNS.main([*args, "--out", str(masked), "--buildings", str(path), "--building-mode", "strict"])
    with rasterio.open(plain / "crown_ids.tif") as src:
        before = src.read(1)
    with rasterio.open(masked / "crown_ids.tif") as src:
        after = src.read(1)
    assert before[12, 12] > 0 and after[12, 12] == 0
    assert before[35, 40] > 0 and after[35, 40] > 0
    with rasterio.open(masked / "canopy_height.tif") as src:
        assert np.isnan(src.read(1)[12, 12])
    report = json.loads((masked / "report.json").read_text())
    assert report["input"]["building_exclusion"]["excluded_pixels"] > 0
    import shapely
    from pyogrio.raw import read
    polys = shapely.from_wkb(read(masked / "tree_crowns.gpkg")[2])
    assert all(p.intersection(polygon).area < 1e-9 for p in polys)
    hole = np.zeros_like(data, dtype=bool)
    hole[35, 42] = True  # small hole normally filled by whole_crowns
    result = CROWNS.delineate(data, 1, excluded=hole)
    assert result.crowns[35, 42] == 0 and result.crowns[35, 40] > 0
    grid = CROWNS.Grid(500000, 5000060, 1, 60, 60)
    a = CROWNS.find_trees(data, grid, CROWNS.Rule(), 4096, "pixels", lambda _: None, excluded=hole)[0]
    b = CROWNS.find_trees(data, grid, CROWNS.Rule(), 20, "pixels", lambda _: None, excluded=hole)[0]
    np.testing.assert_array_equal(a, b)


def test_chm_exclusion_on_both_grids_without_coarse_expansion(scene, tmp_path):
    raster, _data, path, _polygon = scene
    grid = CHM.Grid(rasterio.crs.CRS.from_string(CRS), from_origin(500000, 5000060, 10, 10), 6, 6)
    out = tmp_path / "chm"
    with patch.object(CHM, "make_grid", return_value=grid), \
         patch.object(CHM, "read_eth", return_value=(np.full((6, 6), 12., dtype="float32"), ["fixture"])), \
         patch.object(CHM, "open_meta", side_effect=lambda *a: ([rasterio.open(raster)], [str(raster)])):
        CHM.main(["--bbox", "3", "45", "3.01", "45.01", "--out", str(out), "--debug" if STANDALONE else "--fine",
                  "--no-dates", "--no-plot", "--buildings", str(path), "--building-mode", "strict"])
    with rasterio.open(out / "chm_10m.tif") as src:
        coarse = src.read(1)
    with rasterio.open(out / "chm_1m_scene.tif") as src:
        fine = src.read(1)
    assert np.isnan(coarse[1, 1]) and np.isfinite(coarse[3, 4])
    assert np.isnan(fine[12, 12]) and np.isfinite(fine[35, 40])
    assert np.isfinite(fine[5, 5])  # same excluded coarse cell, outside footprint
    with rasterio.open(out / "chm_10m_source.tif") as src:
        assert src.read(1)[1, 1] == 0
    assert json.loads((out / "report.json").read_text())["building_exclusion"]["sources"][0]["sha256"]


def context_file(tmp_path, **extra):
    path = tmp_path / "context.json"
    path.write_text(json.dumps({"snapshot_year": 2020, "clearance_m": 1, "max_stem_shift_m": 2, **extra}))
    return path


def test_roof_height_overhang_dates_and_elevated_structure(scene, tmp_path):
    _raster, _data, path, _polygon = scene
    content = json.loads(path.read_text())
    content["features"][0]["properties"] = {"height": 12, "construction_year": 2010, "min_height": 0}
    path.write_text(json.dumps(content))
    context = context_file(tmp_path)
    buildings, footprint = from_args([path], (60, 60), TRANSFORM, CRS,
                                     {"mode": "canopy", "date": "2020-06-01", "context": context, "temporal_policy": "evidence"})
    buildings.observe([2020])
    values = np.full((60, 60), 12., dtype="float32")
    values[12, 12] = 20  # above-roof canopy is retained
    excluded = buildings.exclusion(values, TRANSFORM, CRS)
    assert excluded[10, 10] and not excluded[12, 12] and not excluded[35, 40]
    buildings.observe([2009])  # do not apply a later building to earlier imagery
    assert buildings.temporal(0) == "unknown" and not buildings.exclusion(values, TRANSFORM, CRS).any()
    before, _ = from_args([path], (60, 60), TRANSFORM, CRS, {"mode": "canopy", "date": "2009", "context": context})
    before.observe([2009])
    assert before.temporal(0) == "absent"
    no_date, _ = from_args([path], (60, 60), TRANSFORM, CRS, {"mode": "canopy"})
    assert not no_date.exclusion(values, TRANSFORM, CRS).any()
    buildings.years = {2020}
    buildings.attributes[0]["min_height_field"] = 5
    values[10, 10] = 3
    assert not buildings.exclusion(values, TRANSFORM, CRS)[10, 10]
    buildings.attributes[0]["height_field"] = None
    assert not buildings.exclusion(values, TRANSFORM, CRS).any()
    assert footprint.any()


def test_recent_snapshot_assumption_lifecycle_override_and_osm_tags(scene, tmp_path):
    from building_mask import number, optional_year

    _, data, path, _ = scene
    dated = path.with_name("buildings_osm__2026-08-19__src.geojson")
    content = json.loads(path.read_text())
    content["features"][0]["properties"] = {"other_tags": '"height"=>"12 m","min_height"=>"3","start_date"=>"1990"'}
    dated.write_text(json.dumps(content))
    buildings, _ = from_args([dated], data.shape, TRANSFORM, CRS, {"mode": "canopy", "date": "2026"})
    buildings.observe([2020])
    assert buildings.temporal(0) == "active"  # requested current product; original image year remains recorded
    assert buildings.report(np.zeros_like(data, dtype=bool))["comparison_years"] == [2020, 2026]
    assert buildings.attributes[0]["height_field"] == "12 m"
    assert number(buildings.attributes[0]["height_field"]) == 12
    assert number("10 ft") == pytest.approx(3.048)
    assert optional_year("~1990") is None and optional_year("1750") == 1750
    buildings.attributes[0]["construction_year_field"] = "2026"
    assert buildings.temporal(0) == "unknown"
    buildings.attributes[0]["construction_year_field"] = "2027"
    assert buildings.temporal(0) == "absent"
    buildings.attributes[0]["construction_year_field"] = None
    buildings.attributes[0]["demolition_year_field"] = "2026"
    assert buildings.temporal(0) == "unknown"
    buildings.attributes[0]["demolition_year_field"] = "2025"
    assert buildings.temporal(0) == "absent"
    buildings.attributes[0]["demolition_year_field"] = None
    buildings.options["date"] = "2010"
    assert buildings.temporal(0) == "unknown"  # recent snapshot cannot establish historical presence
    buildings.options["date"] = "2026"
    buildings.options["temporal_policy"] = "evidence"
    assert buildings.temporal(0) == "unknown"


def test_raster_years_and_adjusted_terrain_attributes(scene):
    from building_mask import adjust_stem, raster_years

    path, _, _, _ = scene
    with rasterio.open(path, "r+") as dst:
        dst.update_tags(acquisition_years="[2020, 2023]", acquisition_dates_complete="true")
    assert raster_years(path) == [2020, 2023]
    with rasterio.open(path, "r+") as dst:
        dst.update_tags(acquisition_dates_complete="false")
    assert raster_years(path) == []
    table = {key: np.array([value]) for key, value in {
        "stem_x": 0., "stem_y": 0., "top_x": 0., "top_y": 0., "ground_m": 10.,
        "height_m": 20., "trunk_from_top_m": 0., "slope_deg": 8.}.items()}
    adjust_stem(table, 0, (3., 4.), 12.)
    assert table["trunk_from_top_m"][0] == 5 and table["height_m"][0] == 18 and table["ground_m"][0] == 12
    assert np.isnan(table["slope_deg"][0])
    adjust_stem(table, 0, (3., 4.), np.nan)
    assert np.isnan(table["height_m"][0]) and np.isnan(table["ground_m"][0])


@pytest.mark.skipif(STANDALONE, reason="BioTerra fetch integration; standalone reads supplied vectors")
def test_fetchers_preserve_building_evidence(scene, tmp_path, monkeypatch):
    from types import SimpleNamespace

    import geopandas as gpd
    import pandas as pd
    from bioterra_ingest import osm

    _, _, _, polygon = scene
    frame = gpd.GeoDataFrame({"osm_id": [1], "building": ["yes"], "start_date": ["1990"],
                              "other_tags": ['"min_height"=>"3"'], "height": [12.], "geometry": [polygon]}, crs=CRS).to_crs(4326)
    monkeypatch.setattr(osm, "extracts_covering", lambda *a: [Path("fixture.pbf")])
    monkeypatch.setattr(osm, "_buildings_in", lambda *a: frame.copy())
    monkeypatch.setattr(osm.grids, "utm_crs", lambda *a: CRS)
    monkeypatch.setattr(osm.grids, "pad_bbox", lambda b: b)
    monkeypatch.setattr(osm.http, "last_modified_date", lambda: "2026-08-19")
    monkeypatch.setattr(osm.nm, "dest", lambda *a, **k: tmp_path / "buildings.gpkg")
    written = []
    monkeypatch.setattr(osm, "write_vector", lambda g, *a, **k: written.append(g))
    osm.fetch_buildings_osm(SimpleNamespace(), SimpleNamespace(id="buildings_osm"), lambda _: None)
    assert {"height", "start_date", "other_tags"} <= set(written[-1].columns)

    queries = []

    def overture(*args):
        queries.append(args[3])
        return pd.DataFrame({"id": ["b1"], "height": [12.], "min_height": [3.], "roof_height": [2.],
                             "sources": [[{"dataset": "fixture", "update_time": "2026-01-02"}]],
                             "geom": [frame.geometry.iloc[0].wkb]}), "2026-08-19.0"
    monkeypatch.setattr(osm, "_overture_frame", overture)
    osm.fetch_buildings_overture(SimpleNamespace(bbox=[0, 0, 1, 1]), SimpleNamespace(id="buildings_overture"), lambda _: None)
    assert {"min_height", "roof_height", "sources", "release"} <= set(written[-1].columns)
    assert "min_height" in queries[0] and json.loads(written[-1].sources.iloc[0])[0]["dataset"] == "fixture"


def test_constrained_stem_near_edge_and_rejected_roof_centre(scene, tmp_path):
    _raster, data, path, _polygon = scene
    context = context_file(tmp_path)
    buildings, _ = from_args([path], (60, 60), TRANSFORM, CRS, {"mode": "canopy", "date": "2020", "context": context})
    buildings.observe([2020])
    labels = np.zeros((60, 60), dtype="uint32")
    labels[8:18, 8:20] = 1
    polygons = np.array([box(500008, 5000042, 500020, 5000052)], dtype=object)
    table = {"tree_id": np.array([1]), "stem_x": np.array([500017.8]), "stem_y": np.array([5000047.5]),
             "top_x": np.array([500017.8]), "top_y": np.array([5000047.5]), "height_m": np.array([20.]), "ground_m": np.array([np.nan])}
    uniform = np.full_like(data, 20.)
    adjusted, shapes, numbers, evidence = buildings.constrain_stems(table, polygons, labels, uniform, TRANSFORM)
    assert len(shapes) == 1 and adjusted["building_status"][0] == 3
    assert adjusted["stem_x"][0] > 500018 and adjusted["stem_shift_m"][0] <= 2
    assert shapes[0].intersection(box(500008, 5000042, 500018, 5000052)).area > 0  # keep overhang
    assert numbers[12, 12] == 1 and not evidence["rejected_candidates"]
    adjusted["stem_x"][:] = 500012
    adjusted["stem_y"][:] = 5000047
    _rejected, shapes, numbers, evidence = buildings.constrain_stems(adjusted, polygons, labels, uniform, TRANSFORM)
    assert len(shapes) == 0 and not numbers.any() and evidence["rejected_candidates"]


def test_crown_canopy_mode_publishes_unknowns_and_preserves_nearby_tree(scene, tmp_path):
    raster, _, path, _ = scene
    with rasterio.open(raster, "r+") as dst:
        dst.update_tags(acquisition_year=2020)
    out = tmp_path / "canopy-mode"
    CROWNS.main(["--chm", str(raster), "--out", str(out), "--dem", "none", "--no-plot",
                 "--buildings", str(path), "--date", "2020", "--building-context", str(context_file(tmp_path))])
    report = json.loads((out / "report.json").read_text())
    candidates = report["input"]["building_exclusion"]["candidates"]
    assert candidates["rejected_candidates"]  # roof has no supported plausible outside trunk
    from pyogrio.raw import read
    meta, _, _, _ = read(out / "tree_stems.gpkg")
    assert "building_status" in meta["fields"] and "building_height_m" in meta["fields"]
    with rasterio.open(out / "crown_ids.tif") as src:
        labels = src.read(1)
        assert labels[12, 12] == 0 and labels[35, 40] > 0


def test_automatic_buildings_pin_scope_and_detect_changed_cache(scene, tmp_path, monkeypatch):
    import building_mask as mask

    _, _, path, _ = scene
    calls = []

    def fetch(bounds, cache):
        calls.append(bounds)
        return "2026-10-01.0", json.loads(path.read_text())["features"]

    monkeypatch.setattr(mask, "overture_features", fetch)
    cache = tmp_path / "automatic"
    result = mask.automatic_buildings([3, 45, 3.1, 45.1], cache)
    assert mask.automatic_buildings([3, 45, 3.1, 45.1], cache) == result and len(calls) == 1
    with pytest.raises(ValueError, match="another AOI"):
        mask.automatic_buildings([4, 45, 4.1, 45.1], cache)
    result.write_text(result.read_text() + " ")
    with pytest.raises(ValueError, match="changed"):
        mask.automatic_buildings([3, 45, 3.1, 45.1], cache)


def test_overture_file_selection_keeps_boundary_intersections(tmp_path, monkeypatch):
    import building_mask as mask

    release = "2026-09-23.1"
    prefix = f"https://stac.overturemaps.org/{release}/buildings/building/"
    assets = f"https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/release/{release}/theme=buildings/type=building/"
    extents = [[0, 0, 1, 1], [1, 0, 2, 1], [3, 0, 4, 1]]
    collection = {"links": [{"rel": "item", "href": f"{prefix}{i}.json"} for i in range(3)], "partition:file_count": 3}
    records = {f"{prefix}{i}.json": {"id": str(i), "bbox": bbox, "assets": {"aws": {"href": f"{assets}{i}.parquet"}}}
               for i, bbox in enumerate(extents)}
    monkeypatch.setattr(mask, "public_json", lambda url: collection if url.endswith("collection.json") else records[url])
    assert mask.overture_files([0.5, 0.5, 1, 1.5], release, tmp_path) == [f"{assets}0.parquet", f"{assets}1.parquet"]
    collection["partition:file_count"] = 4
    with pytest.raises(ValueError, match="incomplete"):
        mask.overture_files([0.5, 0.5, 1, 1.5], release, tmp_path)


def test_cli_auto_buildings_and_folder_inheritance(scene, tmp_path, monkeypatch):
    from rasterio.warp import transform_geom

    mask = importlib.import_module("standalone_tools.building_mask" if STANDALONE else "building_mask")
    raster, _, _, polygon = scene
    calls = []

    def fetch(bounds, cache):
        calls.append(bounds)
        return "2026-10-01.0", [{"type": "Feature", "properties": {"height": 50},
                                 "geometry": transform_geom(CRS, "EPSG:4326", mapping(polygon))}]

    monkeypatch.setattr(mask, "overture_features", fetch)
    grid = CHM.Grid(rasterio.crs.CRS.from_string(CRS), from_origin(500000, 5000060, 10, 10), 6, 6)
    out = tmp_path / "auto-chm"
    with patch.object(CHM, "make_grid", return_value=grid), \
         patch.object(CHM, "read_eth", return_value=(np.full((6, 6), 12., dtype="float32"), ["fixture"])), \
         patch.object(CHM, "open_meta", side_effect=lambda *a: ([rasterio.open(raster)], [str(raster)])):
        CHM.main(["--bbox", "3", "45", "3.01", "45.01", "--out", str(out), "--debug" if STANDALONE else "--fine",
                  "--no-dates", "--no-plot", "--buildings", "auto", "--date", "2026-10-06"])
    crowns = tmp_path / "auto-crowns"
    CROWNS.main(["--chm", str(out), "--out", str(crowns), "--dem", "none", "--no-plot"])
    report = json.loads((crowns / "report.json").read_text())["input"]["building_exclusion"]
    assert len(calls) == 1 and report["requested_date"] == "2026-10-06"
    assert report["options"]["mode"] == "canopy"
    with rasterio.open(crowns / "crown_ids.tif") as src:
        assert src.read(1)[12, 12] == 0 and src.read(1)[35, 40] > 0
    footprint = Path(report["sources"][0]["path"])
    footprint.write_text(footprint.read_text() + " ")
    with pytest.raises(ValueError, match="changed"):
        CROWNS.main(["--chm", str(out), "--out", str(tmp_path / "changed"), "--dem", "none", "--no-plot"])


def test_mosaic_folder_keeps_dates_nodata_and_seam_tree(tmp_path):
    from types import SimpleNamespace

    import building_mask as mask

    folder = tmp_path / "chm"
    folder.mkdir()
    rr, cc = np.indices((40, 80))
    values = np.maximum(15 - np.hypot(rr - 20, cc - 39.5), 0).astype("float32")
    values[:2, :] = np.nan
    paths = []
    for part, when in enumerate((2020, 2023)):
        path = folder / f"chm_1m_{part}.tif"
        with rasterio.open(path, "w", driver="GTiff", count=1, width=40, height=40, dtype="float32",
                           crs=CRS, transform=from_origin(500000 + part * 40, 5000060, 1, 1), nodata=np.nan) as dst:
            dst.write(values[:, part * 40:(part + 1) * 40], 1)
            dst.update_tags(acquisition_year=when)
        paths.append(path)
    (folder / "report.json").write_text(json.dumps({"fine_files": [p.name for p in paths]}))
    args = SimpleNamespace(chm=folder, out=tmp_path / "mosaic")
    mask.prepare_canopy(args)
    with rasterio.open(args.chm) as src:
        np.testing.assert_array_equal(src.read(1), values)
    assert mask.raster_years(args.chm) == [2020, 2023]
    CROWNS.main(["--chm", str(folder), "--out", str(tmp_path / "crowns"), "--dem", "none", "--no-plot"])
    report = json.loads((tmp_path / "crowns/report.json").read_text())
    assert report["found"]["trees"] == 1
