"""Tests for pipeline/fetch/streets.py's handling of the two ways osmnx can
fail to hand back a usable graph: "no data here" (real for grid tiles that
only clip a borough's real coastline at their edge, still mostly open
water) and transient connection failures (real: three separate
ConnectionRefusedErrors during the Brooklyn run, each recovering within
seconds). The two need opposite handling -- the first means skip this tile
for good, the second means the same request would likely work if asked
again shortly -- so each gets its own tests here. Also covers the
CYCLEWAY_FILTER, FOOT_OVERRIDES_ACCESS_FILTER, and NAMED_SIDEWALK_FILTER
unions (fetch_streets now makes four real Overpass queries, not one --
see the module for why), and the compose-before-simplify fix (each of the
four is fetched unsimplified and simplified once after composing, not
simplified independently before composing -- see fetch_streets()'s own
docstring for the real bug, High Bridge, this fixes)."""

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


def _by_filter(main_fn, cycleway_fn=None, access_override_fn=None, named_sidewalk_fn=None):
    """Dispatch a graph_from_bbox mock by which of the four real queries
    fetch_streets makes -- WALK_FILTER (main), CYCLEWAY_FILTER (the
    shared-path union), FOOT_OVERRIDES_ACCESS_FILTER (the
    foot-designated-despite-access=no/private union), or
    NAMED_SIDEWALK_FILTER (the named-park-path union). Defaults the three
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
        if kwargs.get("custom_filter") == streets.NAMED_SIDEWALK_FILTER:
            if named_sidewalk_fn is not None:
                return named_sidewalk_fn(**kwargs)
            raise ValueError("no named sidewalk-tagged park paths here")
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
    # fetch_streets() now runs every result through simplify_graph() once
    # (see the module for why), which always returns a new graph object --
    # so this checks the result's shape rather than its identity.
    fake_graph = nx.MultiDiGraph()
    calls = {"count": 0}

    def flaky(**kwargs):
        calls["count"] += 1
        if calls["count"] < streets.MAX_FETCH_RETRIES:
            raise requests.exceptions.ConnectionError("connection refused")
        return fake_graph

    _mock_fetch(monkeypatch, tmp_path, _by_filter(flaky))
    result = streets.fetch_streets(BBOX, "test-flaky-tile")
    assert list(result.nodes) == []
    assert calls["count"] == streets.MAX_FETCH_RETRIES


def test_fetch_streets_raises_after_exhausting_retries(monkeypatch, tmp_path):
    def always_fails(**kwargs):
        raise requests.exceptions.ConnectionError("connection refused")

    _mock_fetch(monkeypatch, tmp_path, _by_filter(always_fails))
    with pytest.raises(requests.exceptions.ConnectionError):
        streets.fetch_streets(BBOX, "test-persistent-failure-tile")


def test_fetch_streets_asks_osmnx_for_all_components_with_the_walk_filter(monkeypatch, tmp_path):
    # Three kwargs where a one-line "cleanup" silently changes what data
    # exists, and no data-level test in CI can catch any of them (the
    # pilot tile happens not to depend on them):
    #   - retain_all=True is what keeps neighborhoods that merely *look*
    #     disconnected through one tile's peephole (Red Hook: expressway
    #     trench + water on three sides) from being deleted at fetch time.
    #     The keep-or-drop decision belongs to graph_store.load()'s global
    #     prune, which sees the whole merged borough.
    #   - custom_filter=WALK_FILTER is the centerline model; swapping back
    #     to network_type="walk" reintroduces the unnamed-sidewalk trap
    #     documented in CLAUDE.md.
    #   - simplify=False keeps each of the three filters' own fetch from
    #     simplifying in isolation, before it's had a chance to see the
    #     other two filters' ways -- see _fetch_with_retry's own comment
    #     for the real bug (High Bridge) this caused.
    seen = {}

    def record_kwargs(**kwargs):
        seen.update(kwargs)
        return nx.MultiDiGraph()

    _mock_fetch(monkeypatch, tmp_path, _by_filter(record_kwargs))
    streets.fetch_streets(BBOX, "test-fetch-kwargs-tile")

    assert seen.get("retain_all") is True
    assert seen.get("custom_filter") == streets.WALK_FILTER
    assert seen.get("simplify") is False


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


def test_fetch_streets_simplifies_once_after_composing_all_four_results(monkeypatch, tmp_path):
    # The actual bug fix, pinned directly. Real case (High Bridge,
    # confirmed 2026-07-18): a node shared between WALK_FILTER's and
    # CYCLEWAY_FILTER's own results looks like a plain pass-through to
    # each filter's own isolated simplification pass, so simplifying each
    # fetch BEFORE composing them can drop a real intersection's
    # connection to one side entirely (see PLAN.md/HISTORY.md for the
    # full mechanism). Simplifying is un-mockable in the small, no-network
    # unit tests elsewhere in this file (they bypass osmnx's real
    # graph_from_bbox, so its real simplify=True behavior never runs) --
    # so this test doesn't reproduce the bug on real data, it pins the
    # mechanism of the fix: simplify_graph() is called exactly once, on
    # the graph that already has all four filters' nodes composed in,
    # not once per filter before composing.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=0.0, y=0.0)
    cycleway_graph = nx.MultiDiGraph()
    cycleway_graph.add_node("c1", x=1.0, y=1.0)
    override_graph = nx.MultiDiGraph()
    override_graph.add_node("a1", x=2.0, y=2.0)
    named_sidewalk_graph = nx.MultiDiGraph()
    named_sidewalk_graph.add_node("n1", x=3.0, y=3.0)

    calls = []

    def fake_simplify(graph, **kwargs):
        calls.append(set(graph.nodes))
        return graph

    monkeypatch.setattr(streets.ox.simplification, "simplify_graph", fake_simplify)
    _mock_fetch(
        monkeypatch, tmp_path,
        _by_filter(
            lambda **kwargs: main_graph,
            lambda **kwargs: cycleway_graph,
            lambda **kwargs: override_graph,
            lambda **kwargs: named_sidewalk_graph,
        ),
    )
    streets.fetch_streets(BBOX, "test-simplify-once-tile")

    assert len(calls) == 1
    assert calls[0] == {"m1", "c1", "a1", "n1"}


def test_fetch_streets_works_with_no_access_override_ways_in_the_area(monkeypatch, tmp_path):
    # The common case: most tiles have zero access=no/private-but-
    # foot-designated ways -- that must not be treated as an error, or
    # block the main result.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=0.0, y=0.0)

    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph))
    result = streets.fetch_streets(BBOX, "test-no-access-override-tile")

    assert list(result.nodes) == ["m1"]


def test_fetch_streets_unions_in_named_sidewalk_tagged_park_paths(monkeypatch, tmp_path):
    # Confirmed real: Central Park's own "Central Park Outer Loop" is
    # tagged footway=sidewalk right at its W65th/Central Park West
    # entrance (it IS, in OSM's tagging convention, the "sidewalk" of the
    # park's own internal drive road) -- WALK_FILTER's blanket
    # footway!=sidewalk exclusion was dropping it there, forcing a ~700m
    # detour to a different entrance for two points only ~100m apart in
    # reality (see PLAN.md). Without this union, named interior park
    # paths tagged as a sidewalk would be missing from the fetched data
    # entirely, same as before this fix.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=0.0, y=0.0)

    named_sidewalk_graph = nx.MultiDiGraph()
    named_sidewalk_graph.add_node("n1", x=3.0, y=3.0)

    def named_sidewalk_fn(**kwargs):
        assert kwargs.get("custom_filter") == streets.NAMED_SIDEWALK_FILTER
        return named_sidewalk_graph

    _mock_fetch(
        monkeypatch, tmp_path,
        _by_filter(lambda **kwargs: main_graph, named_sidewalk_fn=named_sidewalk_fn),
    )
    result = streets.fetch_streets(BBOX, "test-named-sidewalk-union-tile")

    assert "m1" in result.nodes
    assert "n1" in result.nodes


def test_fetch_streets_works_with_no_named_sidewalk_park_paths_in_the_area(monkeypatch, tmp_path):
    # The common case: most tiles have zero named footway=sidewalk ways --
    # that must not be treated as an error, or block the main result.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=0.0, y=0.0)

    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph))
    result = streets.fetch_streets(BBOX, "test-no-named-sidewalk-tile")

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


def test_named_sidewalk_filter_requires_both_sidewalk_and_a_name():
    # Scoped tightly on purpose: this should only admit footway=sidewalk
    # ways that ALSO have a real name -- not every sidewalk, which would
    # reintroduce the thousands of unnamed sidewalk fragments
    # WALK_FILTER's own footway!=sidewalk clause exists to keep out.
    assert '"footway"="sidewalk"' in streets.NAMED_SIDEWALK_FILTER
    assert '["name"]' in streets.NAMED_SIDEWALK_FILTER
