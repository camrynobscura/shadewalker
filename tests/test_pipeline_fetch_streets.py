"""Tests for pipeline/fetch/streets.py's handling of the two ways osmnx can
fail to hand back a usable graph: "no data here" (real for grid tiles that
only clip a borough's real coastline at their edge, still mostly open
water) and transient connection failures (real: three separate
ConnectionRefusedErrors during the Brooklyn run, each recovering within
seconds). The two need opposite handling -- the first means skip this tile
for good, the second means the same request would likely work if asked
again shortly -- so each gets its own tests here. Also covers the
CYCLEWAY_FILTER and FOOT_OVERRIDES_ACCESS_FILTER unions (fetch_streets
now makes three real Overpass queries, not one -- see the module for
why)."""

import re

import networkx as nx
import pytest
import requests
from osmnx._errors import InsufficientResponseError

from pipeline.config import Bbox
from pipeline.fetch import streets

BBOX = Bbox(lat_min=40.0, lat_max=40.1, lon_min=-74.0, lon_max=-73.9)


def _mock_fetch(monkeypatch, tmp_path, graph_from_bbox):
    monkeypatch.setattr(streets, "STREETS_DIR", tmp_path)
    monkeypatch.setattr(streets.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(streets.ox, "graph_from_bbox", graph_from_bbox)
    monkeypatch.setattr(streets.ox, "save_graphml", lambda graph, path: None)


def _by_filter(main_fn, cycleway_fn=None, access_override_fn=None):
    """Dispatch a graph_from_bbox mock by which of the three real queries
    fetch_streets makes -- WALK_FILTER (main), CYCLEWAY_FILTER (the
    shared-path union), or FOOT_OVERRIDES_ACCESS_FILTER (the
    foot-designated-despite-access=no/private union). Defaults the two
    narrower queries to "nothing here" (ValueError, the common real
    case) unless a test supplies its own function for one."""
    def dispatch(**kwargs):
        if kwargs.get("custom_filter") == streets.CYCLEWAY_FILTER:
            if cycleway_fn is not None:
                return cycleway_fn(**kwargs)
            raise ValueError("no foot-designated cycleways here")
        if kwargs.get("custom_filter") == streets.FOOT_OVERRIDES_ACCESS_FILTER:
            if access_override_fn is not None:
                return access_override_fn(**kwargs)
            raise ValueError("no foot-designated access=no/private ways here")
        return main_fn(**kwargs)
    return dispatch


def test_fetch_streets_returns_none_when_overpass_returns_no_data(monkeypatch, tmp_path):
    def raise_it(**kwargs):
        raise InsufficientResponseError("No data elements in server response.")

    _mock_fetch(monkeypatch, tmp_path, _by_filter(raise_it))
    assert streets.fetch_streets(BBOX, "test-water-tile") is None


def test_fetch_streets_returns_none_when_no_nodes_survive_polygon_clipping(monkeypatch, tmp_path):
    def raise_it(**kwargs):
        raise ValueError("Found no graph nodes within the requested polygon.")

    _mock_fetch(monkeypatch, tmp_path, _by_filter(raise_it))
    assert streets.fetch_streets(BBOX, "test-water-tile") is None


def test_fetch_streets_retries_on_connection_error_then_succeeds(monkeypatch, tmp_path):
    fake_graph = nx.MultiDiGraph()
    calls = {"count": 0}

    def flaky(**kwargs):
        calls["count"] += 1
        if calls["count"] < streets.MAX_FETCH_RETRIES:
            raise requests.exceptions.ConnectionError("connection refused")
        return fake_graph

    _mock_fetch(monkeypatch, tmp_path, _by_filter(flaky))
    assert streets.fetch_streets(BBOX, "test-flaky-tile") is fake_graph
    assert calls["count"] == streets.MAX_FETCH_RETRIES


def test_fetch_streets_raises_after_exhausting_retries(monkeypatch, tmp_path):
    def always_fails(**kwargs):
        raise requests.exceptions.ConnectionError("connection refused")

    _mock_fetch(monkeypatch, tmp_path, _by_filter(always_fails))
    with pytest.raises(requests.exceptions.ConnectionError):
        streets.fetch_streets(BBOX, "test-persistent-failure-tile")


def test_fetch_streets_asks_osmnx_for_all_components_with_the_walk_filter(monkeypatch, tmp_path):
    # Two kwargs where a one-line "cleanup" silently changes what data
    # exists, and no data-level test in CI can catch either (the pilot
    # tile happens not to depend on them):
    #   - retain_all=True is what keeps neighborhoods that merely *look*
    #     disconnected through one tile's peephole (Red Hook: expressway
    #     trench + water on three sides) from being deleted at fetch time.
    #     The keep-or-drop decision belongs to graph_store.load()'s global
    #     prune, which sees the whole merged borough.
    #   - custom_filter=WALK_FILTER is the centerline model; swapping back
    #     to network_type="walk" reintroduces the unnamed-sidewalk trap
    #     documented in CLAUDE.md.
    seen = {}

    def record_kwargs(**kwargs):
        seen.update(kwargs)
        return nx.MultiDiGraph()

    _mock_fetch(monkeypatch, tmp_path, _by_filter(record_kwargs))
    streets.fetch_streets(BBOX, "test-fetch-kwargs-tile")

    assert seen.get("retain_all") is True
    assert seen.get("custom_filter") == streets.WALK_FILTER


def test_fetch_streets_unions_in_foot_designated_cycleways(monkeypatch, tmp_path):
    # Some NYC bridges (confirmed real: Brooklyn Bridge's Brooklyn-side
    # landing) model their pedestrian path as a shared foot+bike cycleway
    # rather than a footway -- WALK_FILTER's highway allowlist doesn't
    # include cycleway at all, so without this union the landing would be
    # missing from the fetched data entirely, same as it was before this
    # fix (see PLAN.md).
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=0.0, y=0.0)

    cycleway_graph = nx.MultiDiGraph()
    cycleway_graph.add_node("c1", x=1.0, y=1.0)

    def cycleway_fn(**kwargs):
        assert kwargs.get("custom_filter") == streets.CYCLEWAY_FILTER
        return cycleway_graph

    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph, cycleway_fn))
    result = streets.fetch_streets(BBOX, "test-cycleway-union-tile")

    assert "m1" in result.nodes
    assert "c1" in result.nodes


