"""Tests for pipeline/graph/centerline.py's build_edge_table() and
_normalize_name().

build_edge_table() only needs a networkx.MultiDiGraph shaped the way osmnx
produces one (node x/y attrs, a graph-level crs) -- no real OSM fetch or
network access required, confirmed by hand-building one below.
"""

import networkx as nx
import numpy as np
import pytest

from pipeline.graph.centerline import _normalize_name, build_edge_table


def _two_way_graph() -> nx.MultiDiGraph:
    """Two nodes ~150m apart with both directed edges present, the way
    osmnx represents a two-way street before we collapse it."""
    g = nx.MultiDiGraph(crs="epsg:4326")
    g.add_node(1, x=-73.99, y=40.68)
    g.add_node(2, x=-73.989, y=40.681)
    g.add_edge(1, 2, key=0, length=150.0, name="Test St", oneway=False)
    g.add_edge(2, 1, key=0, length=150.0, name="Test St", oneway=False)
    return g


def test_collapses_directed_pairs_into_one_undirected_edge():
    _, edges = build_edge_table(_two_way_graph())
    assert len(edges) == 1


def test_geometry_m_is_reprojected_to_meters_not_degrees():
    _, edges = build_edge_table(_two_way_graph())
    row = edges.iloc[0]

    lon, lat = row["geometry"].coords[0]
    assert -75 < lon < -73  # still plain lon/lat degrees
    assert 40 < lat < 41

    x, y = row["geometry_m"].coords[0]
    assert x > 1000  # UTM 18N eastings/northings are in the hundreds of thousands
    assert y > 1000


def test_length_m_column_is_carried_over_from_osmnx():
    _, edges = build_edge_table(_two_way_graph())
    assert edges.iloc[0]["length_m"] == 150.0


def test_missing_name_column_entirely_falls_back_to_empty_string():
    # A graph where no edge has a "name" attribute at all (unnamed park
    # paths, alleys) -- osmnx never creates the "name" column in that case,
    # distinct from an edge that has the attribute set to None/NaN.
    g = nx.MultiDiGraph(crs="epsg:4326")
    g.add_node(1, x=-73.99, y=40.68)
    g.add_node(2, x=-73.989, y=40.681)
    g.add_edge(1, 2, key=0, length=40.0, oneway=False)
    g.add_edge(2, 1, key=0, length=40.0, oneway=False)

    _, edges = build_edge_table(g)
    assert edges.iloc[0]["name"] == ""


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Court Street", "Court Street"),
        (["Court Street", "Warren Street"], "Court Street"),
        (None, ""),
        (np.nan, ""),
    ],
)
def test_normalize_name(raw, expected):
    assert _normalize_name(raw) == expected
