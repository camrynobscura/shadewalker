"""Tests for pipeline/export.py's write_tile() -- the contract between the
pipeline and the routing server.

The module's own docstring flags a hard-won rule: everything written must be
lat/lon degrees (EPSG:4326), never the meter-based geometry_m -- that exact
bug shipped once in the v1 prototype. The main test below pins it directly by
giving write_tile() an edge with *both* geometry columns present (as real
edges have) and asserting the export used the degrees one.
"""

import gzip
import json

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString

from pipeline import config, export


def test_write_tile_exports_lon_lat_degrees_not_utm_meters(tmp_path, monkeypatch):
    # write_tile()'s own log line does out_path.relative_to(config.REPO_ROOT),
    # so REPO_ROOT needs to be patched alongside TILES_DIR -- otherwise a
    # tmp_path outside the real repo makes that relative_to() raise.
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(config, "TILES_DIR", tmp_path / "tiles")

    nodes = gpd.GeoDataFrame({"x": [-73.99, -73.989], "y": [40.68, 40.681]}, index=[1, 2])
    edges = gpd.GeoDataFrame(
        {
            "length_m": [150.0],
            "name": ["Test St"],
            "tree_deciduous": [2.5],
            "tree_evergreen": [0.0],
            "tree_count": [3],
            "geometry": [LineString([(-73.99, 40.68), (-73.989, 40.681)])],
            # A real edge also carries geometry_m -- present here on purpose,
            # so this test would catch write_tile() ever reading the wrong
            # one of the two columns.
            "geometry_m": [LineString([(585435.9, 4503837.4), (585352.7, 4503950.1)])],
        },
        index=pd.MultiIndex.from_tuples([(1, 2, 0)], names=["u", "v", "key"]),
        geometry="geometry",
    )

    export.write_tile("test_tile", nodes, edges)

    out_path = tmp_path / "tiles" / "test_tile.json.gz"
    assert out_path.exists()
    with gzip.open(out_path, "rt") as f:
        tile = json.load(f)

    assert tile["meta"]["tile_id"] == "test_tile"
    assert tile["meta"]["crs"] == "EPSG:4326"
    assert tile["meta"]["coord_order"] == "lon,lat"
    assert tile["meta"]["node_count"] == 2
    assert tile["meta"]["edge_count"] == 1

    assert tile["nodes"]["1"] == [-73.99, 40.68]

    edge = tile["edges"][0]
    assert edge["name"] == "Test St"
    assert edge["tree_count"] == 3
    for lon, lat in edge["coords"]:
        # lon/lat degrees, not UTM 18N meters (which would be in the
        # hundreds of thousands) -- this is what would fail if export.py
        # ever read geometry_m instead of geometry.
        assert -75 < lon < -73
        assert 40 < lat < 41