def test_fetch_streets_works_with_no_cycleways_in_the_area(monkeypatch, tmp_path):
    # The common case: most tiles have zero foot=designated cycleways --
    # that must not be treated as an error, or block the main result.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=0.0, y=0.0)

    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph))
    result = streets.fetch_streets(BBOX, "test-no-cycleway-tile")

    assert list(result.nodes) == ["m1"]


def test_fetch_streets_unions_in_foot_designated_access_override_ways(monkeypatch, tmp_path):
    # Confirmed real on Queensboro Bridge: its own pedestrian walkway is
    # split across several segments of the same physical path, some
    # tagged access=no + foot=designated, others with no access tag at
    # all -- WALK_FILTER's own access clause drops exactly the access=no
    # segments (see PLAN.md). Without this union those segments would be
    # missing from the fetched data entirely, fragmenting the path.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=0.0, y=0.0)

    override_graph = nx.MultiDiGraph()
    override_graph.add_node("a1", x=2.0, y=2.0)

    def access_override_fn(**kwargs):
        assert kwargs.get("custom_filter") == streets.FOOT_OVERRIDES_ACCESS_FILTER
        return override_graph

    _mock_fetch(
        monkeypatch, tmp_path,
        _by_filter(lambda **kwargs: main_graph, access_override_fn=access_override_fn),
    )
    result = streets.fetch_streets(BBOX, "test-access-override-union-tile")

    assert "m1" in result.nodes
    assert "a1" in result.nodes


def test_fetch_streets_works_with_no_access_override_ways_in_the_area(monkeypatch, tmp_path):
    # The common case: most tiles have zero access=no/private-but-
    # foot-designated ways -- that must not be treated as an error, or
    # block the main result.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=0.0, y=0.0)

    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph))
    result = streets.fetch_streets(BBOX, "test-no-access-override-tile")

    assert list(result.nodes) == ["m1"]


def test_walk_filter_excludes_sidewalk_but_not_crossing():
    # A marked pedestrian crossing bridges a real gap -- e.g. connecting a
    # bridge's own footway to the street grid at its landing (confirmed
    # real on all 3 Manhattan<->Brooklyn bridges checked; see PLAN.md) --
    # rather than duplicating a street's own centerline the way a
    # parallel sidewalk does. The two used to be excluded together;
    # that silently disconnected every such landing. This pins the
    # distinction so a future "cleanup" can't quietly reintroduce it.
    footway_clause = re.search(r'\["footway"!~"([^"]+)"\]', streets.WALK_FILTER)
    assert footway_clause is not None
    excluded = footway_clause.group(1).split("|")
    assert "sidewalk" in excluded
    assert "crossing" not in excluded


def test_cycleway_filter_requires_foot_designated():
    # Scoped tightly on purpose: broadening this to admit every cycleway
    # (not just explicitly shared-use ones) would start routing
    # pedestrians down ordinary bike-only lanes.
    assert '"highway"="cycleway"' in streets.CYCLEWAY_FILTER
    assert '"foot"="designated"' in streets.CYCLEWAY_FILTER


def test_foot_overrides_access_filter_requires_an_explicit_foot_override():
    # Scoped tightly on purpose: this should only admit ways where foot
    # access is explicitly designated/yes despite a general access
    # restriction -- not every access=no/private way, which would
    # reintroduce genuinely gated/private ways WALK_FILTER's own clause
    # exists to keep out.
    assert '"foot"~"designated|yes"' in streets.FOOT_OVERRIDES_ACCESS_FILTER
    assert '"access"~"private|no"' in streets.FOOT_OVERRIDES_ACCESS_FILTER
