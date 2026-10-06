"""A pinned Overture building subset for one output directory."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def overture_features(bounds, cache):
    """Window public GeoParquet, retaining heights and provenance; never use ambient AWS credentials."""
    import duckdb
    import shapely

    release = public_json("https://stac.overturemaps.org/catalog.json").get("latest")
    if not isinstance(release, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.\d+", release):
        raise ValueError("Overture did not publish a recognisable latest release; supply a local building file")
    print(f"Buildings: Overture {release}; reading the AOI subset (first download may take several minutes)", flush=True)
    directory = cache / "duckdb-extensions"
    directory.mkdir(parents=True, exist_ok=True)
    urls = overture_files(bounds, release, cache)
    if not urls:
        return release, []
    west, south, east, north = bounds
    with duckdb.connect(config={"extension_directory": str(directory), "threads": 2, "memory_limit": "1GB",
                               "temp_directory": str(cache / "duckdb-scratch")}) as connection:
        connection.execute("INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs")
        connection.execute("SET s3_endpoint='s3.us-west-2.amazonaws.com'; SET s3_url_style='vhost'; SET s3_use_ssl=true")
        connection.execute("SET s3_region='us-west-2'; SET s3_access_key_id=''; SET s3_secret_access_key=''; SET s3_session_token=''")
        rows = connection.execute("""
            SELECT id, height, min_height, roof_height, to_json(sources), ST_AsWKB(geometry)
            FROM read_parquet(?)
            WHERE bbox.xmax >= ? AND bbox.xmin <= ? AND bbox.ymax >= ? AND bbox.ymin <= ?
            ORDER BY id
        """, [urls, west, east, south, north]).fetchall()
    features = [{"type": "Feature", "geometry": shapely.geometry.mapping(shapely.from_wkb(bytes(geometry))),
                 "properties": {"id": identifier, "height": height, "min_height": minimum, "roof_height": roof,
                                "sources": sources, "release": release}}
                for identifier, height, minimum, roof, sources, geometry in rows]
    print(f"Buildings: {len(features)} footprints read", flush=True)
    return release, features


def public_json(url):
    import urllib.request

    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def overture_files(bounds, release, cache):
    """Use every publisher file extent, including boundary intersections, before reading Parquet."""
    from concurrent.futures import ThreadPoolExecutor

    prefix = f"https://stac.overturemaps.org/{release}/buildings/building/"
    collection = public_json(prefix + "collection.json")
    items = [link["href"] for link in collection["links"] if link["rel"] == "item"]
    if not items or len(items) != collection["partition:file_count"] or any(not url.startswith(prefix) for url in items):
        raise ValueError("Overture file index is incomplete or outside the requested release")
    west, south, east, north = bounds
    selected, records = [], []
    asset_prefix = f"https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/release/{release}/theme=buildings/type=building/"
    with ThreadPoolExecutor(max_workers=8) as pool:
        for item in pool.map(public_json, items):
            left, bottom, right, top = item["bbox"]
            url = item["assets"]["aws"]["href"]
            if not url.startswith(asset_prefix) or not url.endswith(".parquet"):
                raise ValueError("Overture asset is outside the requested building release")
            intersects = right >= west and left <= east and top >= south and bottom <= north
            records.append({"id": item["id"], "bbox": item["bbox"], "href": url, "selected": intersects})
            if intersects:
                selected.append(url)
    (cache / f"overture_partitions__{release}.json").write_text(json.dumps(records, indent=2))
    print(f"Buildings: {len(selected)} of {len(items)} publisher files intersect the AOI", flush=True)
    return sorted(set(selected))


def automatic_buildings(bounds, cache):
    """Pin one AOI/release/file receipt per output directory; reruns do not silently refresh the source."""
    cache.mkdir(parents=True, exist_ok=True)
    receipt = cache / "buildings.json"
    bounds = [round(float(v), 9) for v in bounds]
    if receipt.exists():
        record = json.loads(receipt.read_text())
        path = cache / Path(record["file"]).name
        if record["bbox"] != bounds:
            raise ValueError("Cached buildings belong to another AOI; use a new output directory")
        if file_hash(path) != record["sha256"]:
            raise ValueError("Cached building file changed; inspect it before reusing the run")
        print(f"Buildings: reusing verified Overture {record['release']} subset", flush=True)
        return path
    release, features = overture_features(bounds, cache)
    path = cache / f"buildings_overture__{release[:10]}__src.geojson"
    payload = json.dumps({"type": "FeatureCollection", "features": features}, allow_nan=False).encode()
    temporary = path.with_suffix(".part")
    temporary.write_bytes(payload)
    temporary.replace(path)
    receipt.write_text(json.dumps({"bbox": bounds, "release": release, "file": path.name,
                                   "features": len(features), "sha256": file_hash(path)}, indent=2))
    return path
