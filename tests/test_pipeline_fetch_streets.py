"""Tests for pipeline/fetch/streets.py's handling of the two ways osmnx can
fail to hand back a usable graph: "no data here" (real for grid tiles that
only clip a borough's real coastline at their edge, still mostly open
water) and transient connection failures (real: three separate
ConnectionRefusedErrors during the Brooklyn run, each recovering within
seconds). The two need opposite handling -- the first means skip this tile
for good, the second means the same request would likely work if asked
again shortly -- so each gets its own tests here. Also covers the
CYCLEWAY_FILTER, FOOT_OVERRIDES_ACCESS_FILTER, NAMED_SIDEWALK_FILTER, and
ANY_SIDEWALK_FILTER unions (fetch_streets now makes five real Overpass
queries, not one -- see the module for why), and the
compose-before-simplify fix (each of the five is fetched unsimplified and
simplified once after composing, not simplified independently before
composing -- see fetch_streets()'s own docstring for the real bug, High
Bridge, this fixes)."""

import json
import re

import networkx as nx
import pytest
import requests
from osmnx._errors import InsufficientResponseError
from shapely.geometry import LineString, box
from shapely.ops import transform as shapely_transform
from shapely.strtree import STRtree

from pipeline import config
from pipeline.config import Bbox
from pipeline.fetch import citywide_layers, interior_sidewalks, park_trails, streets

BBOX = Bbox(lat_min=40.0, lat_max=40.1, lon_min=-74.0, lon_max=-73.9)


def _isolate_citywide_layers(monkeypatch, tmp_path):
    """Point the citywide layer cache at a temp dir and clear the
    per-process memo -- without this, one test's mocked layer would leak
    into every later test in the session (the memo is deliberately
    process-lived in production), and marker files would land in the real
    data/raw/citywide_layers/."""
    monkeypatch.setattr(citywide_layers, "CACHE_DIR", tmp_path / "citywide_layers")
    monkeypatch.setattr(citywide_layers, "_MEMO", {})
    monkeypatch.setattr(citywide_layers, "_FRESHENED", set())


def _mock_fetch(monkeypatch, tmp_path, graph_from_bbox):
    monkeypatch.setattr(streets, "STREETS_DIR", tmp_path)
    monkeypatch.setattr(streets, "RAW_STREETS_DIR", tmp_path / "streets_raw")
    monkeypatch.setattr(streets.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(streets.ox, "graph_from_bbox", graph_from_bbox)
    # In-memory stand-ins for GraphML save/load, NOT a bare no-op save:
    # the raw-cache layer (FIXES item 11) re-loads the snapshot it just
    # saved, so a no-op save would crash it -- and these wiring tests use
    # toy string node ids ("m1") that osmnx's REAL load would cast to int
    # and choke on. The dedicated raw-cache tests further down use the
    # real save/load with realistic int ids instead; wiring tests here are
    # about composition, not GraphML fidelity. Returned so a test can
    # inspect what got written where.
    saved = {}
    monkeypatch.setattr(
        streets.ox, "save_graphml", lambda graph, path: saved.__setitem__(str(path), graph)
    )
    monkeypatch.setattr(streets.ox, "load_graphml", lambda path: saved[str(path)])
    # The foot-forbidden clip index is memoized per process (see
    # streets._FOOT_FORBIDDEN_INDEX) -- reset per test for the same
    # isolation reason citywide_layers._MEMO is.
    monkeypatch.setattr(streets, "_FOOT_FORBIDDEN_INDEX", {})
    _isolate_citywide_layers(monkeypatch, tmp_path)
    # Both direct-HTTP layers _by_filter can't intercept -- park_trails (NYC
    # Open Data) and interior_sidewalks (ArcGIS) -- default to "nothing here"
    # (the common real case). Every test needs both mocked, not just the ones
    # that care: unmocked, they go live on a cold CI cache and fail on any
    # transient upstream hiccup (a park-trails 500 once failed the whole
    # suite; interior_sidewalks is the same latent flake -- FIXES item 14).
    # A test that WANTS data overrides the relevant one with its own geojson
    # AFTER calling _mock_fetch, so the override wins (see the union tests).
    monkeypatch.setattr(
        park_trails, "fetch_park_trails",
        lambda **kwargs: {"type": "FeatureCollection", "features": []},
    )
    monkeypatch.setattr(
        interior_sidewalks, "fetch_interior_sidewalks",
        lambda **kwargs: {"type": "FeatureCollection", "features": []},
    )
    return saved


def _by_filter(
    main_fn,
    cycleway_fn=None,
    access_override_fn=None,
    named_sidewalk_fn=None,
    any_sidewalk_fn=None,
    parking_aisle_fn=None,
    barrier_fn=None,
    foot_forbidden_fn=None,
):
    """Dispatch a graph_from_bbox mock by which of the eight real queries
    fetch_streets makes -- WALK_FILTER (main), CYCLEWAY_FILTER (the
    shared-path union), FOOT_OVERRIDES_ACCESS_FILTER (the
    foot-designated-despite-access=no/private union), NAMED_SIDEWALK_FILTER
    (the named-park-path union), ANY_SIDEWALK_FILTER (every sidewalk,
    narrowed to park reach afterwards), PARKING_AISLE_FILTER (every
    parking aisle, narrowed to through-paths afterwards), or BARRIER_FILTER
    (fence/wall/hedge ways, used to veto an interior-sidewalk connection
    that would cross one). Defaults the six narrower queries to "nothing
    here" (ValueError, the common real case) unless a test supplies its
    own function for one."""
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
        if kwargs.get("custom_filter") == streets.ANY_SIDEWALK_FILTER:
            if any_sidewalk_fn is not None:
                return any_sidewalk_fn(**kwargs)
            raise ValueError("no sidewalk-tagged ways here")
        if kwargs.get("custom_filter") == streets.PARKING_AISLE_FILTER:
            if parking_aisle_fn is not None:
                return parking_aisle_fn(**kwargs)
            raise ValueError("no parking aisles here")
        if kwargs.get("custom_filter") == getattr(streets, "BARRIER_FILTER", object()):
            if barrier_fn is not None:
                return barrier_fn(**kwargs)
            raise ValueError("no barrier ways here")
        # The foot-forbidden clip layer (v23) -- MUST be dispatched before
        # the main_fn fallthrough: unmatched, the clip layer would come
        # back as a copy of the WALK graph and silently clip imported test
        # fixtures against the test's own streets.
        if kwargs.get("custom_filter") in (
            getattr(streets, "FOOT_FORBIDDEN_FILTER", object()),
            getattr(streets, "ACCESS_FORBIDDEN_FILTER", object()),
        ):
            if foot_forbidden_fn is not None:
                return foot_forbidden_fn(**kwargs)
            raise ValueError("no forbidden paths here")
        return main_fn(**kwargs)
    return dispatch


class _FakeParkReach:
    """Stands in for canopy.citywide_park_reach_m()'s prepared geometry.

    Takes the same .contains(point) call the real one does, answering from
    a plain lon/lat box rather than the real citywide park union -- these
    tests are about fetch_streets()'s wiring, not about park geometry
    (pipeline/graph/boundary.py's own tests cover that). Reprojection into
    METRIC_CRS happens before .contains() is called, so the box is
    expressed in projected meters.
    """

    def __init__(self, min_x, min_y, max_x, max_y):
        self.bounds = (min_x, min_y, max_x, max_y)

    def contains(self, point):
        min_x, min_y, max_x, max_y = self.bounds
        return min_x <= point.x <= max_x and min_y <= point.y <= max_y


def _park_reach_around(lon, lat, half_width_m=200.0):
    """A fake park-reach shape covering half_width_m around one lon/lat --
    built by reprojecting that point the same way streets.py does, so the
    box lands where the real code will look for it."""
    x, y = streets._TO_METRIC_CRS(lon, lat)
    return _FakeParkReach(x - half_width_m, y - half_width_m, x + half_width_m, y + half_width_m)


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


def test_fetch_streets_retries_on_mid_response_drop_then_succeeds(monkeypatch, tmp_path):
    # ChunkedEncodingError is a connection dying mid-response rather than
    # at connect time; it is a RequestException sibling of ConnectionError,
    # not a subclass, so an unlisted catch lets it kill a whole borough run
    # (Queens tile 76/155, 2026-08-15).
    fake_graph = nx.MultiDiGraph()
    calls = {"count": 0}

    def flaky(**kwargs):
        calls["count"] += 1
        if calls["count"] < streets.MAX_FETCH_RETRIES:
            raise requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead")
        return fake_graph

    _mock_fetch(monkeypatch, tmp_path, _by_filter(flaky))
    result = streets.fetch_streets(BBOX, "test-midstream-drop-tile")
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

    # In-BBOX coordinates on purpose: aux results now arrive as slices of
    # a citywide layer (FIXES item 6b), and the slice keeps only nodes
    # inside the tile's bbox -- a node parked at (1, 1) would be sliced
    # away before fetch_streets ever composed it.
    cycleway_graph = nx.MultiDiGraph()
    cycleway_graph.add_node("c1", x=-73.951, y=40.051)

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
    override_graph.add_node("a1", x=-73.952, y=40.052)  # in-BBOX: see cycleway test

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


def test_fetch_streets_simplifies_once_after_composing_all_five_results(monkeypatch, tmp_path):
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
    # the graph that already has all five filters' nodes composed in,
    # not once per filter before composing.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=0.0, y=0.0)
    cycleway_graph = nx.MultiDiGraph()
    cycleway_graph.add_node("c1", x=-73.951, y=40.051)  # in-BBOX: see cycleway test
    override_graph = nx.MultiDiGraph()
    override_graph.add_node("a1", x=-73.952, y=40.052)
    named_sidewalk_graph = nx.MultiDiGraph()
    named_sidewalk_graph.add_node("n1", x=-73.953, y=40.053)
    any_sidewalk_graph = nx.MultiDiGraph()
    any_sidewalk_graph.add_node("s1", x=-73.95, y=40.05)
    any_sidewalk_graph.add_node("s2", x=-73.9501, y=40.0501)
    any_sidewalk_graph.add_edge("s1", "s2")

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
            lambda **kwargs: any_sidewalk_graph,
        ),
    )
    streets.fetch_streets(
        BBOX, "test-simplify-once-tile", park_reach=_park_reach_around(-73.95, 40.05)
    )

    assert len(calls) == 1
    assert calls[0] == {"m1", "c1", "a1", "n1", "s1", "s2"}


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
    named_sidewalk_graph.add_node("n1", x=-73.953, y=40.053)  # in-BBOX: see cycleway test

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


def test_walk_filter_admits_bridleway():
    # NYC's bridle paths are walked and run on daily -- Central Park's
    # reservoir loop, Prospect Park's bridle path (100% of that park's
    # fixable coverage gap) -- and a citywide tag survey found ZERO of the
    # city's 32.45km tagged foot=no, with 37% carrying no foot tag at all.
    # That's why bridleway sits in this broad allowlist rather than getting
    # a narrow foot=designated-only query like CYCLEWAY_FILTER: narrowing
    # would silently drop ~12km of real walkable path. Pinned so a future
    # "why is a horse path in the walk filter?" cleanup has to read the
    # reasoning first.
    highway_clause = re.search(r'\["highway"~"([^"]+)"\]', streets.WALK_FILTER)
    assert highway_clause is not None
    assert "bridleway" in highway_clause.group(1).split("|")
    # The restricted mileage is excluded by access, not by omission -- so
    # that clause has to stay for admitting bridleway to remain safe.
    assert '["access"!~"^(private|no)$"]' in streets.WALK_FILTER


def test_walk_filter_admits_track():
    # highway=track: OSM's own pedestrian-navigation guidelines
    # (wiki.openstreetmap.org/wiki/Guidelines_for_pedestrian_navigation)
    # list track as a default "pedestrian (distinct) way," the same
    # category as footway/path/steps -- not something needing a special
    # foot=yes override the way a shared carriageway does. A full
    # citywide tag survey (FIXES.md item 1f, 2026-08-09) found 497 real
    # NYC track ways; 323 (65%) already pass this filter's existing
    # foot/access clauses unchanged. Manually checked every real risk
    # cluster in that admitted set against satellite imagery (an untagged
    # paved path network in Pelham Bay, Rockaway/Tilden Beach's service
    # roads, a beach-path access=customers cluster, Ocean Breeze Park) --
    # all confirmed real public paths, not private facilities slipping
    # through on a missing access tag.
    highway_clause = re.search(r'\["highway"~"([^"]+)"\]', streets.WALK_FILTER)
    assert highway_clause is not None
    assert "track" in highway_clause.group(1).split("|")
    # Same safety net bridleway's own test re-asserts above: admitting a
    # new type only stays safe as long as this clause keeps excluding
    # genuinely private/gated ways.
    assert '["access"!~"^(private|no)$"]' in streets.WALK_FILTER


def test_walk_filter_excludes_foot_private():
    # Found during the highway=track survey (FIXES.md item 1f,
    # 2026-08-09): 4 real ways (near Marine Park/Bergen Beach) are
    # tagged foot=private -- explicitly not public for pedestrians -- but
    # this clause only ever excluded foot=no, so these would have slipped
    # straight through untouched. Applies to every highway type this
    # filter admits, not just track.
    foot_clause = re.search(r'\["foot"!~"([^"]+)"\]', streets.WALK_FILTER)
    assert foot_clause is not None
    pattern = foot_clause.group(1)
    # The pattern is anchored (see test_foot_and_access_clauses_are_anchored
    # for why), so assert by matching values against it, not by splitting
    # the string: exactly no and private excluded, unknown NOT excluded.
    assert re.search(pattern, "no")
    assert re.search(pattern, "private")
    assert not re.search(pattern, "unknown")


def test_field_check_excluded_ways_drop_their_edges_and_stranded_nodes():
    # The two entries are real, field-checked 2026-08-19 (an overgrown
    # informal path on unbuilt Waring Ave; an impassable Fort Washington
    # trail Google routes 2.6mi around) -- see FIELD_CHECK_EXCLUDED_WAY_IDS.
    excluded_id = next(iter(streets.FIELD_CHECK_EXCLUDED_WAY_IDS))
    graph = nx.MultiDiGraph()
    graph.add_edge("a", "b", osmid=excluded_id)          # scalar form
    graph.add_edge("b", "c", osmid=[excluded_id, 42])    # merged-list form
    graph.add_edge("c", "d", osmid=42)                   # unrelated, kept
    graph.add_node("island")                             # pre-existing isolate, kept

    result = streets._drop_field_check_excluded_ways(graph, "test-tile")

    assert set(result.edges()) == {("c", "d")}
    # "a"/"b" were stranded BY the drop and go; "c"/"d" still carry an
    # edge; "island" was isolated before the drop and must survive (aux
    # slices keep genuinely isolated nodes on purpose).
    assert set(result.nodes) == {"c", "d", "island"}


def test_field_check_excluded_ways_registry_is_documented():
    # Every entry must say WHY (the field-check evidence) -- an id with no
    # reason can't be re-verified when OSM drifts (REFETCH ritual).
    assert streets.FIELD_CHECK_EXCLUDED_WAY_IDS  # never silently empty
    for way_id, reason in streets.FIELD_CHECK_EXCLUDED_WAY_IDS.items():
        assert isinstance(way_id, int)
        assert "field check" in reason


def test_fetch_streets_drops_field_check_excluded_ways(monkeypatch, tmp_path):
    excluded_id = next(iter(streets.FIELD_CHECK_EXCLUDED_WAY_IDS))
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=-73.95, y=40.05)
    main_graph.add_node("m2", x=-73.949, y=40.051)
    main_graph.add_node("m3", x=-73.948, y=40.052)
    main_graph.add_edge("m1", "m2", osmid=42)
    main_graph.add_edge("m2", "m3", osmid=excluded_id)

    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph))
    result = streets.fetch_streets(BBOX, "test-field-check-excluded-tile")

    assert "m3" not in result.nodes  # stranded by the drop
    assert nx.has_path(result.to_undirected(), "m1", "m2")


def test_foot_and_access_clauses_are_anchored_in_every_filter():
    # Overpass regexes are UNANCHORED substring matches. An unanchored
    # exclusion like ["access"!~"private|no"] therefore also excludes
    # access=unknown -- "unknown" contains "no" -- which silently deleted
    # 130 real walkable ways citywide (Calvary Cemetery's whole internal
    # lane grid, a Bronx secondary street; found via a 1.35x routing
    # detour vs OSRM/Valhalla, 2026-08-19). Inclusions have the mirror
    # problem: foot~"designated|yes" would admit foot="designated;no".
    # v19 anchored CYCLEWAY_FILTER's foot clause for exactly this; this
    # test makes the rule stick for EVERY filter's foot/access clause so
    # a future edit can't quietly reintroduce the class. Census
    # 2026-08-19: "unknown" is the only real NYC value that collides, and
    # no semicolon multi-values exist -- re-checked per refetch
    # (REFETCH.md ritual).
    filters = {
        "WALK_FILTER": streets.WALK_FILTER,
        "CYCLEWAY_FILTER": streets.CYCLEWAY_FILTER,
        "FOOT_OVERRIDES_ACCESS_FILTER": streets.FOOT_OVERRIDES_ACCESS_FILTER,
        "NAMED_SIDEWALK_FILTER": streets.NAMED_SIDEWALK_FILTER,
        "ANY_SIDEWALK_FILTER": streets.ANY_SIDEWALK_FILTER,
        "PARKING_AISLE_FILTER": streets.PARKING_AISLE_FILTER,
        "FOOT_FORBIDDEN_FILTER": streets.FOOT_FORBIDDEN_FILTER,
        "ACCESS_FORBIDDEN_FILTER": streets.ACCESS_FORBIDDEN_FILTER,
    }
    found_any = False
    for name, filt in filters.items():
        for key, op, pattern in re.findall(r'\["(foot|access)"(!?~)"([^"]+)"\]', filt):
            found_any = True
            assert pattern.startswith("^") and pattern.endswith("$"), (
                f"{name}'s [{key}{op}\"{pattern}\"] clause is unanchored -- "
                f"Overpass matches substrings, so e.g. {key}=unknown would "
                f"{'wrongly be excluded' if op == '!~' else 'wrongly match'} "
                f"(\"unknown\" contains \"no\")"
            )
    assert found_any  # the scan itself must keep finding clauses


def test_cycleway_filter_requires_an_explicit_foot_allowance():
    # Scoped on purpose: broadening this to admit every cycleway (not
    # just explicitly foot-allowed ones) would start routing pedestrians
    # down ordinary bike-only lanes. v19 widened designated ->
    # designated|yes (anchored, so foot=no/foot=designated;no etc. can't
    # sneak through a substring match): ~95km of real named greenways
    # citywide carry foot=yes, measured 2026-08-14 (Cunningham Park
    # Greenway was the found case, FIXES item 1).
    assert '"highway"="cycleway"' in streets.CYCLEWAY_FILTER
    assert '"foot"~"^(designated|yes)$"' in streets.CYCLEWAY_FILTER


def test_foot_overrides_access_filter_requires_an_explicit_foot_override():
    # Scoped tightly on purpose: this should only admit ways where foot
    # access is explicitly designated/yes despite a general access
    # restriction -- not every access=no/private way, which would
    # reintroduce genuinely gated/private ways WALK_FILTER's own clause
    # exists to keep out.
    assert '"foot"~"^(designated|yes)$"' in streets.FOOT_OVERRIDES_ACCESS_FILTER
    assert '"access"~"^(private|no)$"' in streets.FOOT_OVERRIDES_ACCESS_FILTER


def test_foot_overrides_access_filter_excludes_golf_ways():
    # Confirmed real citywide (FIXES.md item 1e's follow-up, 2026-08-09):
    # of 222 ways this filter matches citywide, 29 carry a "golf" tag
    # (golf=path/cartpath) -- real golf-course cart paths, where
    # foot=designated/yes marks the walking lane of the path (as opposed
    # to the cart lane), not "the general public may enter." One of these
    # (a Marine Park golf cart path) was already confirmed live in
    # data/tiles/r7c14.json.gz -- an access=private golf course path
    # routable in production. The other 193 matches (the original
    # Queensboro Bridge case, Columbia's College Walk, Fulton Mall, gated
    # communities that block cars but explicitly admit pedestrians) don't
    # carry a golf tag and should stay admitted.
    assert '"golf"!~"."' in streets.FOOT_OVERRIDES_ACCESS_FILTER


def test_foot_overrides_access_filter_admits_track():
    # One real case found during the highway=track survey (FIXES.md item
    # 1f, 2026-08-09): way 1240381845 (40.63868,-73.87592) is tagged
    # access=no + foot=yes + horse=yes -- the exact Queensboro Bridge
    # pattern this filter exists for -- but fell through untouched
    # because track wasn't in this filter's own highway allowlist,
    # even after being added to WALK_FILTER's.
    highway_clause = re.search(r'\["highway"~"([^"]+)"\]', streets.FOOT_OVERRIDES_ACCESS_FILTER)
    assert highway_clause is not None
    assert "track" in highway_clause.group(1).split("|")


def test_named_sidewalk_filter_requires_both_sidewalk_and_a_name():
    # Scoped tightly on purpose: this should only admit footway=sidewalk
    # ways that ALSO have a real name -- not every sidewalk, which would
    # reintroduce the thousands of unnamed sidewalk fragments
    # WALK_FILTER's own footway!=sidewalk clause exists to keep out.
    assert '"footway"="sidewalk"' in streets.NAMED_SIDEWALK_FILTER
    assert '["name"]' in streets.NAMED_SIDEWALK_FILTER


def test_parking_aisle_filter_matches_service_parking_aisle():
    assert '"service"="parking_aisle"' in streets.PARKING_AISLE_FILTER


def test_parking_aisle_filter_does_not_exclude_by_access():
    # Deliberately broader than WALK_FILTER's own access clause: checked
    # directly against 8 real, confirmed through-path lots citywide
    # (2026-08-08) -- 6 of 8 carry access=private/customers on at least
    # some aisles, one (a Home Depot) on every single aisle way, despite
    # being an obvious, heavily-used public shortcut. Gating on access here
    # would exclude most of the real cases this filter exists to find --
    # _through_path_parking_aisles()'s own connectivity check is what
    # decides real vs. dead-end, not this tag.
    assert "access" not in streets.PARKING_AISLE_FILTER


def test_parking_aisle_filter_excludes_building_passages():
    # tunnel=building_passage means the aisle runs through or under a
    # building -- a private indoor/underground garage lane, not an open
    # lot. Real and citywide (38 ways, confirmed 2026-08-08 near Times
    # Square: a private, motor_vehicle-only lane tunneling under a hotel),
    # but categorically different from the open-air through-paths this
    # filter exists to find -- a building's internal driveway isn't a
    # public-feeling shortcut the way a Home Depot parking lot is,
    # regardless of how many street connections it has.
    assert '"tunnel"!~"building_passage"' in streets.PARKING_AISLE_FILTER
    # Still excludes explicitly foot-prohibited aisles -- a much more
    # direct pedestrian-specific signal than access=private/customers.
    assert '"foot"!~"^no$"' in streets.PARKING_AISLE_FILTER


def _cache_roundtrip(monkeypatch, tmp_path, cached_graph, recorded_bbox_signature,
                     refresh_raw=False):
    """Put a graph in the cache with a given recorded fetch_bbox, then call
    fetch_streets and report whether it re-fetched. Uses osmnx's real
    save/load so the attribute genuinely survives a GraphML round trip
    rather than being asserted against a mock's in-memory dict."""
    monkeypatch.setattr(streets, "STREETS_DIR", tmp_path)
    monkeypatch.setattr(streets, "RAW_STREETS_DIR", tmp_path / "streets_raw")
    monkeypatch.setattr(streets.time, "sleep", lambda seconds: None)
    _isolate_citywide_layers(monkeypatch, tmp_path)
    # These cache-behavior tests are about fetch_streets's OWN graph cache,
    # not the imported layers -- but fetch_streets still calls the two direct
    # HTTP fetches _by_filter doesn't intercept: interior_sidewalks (ArcGIS)
    # and park_trails (NYC Open Data). Locally a warm cache hides them; in CI
    # (cold cache) they go live, and a transient park-trails 500 failed the
    # whole suite. Mock both to empty so these tests never touch the network.
    monkeypatch.setattr(
        interior_sidewalks, "fetch_interior_sidewalks",
        lambda **kwargs: {"type": "FeatureCollection", "features": []},
    )
    monkeypatch.setattr(
        park_trails, "fetch_park_trails",
        lambda **kwargs: {"type": "FeatureCollection", "features": []},
    )

    if recorded_bbox_signature is not None:
        cached_graph.graph["fetch_bbox"] = recorded_bbox_signature
    cached_graph.graph["crs"] = "epsg:4326"
    # Must match fetch_streets()'s own filename exactly, including the
    # park-reach variant suffix -- this helper calls it without a
    # park_reach, so the cache it writes has to carry that suffix too.
    # Getting this wrong makes the "must re-fetch" tests below pass for the
    # wrong reason (file never found at all, guard never consulted).
    path = tmp_path / f"cachetile_v{streets.GRAPH_CACHE_VERSION}_noparkreach.graphml"
    streets.ox.save_graphml(cached_graph, path)

    fetched = {"count": 0}

    def record_fetch(**kwargs):
        fetched["count"] += 1
        # crs like a real osmnx graph: the raw-cache layer round-trips this
        # through the real save/load before processing sees it.
        fresh = nx.MultiDiGraph(crs="epsg:4326")
        fresh.add_node(2222, x=0.0, y=0.0)  # int ids: osmnx casts them on load
        return fresh

    monkeypatch.setattr(streets.ox, "graph_from_bbox", _by_filter(record_fetch))
    result = streets.fetch_streets(BBOX, "cachetile", refresh_raw=refresh_raw)
    return result, fetched["count"]


def _cached_graph():
    graph = nx.MultiDiGraph()
    # Integer node id on purpose: osmnx's load_graphml casts ids to int
    # (real OSM node ids are integers), so a string id here fails to load
    # and every cache test would pass for the wrong reason.
    graph.add_node(1111, x=0.0, y=0.0)
    return graph


def test_fetch_streets_reuses_a_cache_whose_recorded_bbox_matches(monkeypatch, tmp_path):
    result, fetches = _cache_roundtrip(
        monkeypatch, tmp_path, _cached_graph(), streets._bbox_signature(BBOX)
    )
    assert fetches == 0
    assert 1111 in result.nodes


def test_fetch_streets_refetches_a_cache_recorded_for_a_different_bbox(monkeypatch, tmp_path):
    # The real failure: commit a461e36 shifted CITY_BBOX.lat_min by exactly
    # one tile row, so r17c14's cache kept its filename while its contents
    # became the tile one row north (South Bronx streets served as Astoria
    # for twelve days). Same shift reproduced here.
    shifted = Bbox(
        lat_min=BBOX.lat_min + config.TILE_SIZE_LAT_DEG,
        lat_max=BBOX.lat_max + config.TILE_SIZE_LAT_DEG,
        lon_min=BBOX.lon_min,
        lon_max=BBOX.lon_max,
    )
    result, fetches = _cache_roundtrip(
        monkeypatch, tmp_path, _cached_graph(), streets._bbox_signature(shifted)
    )
    assert fetches > 0, "a cache for a different area must not be reused"
    assert 1111 not in result.nodes


def test_fetch_streets_refetches_a_cache_with_no_recorded_bbox(monkeypatch, tmp_path):
    # Written before this check existed, so nothing ever verified the area
    # it covers -- unverifiable has to mean re-fetch, or the guard is
    # trivially bypassed by every pre-existing file.
    result, fetches = _cache_roundtrip(monkeypatch, tmp_path, _cached_graph(), None)
    assert fetches > 0
    assert 1111 not in result.nodes


def test_fetch_streets_records_the_fetch_bbox_it_used(monkeypatch, tmp_path):
    # Without this the guard above can never fire on a freshly written file
    # -- and since FIXES item 11 there are TWO freshly written files, the
    # raw snapshot and the processed graph, each with its own load-time
    # bbox guard, so both must record it.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=0.0, y=0.0)
    saved = _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph))

    streets.fetch_streets(BBOX, "test-records-bbox-tile")

    recorded = [graph.graph.get("fetch_bbox") for graph in saved.values()]
    processed_and_raw = [
        graph.graph.get("fetch_bbox") for path, graph in saved.items()
        if "test-records-bbox-tile" in path
    ]
    assert len(processed_and_raw) == 2, "expected one raw snapshot + one processed graph"
    assert all(sig == streets._bbox_signature(BBOX) for sig in processed_and_raw), recorded


def test_any_sidewalk_filter_matches_sidewalks_with_or_without_a_name():
    # The deliberate counterpart to the test above: this filter drops the
    # name requirement (a park's entrance paths are as often unnamed as
    # named in OSM), which is only safe because _park_reach_sidewalks()
    # narrows its result to park reach before anything is composed in.
    assert '"footway"="sidewalk"' in streets.ANY_SIDEWALK_FILTER
    assert '["name"]' not in streets.ANY_SIDEWALK_FILTER
    # Every other clause stays identical to the named variant -- this is
    # that filter minus one bracket, not a separately-drifting copy.
    assert streets.ANY_SIDEWALK_FILTER == streets.NAMED_SIDEWALK_FILTER.replace('["name"]', '')


def test_fetch_streets_keeps_park_reach_sidewalks_and_drops_the_rest(monkeypatch, tmp_path):
    # The core of the fix. ANY_SIDEWALK_FILTER matches every walkable
    # footway=sidewalk way, the vast majority of which are the unnamed
    # duplicates of a street centerline we already have -- only the ones
    # reaching a real park may be composed in.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=-73.95, y=40.05)

    sidewalks = nx.MultiDiGraph()
    # In park reach: a park-entrance path, the case this exists for.
    sidewalks.add_node("in1", x=-73.9500, y=40.0500)
    sidewalks.add_node("in2", x=-73.9501, y=40.0501)
    sidewalks.add_edge("in1", "in2")
    # Several km away: an ordinary street's sidewalk, must be dropped.
    sidewalks.add_node("out1", x=-73.9900, y=40.0900)
    sidewalks.add_node("out2", x=-73.9901, y=40.0901)
    sidewalks.add_edge("out1", "out2")

    _mock_fetch(
        monkeypatch, tmp_path,
        _by_filter(lambda **kwargs: main_graph, any_sidewalk_fn=lambda **kwargs: sidewalks),
    )
    result = streets.fetch_streets(
        BBOX, "test-park-reach-tile", park_reach=_park_reach_around(-73.95, 40.05)
    )

    assert {"in1", "in2"} <= set(result.nodes)
    assert not {"out1", "out2"} & set(result.nodes)


def test_fetch_streets_survives_a_tile_where_no_sidewalk_reaches_a_park(monkeypatch, tmp_path):
    # Not an edge case -- this is most of the city. Any tile with no park
    # inside PARK_REACH_BUFFER_M drops every segment the sidewalk query
    # returned, and _park_reach_sidewalks() hands back None rather than an
    # empty graph (composing an edgeless graph in would contribute stray
    # nodes with no edges, the same shape as the Bronx r22c19 case
    # run_tile.py already guards against). Untested, this would have
    # crashed on the first park-free tile of a 276-tile citywide run.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=-73.95, y=40.05)

    sidewalks = nx.MultiDiGraph()
    sidewalks.add_node("far1", x=-73.9900, y=40.0900)
    sidewalks.add_node("far2", x=-73.9901, y=40.0901)
    sidewalks.add_edge("far1", "far2")

    _mock_fetch(
        monkeypatch, tmp_path,
        _by_filter(lambda **kwargs: main_graph, any_sidewalk_fn=lambda **kwargs: sidewalks),
    )
    result = streets.fetch_streets(
        BBOX, "test-parkless-tile", park_reach=_park_reach_around(-73.95, 40.05)
    )

    assert list(result.nodes) == ["m1"]


def test_fetch_streets_narrows_sidewalks_before_simplifying_not_after(monkeypatch, tmp_path):
    # Load-bearing ordering, measured: filtering before the single simplify
    # pass costs +0% edges in a park-free area (output byte-identical to not
    # running the query at all), while filtering afterwards costs +14% --
    # sidewalk ways present at simplify time preserve intersection nodes
    # that would otherwise collapse, and deleting their edges later strands
    # those nodes in the graph forever. So the out-of-reach sidewalk must
    # never reach simplify_graph() at all, not merely be absent from the
    # final result.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=-73.95, y=40.05)

    sidewalks = nx.MultiDiGraph()
    sidewalks.add_node("in1", x=-73.9500, y=40.0500)
    sidewalks.add_node("in2", x=-73.9501, y=40.0501)
    sidewalks.add_edge("in1", "in2")
    sidewalks.add_node("out1", x=-73.9900, y=40.0900)
    sidewalks.add_node("out2", x=-73.9901, y=40.0901)
    sidewalks.add_edge("out1", "out2")

    seen = []

    def fake_simplify(graph, **kwargs):
        seen.append(set(graph.nodes))
        return graph

    monkeypatch.setattr(streets.ox.simplification, "simplify_graph", fake_simplify)
    _mock_fetch(
        monkeypatch, tmp_path,
        _by_filter(lambda **kwargs: main_graph, any_sidewalk_fn=lambda **kwargs: sidewalks),
    )
    streets.fetch_streets(
        BBOX, "test-prefilter-tile", park_reach=_park_reach_around(-73.95, 40.05)
    )

    assert len(seen) == 1
    assert not {"out1", "out2"} & seen[0]


def test_fetch_streets_skips_the_sidewalk_query_without_a_park_reach_shape(monkeypatch, tmp_path):
    # No park data available (the pilot tile, CI) must still produce a real
    # graph -- the query is skipped entirely rather than run unfiltered,
    # which would re-admit every duplicate street sidewalk citywide.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=-73.95, y=40.05)

    asked = []

    def record_sidewalk_query(**kwargs):
        asked.append(kwargs.get("custom_filter"))
        raise AssertionError("ANY_SIDEWALK_FILTER must not be queried without park reach")

    _mock_fetch(
        monkeypatch, tmp_path,
        _by_filter(lambda **kwargs: main_graph, any_sidewalk_fn=record_sidewalk_query),
    )
    result = streets.fetch_streets(BBOX, "test-no-park-reach-tile")

    assert list(result.nodes) == ["m1"]
    assert asked == []


def test_fetch_streets_caches_park_reach_and_plain_results_under_different_names(monkeypatch, tmp_path):
    # The two results genuinely differ, so they must never share a cache
    # entry: a tile fetched once without park data (pilot/CI) would
    # otherwise be reused forever afterwards as if it had the park-reach
    # sidewalks in it, silently un-fixing the bug on any machine that
    # happened to run it that way first.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=-73.95, y=40.05)

    saved = _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph))

    streets.fetch_streets(BBOX, "test-cache-variant-tile", park_reach=_park_reach_around(-73.95, 40.05))
    streets.fetch_streets(BBOX, "test-cache-variant-tile")

    # save_graphml also fires for the citywide layer caches (item 6b) and
    # the raw WALK_FILTER snapshot (item 11, variant-INdependent by design:
    # park_reach only changes processing, not what Overpass returned) --
    # only the two PROCESSED tile cache writes are under test here.
    saved_tiles = [
        path for path in saved
        if "test-cache-variant-tile" in path and "streets_raw" not in path
    ]
    assert len(saved_tiles) == 2
    assert saved_tiles[0] != saved_tiles[1]


# FIXES.md item 1b: parking_aisle ways are excluded from WALK_FILTER
# entirely (real duplicates/dead-ends are the common case), but some form
# a real through-path across a large lot -- confirmed citywide (646
# substantial lots) and validated directly against 8 real examples
# (2026-08-08): Lowe's Gowanus, several Staten Island big-box stores, and
# Aviator Sports/Floyd Bennett Field all connect to the surrounding street
# network at 2+ distinct points. _through_path_parking_aisles() is the
# connectivity check that tells a real through-path apart from a dead-end
# spur into a single row of parking spaces -- a graph-topology question,
# not a tag lookup, unlike 1a/1d.

def test_through_path_parking_aisles_keeps_a_cluster_touching_the_street_twice():
    graph = nx.MultiDiGraph()
    graph.add_node("street_a")
    graph.add_node("street_b")

    aisles = nx.MultiDiGraph()
    aisles.add_edge("street_a", "mid")
    aisles.add_edge("mid", "street_b")

    result = streets._through_path_parking_aisles(graph, aisles, "test-tile")

    assert set(result.edges()) == {("street_a", "mid"), ("mid", "street_b")}


def test_through_path_parking_aisles_drops_a_dead_end_cluster():
    # Only one attachment point (street_a) -- street_a to a "dead_end"
    # node with no other exit is a spur, not a shortcut.
    graph = nx.MultiDiGraph()
    graph.add_node("street_a")

    aisles = nx.MultiDiGraph()
    aisles.add_edge("street_a", "mid")
    aisles.add_edge("mid", "dead_end")

    assert streets._through_path_parking_aisles(graph, aisles, "test-tile") is None


def test_through_path_parking_aisles_drops_a_cluster_with_no_street_connection():
    graph = nx.MultiDiGraph()
    graph.add_node("street_a")

    aisles = nx.MultiDiGraph()
    aisles.add_edge("isolated_1", "isolated_2")

    assert streets._through_path_parking_aisles(graph, aisles, "test-tile") is None


def test_through_path_parking_aisles_evaluates_each_cluster_independently():
    # Two unrelated lots in the same fetched aisle graph: one a real
    # through-path (street_a <-> street_b), the other a dead-end off
    # street_c. Only the real one's edges should survive.
    graph = nx.MultiDiGraph()
    graph.add_node("street_a")
    graph.add_node("street_b")
    graph.add_node("street_c")

    aisles = nx.MultiDiGraph()
    aisles.add_edge("street_a", "mid1")
    aisles.add_edge("mid1", "street_b")
    aisles.add_edge("street_c", "mid2")
    aisles.add_edge("mid2", "dead_end")

    result = streets._through_path_parking_aisles(graph, aisles, "test-tile")

    assert set(result.edges()) == {("street_a", "mid1"), ("mid1", "street_b")}
    assert "dead_end" not in result.nodes


def test_through_path_parking_aisles_returns_none_when_given_none():
    graph = nx.MultiDiGraph()
    assert streets._through_path_parking_aisles(graph, None, "test-tile") is None


def test_fetch_streets_unions_in_through_path_parking_aisles(monkeypatch, tmp_path):
    # street_a and street_b are deliberately NOT connected within the main
    # street graph itself -- each sits on its own separate little edge, so
    # any path between them can only come from the aisle union. "mid" is a
    # plain degree-2 chain node that simplify_graph() correctly collapses
    # into a single street_a<->street_b edge, same as it would for any real
    # intermediate OSM node -- hence checking connectivity, not that
    # specific node, survived.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("street_a", x=-73.95, y=40.05)
    main_graph.add_node("other_a", x=-73.949, y=40.049)
    main_graph.add_edge("street_a", "other_a")
    main_graph.add_node("street_b", x=-73.951, y=40.051)
    main_graph.add_node("other_b", x=-73.952, y=40.052)
    main_graph.add_edge("street_b", "other_b")

    aisles = nx.MultiDiGraph()
    aisles.add_node("street_a", x=-73.95, y=40.05)
    aisles.add_node("mid", x=-73.9505, y=40.0505)
    aisles.add_node("street_b", x=-73.951, y=40.051)
    aisles.add_edge("street_a", "mid")
    aisles.add_edge("mid", "street_b")

    _mock_fetch(
        monkeypatch, tmp_path,
        _by_filter(lambda **kwargs: main_graph, parking_aisle_fn=lambda **kwargs: aisles),
    )
    result = streets.fetch_streets(BBOX, "test-aisle-tile")

    # street_a/mid/street_b are themselves plain degree-2 pass-through
    # points once composed (each with exactly one neighbor on either
    # side), so simplify_graph() collapses the whole chain into a single
    # edge and none of them necessarily survive as nodes -- other_a and
    # other_b are the real endpoints (degree 1), guaranteed to survive,
    # and connectivity between them can only exist via the aisle path.
    assert nx.has_path(result.to_undirected(), "other_a", "other_b")


def test_fetch_streets_works_with_no_parking_aisles_in_the_area(monkeypatch, tmp_path):
    # Most tiles have none -- the common real case, same as every other
    # narrower query.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=-73.95, y=40.05)

    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph))
    result = streets.fetch_streets(BBOX, "test-no-aisle-tile")

    assert list(result.nodes) == ["m1"]


def test_fetch_streets_drops_dead_end_parking_aisles(monkeypatch, tmp_path):
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("street_a", x=-73.95, y=40.05)

    aisles = nx.MultiDiGraph()
    aisles.add_node("street_a", x=-73.95, y=40.05)
    aisles.add_node("dead_end", x=-73.9505, y=40.0505)
    aisles.add_edge("street_a", "dead_end")

    _mock_fetch(
        monkeypatch, tmp_path,
        _by_filter(lambda **kwargs: main_graph, parking_aisle_fn=lambda **kwargs: aisles),
    )
    result = streets.fetch_streets(BBOX, "test-dead-end-aisle-tile")

    assert "dead_end" not in result.nodes


# FIXES.md item 1a: NYC's Interior Sidewalk Centerline data (real off-ROW
# walking paths in parks, NYCHA, hospital/school campuses, and ordinary
# residential complexes) arrives as independently-digitized line segments
# from an ArcGIS source, not OSM ways -- so there's no shared node id to
# rely on the way the other five filters above get for free. Three
# functions handle this: _interior_sidewalks_for_tile() narrows the
# citywide cache to one tile, _build_interior_sidewalk_graph() stitches
# that tile's segments into one shape (merging endpoints that coincide
# within INTERIOR_SIDEWALK_MERGE_TOLERANCE_M, since two segments meeting
# at the same real point don't necessarily share a coordinate exactly),
# and _snap_interior_sidewalks() connects the result's loose ends onto the
# real street network.
#
# The snap distance (INTERIOR_SIDEWALK_SNAP_MAX_M = 5.0) and the decision
# to check for a real barrier (fence/wall/hedge) crossing the connection
# were both settled against real data, not guessed: median real distance
# from a loose end to the nearest street is 0.6m, 95% are within 5m, and
# the two datasets are independently digitized enough that snapping to
# the nearest existing NODE instead of the nearest EDGE would misplace
# 63% of real connections by >3m (confirmed live, Holmes Towers, NYCHA,
# Manhattan). The barrier check is real but known-incomplete -- OSM's
# fence/wall tagging is crowdsourced and nowhere near complete, and no
# professionally-surveyed dataset covers ordinary fencing (checked; only
# a narrow "Retaining Wall" layer exists) -- so 5m was chosen specifically
# to keep the unverifiable "invisible fence" risk small, not to maximize
# how many real connections get captured.

def _segment_feature(coords):
    return {"type": "Feature", "properties": {}, "geometry": {"type": "LineString", "coordinates": coords}}


def test_interior_sidewalks_for_tile_keeps_segments_inside_the_bbox():
    geojson = {"type": "FeatureCollection", "features": [
        _segment_feature([[-73.95, 40.05], [-73.949, 40.051]]),
    ]}

    result = streets._interior_sidewalks_for_tile(geojson, BBOX)

    assert result == [[(-73.95, 40.05), (-73.949, 40.051)]]


def test_interior_sidewalks_for_tile_drops_segments_outside_the_bbox():
    geojson = {"type": "FeatureCollection", "features": [
        _segment_feature([[10.0, 10.0], [10.001, 10.001]]),  # nowhere near BBOX
    ]}

    assert streets._interior_sidewalks_for_tile(geojson, BBOX) == []


def test_interior_sidewalks_for_tile_keeps_a_segment_crossing_the_boundary():
    # One end outside BBOX (lon_max=-73.9), one end inside -- a real
    # segment straddling a tile edge should still count as belonging here,
    # same reasoning WALK_FILTER's own tile fetches rely on.
    geojson = {"type": "FeatureCollection", "features": [
        _segment_feature([[-73.95, 40.05], [-73.8, 40.05]]),
    ]}

    assert streets._interior_sidewalks_for_tile(geojson, BBOX) != []


# ── the foot-forbidden clip (v23, FIXES.md item 1) ──────────────────────────
# The planimetric survey has no access attributes, so it re-traced NYC's
# bike-only/private paths as walkable geometry. The real case: the
# Manhattan Bridge Bike Path (foot=no), imported whole and welded at only
# its Manhattan end, turned a 100m walk in DUMBO into a 3.6km route over
# the bridge and back. These test the clip's three behaviors -- remove
# co-running duplicates, protect crossings, drop slivers -- on synthetic
# geometry built through the same metric projection the real code uses.

_M_PER_DEG_LON = 85_300.0  # ~meters per degree longitude at 40deg N
_CLIP_LAT = 40.05
_CLIP_LON = -73.95


def _lon_at(meters_east):
    return _CLIP_LON + meters_east / _M_PER_DEG_LON


def _east_west_line(start_m, end_m, lat=_CLIP_LAT):
    return LineString([(_lon_at(start_m), lat), (_lon_at(end_m), lat)])


def _forbidden_index_for(lines_lonlat):
    """A clip index of the same (STRtree, buffers) shape
    streets._foot_forbidden_index() builds from the citywide layer,
    without any fetch."""
    buffers = []
    for line in lines_lonlat:
        line_m = shapely_transform(streets._TO_METRIC_CRS, line)
        buffers.append(line_m.buffer(streets.FOOT_FORBIDDEN_BUFFER_M))
    return STRtree(buffers), buffers


def _length_m(line_lonlat):
    return shapely_transform(streets._TO_METRIC_CRS, line_lonlat).length


def test_clip_foot_forbidden_removes_a_co_running_duplicate():
    # 200m imported segment lying directly on a forbidden path -- the
    # Manhattan Bridge bikeway shape. Nothing survives.
    imported = _east_west_line(0, 200)
    index = _forbidden_index_for([_east_west_line(0, 200)])

    assert streets._clip_foot_forbidden(imported, index) == []


def test_clip_foot_forbidden_keeps_a_perpendicular_crossing_whole():
    # A real path CROSSING a forbidden one is inside the 6m buffer for
    # ~12m -- far under FOOT_FORBIDDEN_MIN_OVERLAP_M -- and must come back
    # as ONE continuous line, not two pieces broken at the buffer.
    imported = _east_west_line(0, 200)
    crossing_lon = _lon_at(100)
    forbidden = LineString([(crossing_lon, _CLIP_LAT - 0.001), (crossing_lon, _CLIP_LAT + 0.001)])
    index = _forbidden_index_for([forbidden])

    result = streets._clip_foot_forbidden(imported, index)

    assert len(result) == 1
    assert _length_m(result[0]) == pytest.approx(200, abs=1)


def test_clip_foot_forbidden_keeps_the_walkable_tail_of_a_partial_duplicate():
    # First 100m duplicates a forbidden path, the rest is a real walkable
    # tail -- the tail survives (a whole-feature drop would lose it).
    imported = _east_west_line(0, 200)
    index = _forbidden_index_for([_east_west_line(0, 100)])

    result = streets._clip_foot_forbidden(imported, index)

    assert len(result) == 1
    # The buffer's round cap eats ~6m past the forbidden way's end, so
    # the tail is ~94m, anchored at the far (200m) end.
    assert _length_m(result[0]) == pytest.approx(94, abs=2)
    assert max(lon for lon, lat in result[0].coords) == pytest.approx(_lon_at(200), abs=1e-6)


def test_clip_foot_forbidden_drops_a_boundary_sliver():
    # Forbidden coverage reaches 185m of a 200m segment; with the buffer
    # cap extending ~6m further, the leftover is ~9m -- under
    # FOOT_FORBIDDEN_MIN_REMNANT_M, a sliver, not a path.
    imported = _east_west_line(0, 200)
    index = _forbidden_index_for([_east_west_line(0, 185)])

    assert streets._clip_foot_forbidden(imported, index) == []


def test_clip_foot_forbidden_without_a_layer_is_a_no_op():
    imported = _east_west_line(0, 200)

    assert streets._clip_foot_forbidden(imported, None) == [imported]


def test_clip_foot_forbidden_removes_an_endpoint_stub():
    # v24 endpoint bar: the feature's first ~21m (15m of forbidden way +
    # the 6m buffer cap) co-runs a forbidden path and TOUCHES the
    # feature's start -- under the 25m mid-feature bar, but a stretch
    # ending at the feature boundary is a chain continuation/terminal
    # stub, not a crossing. Removed; the walkable tail survives.
    imported = _east_west_line(0, 200)
    index = _forbidden_index_for([_east_west_line(0, 15)])

    result = streets._clip_foot_forbidden(imported, index)

    assert len(result) == 1
    assert _length_m(result[0]) == pytest.approx(179, abs=2)


def test_clip_foot_forbidden_removes_a_whole_short_chain_member():
    # A 20m feature lying entirely on a forbidden path -- the exact shape
    # that evaded v23's per-feature 25m bar when the survey chopped one
    # duplicate into short adjacent features. Both endpoints touch, so
    # the endpoint bar takes it whole.
    imported = _east_west_line(0, 20)
    index = _forbidden_index_for([_east_west_line(-50, 70)])

    assert streets._clip_foot_forbidden(imported, index) == []


def test_clip_foot_forbidden_keeps_a_t_junction_nub():
    # A path ending perpendicular AT a forbidden way is inside the buffer
    # for only ~6m -- under the 12m endpoint bar. The junction nub (and
    # the whole feature) must survive, or every path T-ending at a
    # bikeway would lose its connection point.
    imported = _east_west_line(0, 200)
    end_lon = _lon_at(200)
    forbidden = LineString([(end_lon, _CLIP_LAT - 0.001), (end_lon, _CLIP_LAT + 0.001)])
    index = _forbidden_index_for([forbidden])

    result = streets._clip_foot_forbidden(imported, index)

    assert len(result) == 1
    assert _length_m(result[0]) == pytest.approx(200, abs=1)


def test_interior_sidewalks_for_tile_applies_the_foot_forbidden_clip():
    # Wiring check: a duplicate feature disappears, a crossing feature
    # survives untouched, through the real _interior_sidewalks_for_tile.
    duplicate = [[_lon_at(0), _CLIP_LAT], [_lon_at(200), _CLIP_LAT]]
    crossing_lon = _lon_at(500)
    crossing = [[crossing_lon, _CLIP_LAT - 0.001], [crossing_lon, _CLIP_LAT + 0.001]]
    geojson = {"type": "FeatureCollection", "features": [
        _segment_feature(duplicate), _segment_feature(crossing),
    ]}
    index = _forbidden_index_for([
        _east_west_line(0, 200),
        _east_west_line(450, 550),  # crosses the second feature at 90deg
    ])

    result = streets._interior_sidewalks_for_tile(geojson, BBOX, index, "test-tile")

    assert result == [[(crossing[0][0], crossing[0][1]), (crossing[1][0], crossing[1][1])]]


def test_build_interior_sidewalk_graph_returns_none_for_no_segments():
    assert streets._build_interior_sidewalk_graph([], "test-tile") is None


def test_build_interior_sidewalk_graph_merges_exactly_touching_endpoints():
    segments = [
        [(-73.95, 40.05), (-73.949, 40.05)],
        [(-73.949, 40.05), (-73.948, 40.05)],  # shares an exact coordinate
    ]

    result = streets._build_interior_sidewalk_graph(segments, "test-tile")

    assert result.number_of_nodes() == 3  # not 4 -- the shared point is one node
    assert nx.is_connected(result.to_undirected())


def test_build_interior_sidewalk_graph_merges_endpoints_within_tolerance():
    # ~0.3m apart (well under INTERIOR_SIDEWALK_MERGE_TOLERANCE_M=1.0) --
    # independently-digitized segments meeting at the same real point
    # essentially never share an exact coordinate.
    segments = [
        [(-73.95, 40.05), (-73.949, 40.05)],
        [(-73.949, 40.0500027), (-73.948, 40.05)],
    ]

    result = streets._build_interior_sidewalk_graph(segments, "test-tile")

    assert result.number_of_nodes() == 3
    assert nx.is_connected(result.to_undirected())


def test_build_interior_sidewalk_graph_does_not_merge_endpoints_beyond_tolerance():
    # ~5m apart -- clearly beyond the 1m tolerance, so these are two real,
    # separate loose ends, not one shared point.
    segments = [
        [(-73.95, 40.05), (-73.949, 40.05)],
        [(-73.949, 40.05005), (-73.948, 40.05)],
    ]

    result = streets._build_interior_sidewalk_graph(segments, "test-tile")

    assert result.number_of_nodes() == 4
    assert not nx.is_connected(result.to_undirected())


def test_build_interior_sidewalk_graph_gives_every_edge_a_synthetic_osmid():
    # Real bug (2026-08-09): these edges came from ArcGIS data, not OSM, so
    # they never had an osmid at all -- osmnx's own to_undirected()
    # (centerline.build_edge_table, called later in the real pipeline)
    # requires one on every edge, and crashed with a bare KeyError the
    # first time this ran end-to-end on a real tile (Marine Park, r7c14).
    # Distinct per edge, and negative -- real OSM ids are always large
    # positive ints, confirmed against real cached tiles -- so nothing
    # here can collide with a real osmid once composed with the street
    # graph, same reasoning as this function's synthetic node ids.
    segments = [
        [(-73.95, 40.05), (-73.949, 40.05)],
        [(-73.949, 40.05), (-73.948, 40.05)],
    ]

    result = streets._build_interior_sidewalk_graph(segments, "test-tile")

    osmids = [data["osmid"] for _, _, data in result.edges(data=True)]
    assert all(isinstance(osmid, int) and osmid < 0 for osmid in osmids)
    assert len(set(osmids)) == len(osmids)  # each edge gets its own, not shared


def test_build_interior_sidewalk_graph_respects_custom_start_ids():
    # FIXES.md item 1g needs this: once park trails are a second synthetic
    # source composed into the same tile, its ids have to continue from
    # wherever interior sidewalks (or any earlier source) left off, not
    # restart at -1 and collide with an id that already means a different
    # real-world point.
    segments = [[(-73.95, 40.05), (-73.949, 40.05)]]

    result = streets._build_interior_sidewalk_graph(
        segments, "test-tile", start_id=-100, start_edge_osmid=-200,
    )

    assert max(result.nodes) == -100  # the first node gets exactly the given start
    osmids = [data["osmid"] for _, _, data in result.edges(data=True)]
    assert osmids == [-200]


def test_build_interior_sidewalk_graph_respects_a_custom_merge_tolerance():
    # ~5m apart -- beyond the default 1m interior-sidewalk tolerance
    # (see test_build_interior_sidewalk_graph_does_not_merge_endpoints_
    # beyond_tolerance above), but within a wider tolerance a different
    # synthetic source might legitimately need.
    segments = [
        [(-73.95, 40.05), (-73.949, 40.05)],
        [(-73.949, 40.05005), (-73.948, 40.05)],
    ]

    result = streets._build_interior_sidewalk_graph(segments, "test-tile", merge_tolerance_m=10.0)

    assert result.number_of_nodes() == 3
    assert nx.is_connected(result.to_undirected())


def test_uncovered_trail_segments_returns_the_whole_line_when_nothing_covers_it():
    trail = LineString([(0, 0), (100, 0)])
    covered = box(500, 500, 600, 600)  # nowhere near the trail

    result = streets._uncovered_trail_segments(trail, covered, min_length_m=10)

    assert len(result) == 1
    assert result[0].equals(trail)


def test_uncovered_trail_segments_returns_empty_when_fully_covered():
    trail = LineString([(0, 0), (100, 0)])
    covered = box(-10, -10, 110, 10)  # comfortably contains the whole trail

    assert streets._uncovered_trail_segments(trail, covered, min_length_m=10) == []


def test_uncovered_trail_segments_splits_into_multiple_pieces_when_partially_covered():
    # A real, not hypothetical, shape: an 8m buffer against a trail that's
    # only covered in its middle stretch leaves two disconnected leftover
    # pieces, not one -- FIXES.md item 1g's own three-park survey found
    # exactly this pattern (a trail dipping in and out of OSM coverage
    # along its length).
    trail = LineString([(0, 0), (300, 0)])
    covered = box(90, -5, 210, 5)  # covers the middle third only

    result = streets._uncovered_trail_segments(trail, covered, min_length_m=10)

    assert len(result) == 2
    lengths = sorted(piece.length for piece in result)
    assert lengths == pytest.approx([90, 90])


def test_uncovered_trail_segments_drops_pieces_below_the_minimum_length():
    # Same middle-covered shape, but one leftover end is a short 5m sliver
    # (digitization noise -- the buffer's edge doesn't land exactly on a
    # real gap boundary) and the other is a real 90m gap.
    trail = LineString([(0, 0), (300, 0)])
    covered = box(5, -5, 210, 5)  # covers everything except a 5m sliver and a 90m gap

    result = streets._uncovered_trail_segments(trail, covered, min_length_m=10)

    assert len(result) == 1
    assert result[0].length == pytest.approx(90)


def _trail_feature(coords, trail_class="Class IV : Highly Developed"):
    return {"type": "Feature", "properties": {"class": trail_class},
            "geometry": {"type": "LineString", "coordinates": coords}}


def test_park_trails_for_tile_keeps_class_iv_and_v_only():
    geojson = {"type": "FeatureCollection", "features": [
        _trail_feature([[-73.95, 40.05], [-73.949, 40.05]], "Class IV : Highly Developed"),
        _trail_feature([[-73.95, 40.06], [-73.949, 40.06]], "Class V : Fully Developed"),
        _trail_feature([[-73.95, 40.07], [-73.949, 40.07]], "Class III : Developed/Improved"),
    ]}

    result = streets._park_trails_for_tile(geojson, BBOX)

    assert len(result) == 2


def test_park_trails_for_tile_drops_segments_outside_the_bbox():
    geojson = {"type": "FeatureCollection", "features": [
        _trail_feature([[10.0, 10.0], [10.001, 10.001]]),  # nowhere near BBOX
    ]}

    assert streets._park_trails_for_tile(geojson, BBOX) == []


def test_park_trails_for_tile_explodes_a_multilinestring_into_separate_parts():
    # A real shape in the raw data: one row's `shape` can be a
    # MultiLineString with more than one disconnected part.
    geojson = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"class": "Class IV : Highly Developed"},
         "geometry": {"type": "MultiLineString", "coordinates": [
             [[-73.95, 40.05], [-73.949, 40.05]],
             [[-73.94, 40.05], [-73.939, 40.05]],
         ]}},
    ]}

    result = streets._park_trails_for_tile(geojson, BBOX)

    assert len(result) == 2


def _street_graph_with_one_edge():
    graph = nx.MultiDiGraph()
    graph.add_node("s1", x=-73.9500, y=40.0500)
    graph.add_node("s2", x=-73.9490, y=40.0500)
    graph.add_edge("s1", "s2", osmid=555)
    return graph


def _interior_graph_with_one_loose_end(lon, lat):
    interior = nx.MultiDiGraph()
    interior.add_node("loose_end", x=lon, y=lat)
    interior.add_node("anchor", x=lon, y=lat + 0.001)  # far away, not near any street
    interior.add_edge("loose_end", "anchor", osmid=-1)  # as _build_interior_sidewalk_graph would set
    return interior


def test_snap_interior_sidewalks_connects_a_loose_end_within_range():
    # ~3.3m from the s1-s2 edge -- comfortably under the 5m snap distance.
    graph = _street_graph_with_one_edge()
    interior = _interior_graph_with_one_loose_end(-73.9495, 40.05003)

    result = streets._snap_interior_sidewalks(graph, interior, None, "test-tile")

    undirected = result.to_undirected()
    assert nx.has_path(undirected, "loose_end", "s1")
    assert nx.has_path(undirected, "loose_end", "s2")


def test_snap_interior_sidewalks_leaves_a_far_loose_end_disconnected():
    # ~22m from the s1-s2 edge -- well beyond the 5m snap distance, a real
    # digitization gap rather than something to force-connect.
    graph = _street_graph_with_one_edge()
    interior = _interior_graph_with_one_loose_end(-73.9495, 40.05020)

    result = streets._snap_interior_sidewalks(graph, interior, None, "test-tile")

    undirected = result.to_undirected()
    assert not nx.has_path(undirected, "loose_end", "s1")
    assert "loose_end" in result.nodes  # kept, just not connected to the street


def test_snap_interior_sidewalks_skips_a_connection_blocked_by_a_real_barrier():
    # Same geometry as the "connects" case above, but a fence crosses the
    # straight line between the loose end and its snap point -- known
    # incomplete data (OSM's own fence tagging is crowdsourced), but a
    # real, mapped barrier here is real evidence the two points aren't
    # actually walkably connected, distance notwithstanding.
    graph = _street_graph_with_one_edge()
    interior = _interior_graph_with_one_loose_end(-73.9495, 40.05003)

    barriers = nx.MultiDiGraph()
    barriers.add_node("b1", x=-73.9496, y=40.050015)
    barriers.add_node("b2", x=-73.9494, y=40.050015)
    barriers.add_edge("b1", "b2")

    result = streets._snap_interior_sidewalks(graph, interior, barriers, "test-tile")

    assert not nx.has_path(result.to_undirected(), "loose_end", "s1")


def test_snap_interior_sidewalks_avoids_id_collision_with_a_prior_synthetic_source():
    # FIXES.md item 1g: once park trails are snapped in AFTER interior
    # sidewalks (a second call in the same tile), `graph` (the
    # destination) already contains negative synthetic ids from that
    # first pass. This call's own new split-node numbering must not
    # restart at -1 and silently collide with an id that already means a
    # different real-world point once composed -- nx.compose would
    # silently merge the two into one node instead of raising anything.
    graph = _street_graph_with_one_edge()
    graph.add_node(-1, x=-73.9500, y=40.06)  # simulates a node an earlier
                                              # synthetic source already placed
    graph.add_edge("s1", -1, osmid=-1, length=1000.0)

    interior = _interior_graph_with_one_loose_end(-73.9495, 40.05003)

    result = streets._snap_interior_sidewalks(graph, interior, None, "test-tile")

    # The pre-existing node's own identity must survive untouched -- if
    # the new split node collided with it, this would instead read back
    # as wherever the split landed on the s1-s2 edge (~40.05), not 40.06.
    assert result.nodes[-1]["y"] == 40.06


def test_snap_interior_sidewalks_respects_a_custom_snap_max():
    # Same ~22m-away loose end that the default 5m threshold leaves
    # disconnected (see test_snap_interior_sidewalks_leaves_a_far_loose_
    # end_disconnected above) -- park trails need a larger threshold,
    # since a trimmed trail's own endpoint can legitimately sit close to
    # PARK_TRAIL_OVERLAP_BUFFER_M (8m) from the nearest covered edge by
    # construction (see PARK_TRAIL_SNAP_MAX_M's own comment).
    graph = _street_graph_with_one_edge()
    interior = _interior_graph_with_one_loose_end(-73.9495, 40.05020)

    result = streets._snap_interior_sidewalks(graph, interior, None, "test-tile", snap_max_m=25.0)

    undirected = result.to_undirected()
    assert nx.has_path(undirected, "loose_end", "s1")


def test_snap_interior_sidewalks_evaluates_each_loose_end_independently():
    # close and far are two SEPARATE, unconnected little interior paths
    # (not two ends of the same one) -- a single shared edge between them
    # would make this test unsatisfiable by construction: once "close"
    # snaps to the street, "far" would become transitively connected to it
    # regardless of what the snapping logic actually does.
    graph = _street_graph_with_one_edge()
    interior = nx.MultiDiGraph()
    interior.add_node("close", x=-73.9495, y=40.05003)
    interior.add_node("close_anchor", x=-73.9495, y=40.052)
    interior.add_edge("close", "close_anchor")
    interior.add_node("far", x=-73.9495, y=40.05020)
    interior.add_node("far_anchor", x=-73.9495, y=40.052)
    interior.add_edge("far", "far_anchor")

    result = streets._snap_interior_sidewalks(graph, interior, None, "test-tile")

    undirected = result.to_undirected()
    assert nx.has_path(undirected, "close", "s1")
    assert not nx.has_path(undirected, "far", "s1")


def test_snap_interior_sidewalks_splits_the_same_street_edge_for_two_different_loose_ends():
    # Two unrelated interior paths (e.g. two separate courtyard entrances
    # onto the same block) both land near the s1-s2 edge, at two different
    # points along it. The second split must not silently ignore or
    # overwrite the first -- both connections need to survive
    # independently, and the two loose ends should also end up connected
    # to each other via the (now twice-split) street edge.
    graph = _street_graph_with_one_edge()
    interior = nx.MultiDiGraph()
    interior.add_node("close_1", x=-73.9497, y=40.05003)
    interior.add_node("anchor_1", x=-73.9497, y=40.052)
    interior.add_edge("close_1", "anchor_1")
    interior.add_node("close_2", x=-73.9493, y=40.05003)
    interior.add_node("anchor_2", x=-73.9493, y=40.052)
    interior.add_edge("close_2", "anchor_2")

    result = streets._snap_interior_sidewalks(graph, interior, None, "test-tile")

    undirected = result.to_undirected()
    assert nx.has_path(undirected, "close_1", "s1")
    assert nx.has_path(undirected, "close_1", "s2")
    assert nx.has_path(undirected, "close_2", "s1")
    assert nx.has_path(undirected, "close_2", "s2")
    assert nx.has_path(undirected, "close_1", "close_2")


def test_snap_interior_sidewalks_gives_every_new_edge_an_osmid():
    # Real bug (2026-08-09): a split street sub-segment and the new
    # connector edge to the interior sidewalk's loose end were both being
    # added without an osmid, which crashed centerline.build_edge_table()
    # (via osmnx's own to_undirected()) the first time this ran end-to-end
    # on a real tile with a true parallel edge (Marine Park, r7c14) --
    # never caught by the narrower unit tests above, which only check
    # connectivity, not this attribute. A split sub-segment genuinely is
    # still part of the original street (s1-s2, osmid=555 from the fixture
    # above), so it should keep that real id; the brand-new connector edge
    # to the interior sidewalk never existed in OSM at all, so it needs its
    # own synthetic one instead.
    graph = _street_graph_with_one_edge()
    interior = _interior_graph_with_one_loose_end(-73.9495, 40.05003)

    result = streets._snap_interior_sidewalks(graph, interior, None, "test-tile")

    for u, v, k, data in result.edges(keys=True, data=True):
        assert data.get("osmid") is not None, f"edge ({u}, {v}) is missing osmid"

    split_segment_osmids = {
        data["osmid"] for u, v, k, data in result.edges(keys=True, data=True)
        if "geometry" in data
    }
    assert split_segment_osmids == {555}  # the real street's own id, preserved


def test_snap_interior_sidewalks_result_survives_build_edge_table():
    # The actual regression test for the real bug: running the snapped
    # result through the same downstream step the real pipeline does
    # (centerline.build_edge_table, which calls osmnx's to_undirected())
    # must not crash. Two loose ends snapping to the same street edge
    # (see the split test above) is what actually produces a true
    # parallel-edge scenario like the one that crashed on r7c14.
    from pipeline.graph.centerline import build_edge_table

    graph = _street_graph_with_one_edge()
    graph.graph["crs"] = "epsg:4326"
    interior = nx.MultiDiGraph()
    interior.add_node("close_1", x=-73.9497, y=40.05003)
    interior.add_node("anchor_1", x=-73.9497, y=40.052)
    interior.add_edge("close_1", "anchor_1", osmid=-1)
    interior.add_node("close_2", x=-73.9493, y=40.05003)
    interior.add_node("anchor_2", x=-73.9493, y=40.052)
    interior.add_edge("close_2", "anchor_2", osmid=-2)

    result = streets._snap_interior_sidewalks(graph, interior, None, "test-tile")

    # Mirror the pipeline: annotate node_ids (normally done right after
    # simplify) before build_edge_table, which now requires it (FIXES 13).
    streets._annotate_node_ids(result, streets._capture_coord_to_id(result))

    build_edge_table(result)  # must not raise


def test_missing_park_trail_graph_returns_none_for_no_trail_lines():
    graph = _street_graph_with_one_edge()

    assert streets._missing_park_trail_graph(graph, [], "test-tile") is None


def test_missing_park_trail_graph_returns_none_when_everything_is_already_covered():
    graph = _street_graph_with_one_edge()  # s1 (-73.9500, 40.0500) -> s2 (-73.9490, 40.0500)
    trail = LineString([(-73.9500, 40.0500), (-73.9490, 40.0500)])  # exactly on top of it

    assert streets._missing_park_trail_graph(graph, [trail], "test-tile") is None


def test_missing_park_trail_graph_builds_a_graph_for_a_genuinely_missing_trail():
    graph = _street_graph_with_one_edge()
    trail = LineString([(-73.9000, 40.1000), (-73.8990, 40.1000)])  # nowhere near the street

    result = streets._missing_park_trail_graph(graph, [trail], "test-tile")

    assert result is not None
    assert result.number_of_edges() == 1


def test_missing_park_trail_graph_continues_synthetic_ids_from_the_existing_graph():
    # FIXES.md item 1g: if interior sidewalks already snapped a synthetic
    # source into this tile, park trails' own new ids must continue past
    # whatever's already there, not restart at -1 (see
    # _snap_interior_sidewalks' own comment on the same collision risk).
    graph = _street_graph_with_one_edge()
    graph.add_node(-5, x=-73.8, y=40.2)  # simulates a node an earlier synthetic source placed
    trail = LineString([(-73.9000, 40.1000), (-73.8990, 40.1000)])

    result = streets._missing_park_trail_graph(graph, [trail], "test-tile")

    assert all(n <= -6 for n in result.nodes)


def test_fetch_streets_unions_in_interior_sidewalks(monkeypatch, tmp_path):
    # street_a and street_b are deliberately NOT connected within the main
    # street graph itself (each on its own separate stub edge to
    # other_a/other_b), same test shape as the parking-aisle union test
    # above -- so any connectivity between other_a and other_b can only
    # come from the interior-sidewalk union + snap.
    from pipeline.fetch import interior_sidewalks

    main_graph = nx.MultiDiGraph()
    main_graph.add_node("street_a", x=-73.9500, y=40.0500)
    main_graph.add_node("other_a", x=-73.9501, y=40.0499)
    main_graph.add_edge("street_a", "other_a")
    main_graph.add_node("street_b", x=-73.9490, y=40.0500)
    main_graph.add_node("other_b", x=-73.9489, y=40.0499)
    main_graph.add_edge("street_b", "other_b")

    # One interior segment running roughly parallel to the (nonexistent)
    # street_a<->street_b connection, ~3.3m offset -- comfortably within
    # the 5m snap distance at both ends.
    interior_geojson = {"type": "FeatureCollection", "features": [
        _segment_feature([[-73.9500, 40.05003], [-73.9490, 40.05003]]),
    ]}

    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph))
    # Override _mock_fetch's empty default AFTER it, same as the park-trail
    # union test below -- so this test's own segment wins.
    monkeypatch.setattr(interior_sidewalks, "fetch_interior_sidewalks", lambda **kwargs: interior_geojson)
    result = streets.fetch_streets(BBOX, "test-interior-sidewalk-tile")

    assert nx.has_path(result.to_undirected(), "other_a", "other_b")


def test_fetch_streets_works_with_no_interior_sidewalks_in_the_area(monkeypatch, tmp_path):
    # Most tiles have none -- the common real case, same as every other
    # narrower query. (_mock_fetch's default already covers this, but an
    # explicit test documents the behavior same as every other source.)
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=-73.95, y=40.05)

    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph))
    result = streets.fetch_streets(BBOX, "test-no-interior-sidewalk-tile")

    assert list(result.nodes) == ["m1"]


def test_fetch_streets_unions_in_missing_park_trail_segments(monkeypatch, tmp_path):
    # Same test shape as test_fetch_streets_unions_in_interior_sidewalks:
    # street_a and street_b are deliberately unconnected within the main
    # graph, so any connectivity between other_a and other_b can only
    # come from the trail union + snap.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("street_a", x=-73.9500, y=40.0500)
    main_graph.add_node("other_a", x=-73.9501, y=40.0499)
    main_graph.add_edge("street_a", "other_a")
    main_graph.add_node("street_b", x=-73.9490, y=40.0500)
    main_graph.add_node("other_b", x=-73.9489, y=40.0499)
    main_graph.add_edge("street_b", "other_b")

    trail_geojson = {"type": "FeatureCollection", "features": [
        _trail_feature([[-73.9500, 40.05003], [-73.9490, 40.05003]]),
    ]}

    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph))
    monkeypatch.setattr(park_trails, "fetch_park_trails", lambda **kwargs: trail_geojson)
    result = streets.fetch_streets(BBOX, "test-park-trail-tile")

    assert nx.has_path(result.to_undirected(), "other_a", "other_b")


def test_fetch_streets_works_with_no_park_trails_in_the_area(monkeypatch, tmp_path):
    # Most tiles have none -- the common real case, same as every other
    # narrower query. (_mock_fetch's default already covers this, but an
    # explicit test documents the behavior same as every other source.)
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=-73.95, y=40.05)

    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph))
    result = streets.fetch_streets(BBOX, "test-no-park-trail-tile")

    assert list(result.nodes) == ["m1"]


def test_fetch_streets_does_not_duplicate_an_already_covered_park_trail(monkeypatch, tmp_path):
    # The one behavior park trails need that interior sidewalks never
    # did: most real trails mostly duplicate a path OSM already has
    # (FIXES.md item 1g's own three-park survey found 82-92% overlap), so
    # a trail sitting right on top of an existing street must NOT get
    # spliced in as a second, redundant edge.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("street_a", x=-73.9500, y=40.0500)
    main_graph.add_node("street_b", x=-73.9490, y=40.0500)
    main_graph.add_edge("street_a", "street_b")

    trail_geojson = {"type": "FeatureCollection", "features": [
        _trail_feature([[-73.9500, 40.0500], [-73.9490, 40.0500]]),  # exactly on top
    ]}

    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph))
    monkeypatch.setattr(park_trails, "fetch_park_trails", lambda **kwargs: trail_geojson)
    result = streets.fetch_streets(BBOX, "test-covered-park-trail-tile")

    assert set(result.nodes) == {"street_a", "street_b"}


def test_fetch_streets_leaves_a_too_far_interior_sidewalk_unconnected(monkeypatch, tmp_path):
    # A real digitization gap (~22m from the nearest street, well beyond
    # the 5m snap distance) should still be added to the graph, just not
    # wired into the surrounding street network -- same treatment as a
    # dead-end parking aisle cluster, not silently dropped and not
    # force-connected.
    from pipeline.fetch import interior_sidewalks

    main_graph = nx.MultiDiGraph()
    main_graph.add_node("street_a", x=-73.9500, y=40.0500)
    main_graph.add_node("street_b", x=-73.9490, y=40.0500)
    main_graph.add_edge("street_a", "street_b")

    interior_geojson = {"type": "FeatureCollection", "features": [
        _segment_feature([[-73.9495, 40.05020], [-73.9495, 40.06]]),
    ]}
    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph))
    monkeypatch.setattr(interior_sidewalks, "fetch_interior_sidewalks", lambda **kwargs: interior_geojson)
    result = streets.fetch_streets(BBOX, "test-far-interior-sidewalk-tile")

    # The interior segment's nodes get synthetic ids assigned internally
    # (see _build_interior_sidewalk_graph), so identify them by set
    # difference rather than a name this test doesn't control.
    new_nodes = set(result.nodes) - set(main_graph.nodes)
    assert new_nodes  # the far segment was still added, not dropped
    undirected = result.to_undirected()
    assert not any(nx.has_path(undirected, node, "street_a") for node in new_nodes)


def test_fetch_streets_fetches_barrier_ways_once_for_both_interior_sidewalks_and_park_trails(monkeypatch, tmp_path):
    # interior sidewalks (1a) and park trails (1g) each need barrier data
    # for their own snap pass -- same bbox/filter either way, so a tile
    # where both apply should cost one Overpass barrier-ways request, not
    # two. Found while investigating why the v18 citywide re-fetch ran
    # slower than v9's baseline: every tile with both sources was asking
    # Overpass for identical data twice.
    from pipeline.fetch import interior_sidewalks

    main_graph = nx.MultiDiGraph()
    main_graph.add_node("street_a", x=-73.9500, y=40.0500)
    main_graph.add_node("street_b", x=-73.9490, y=40.0500)
    main_graph.add_edge("street_a", "street_b")

    interior_geojson = {"type": "FeatureCollection", "features": [
        _segment_feature([[-73.9495, 40.05020], [-73.9495, 40.06]]),
    ]}

    trail_geojson = {"type": "FeatureCollection", "features": [
        _trail_feature([[-73.9480, 40.0700], [-73.9470, 40.0700]]),
    ]}

    barrier_calls = {"count": 0}

    def barrier_fn(**kwargs):
        barrier_calls["count"] += 1
        raise ValueError("no barrier ways here")

    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph, barrier_fn=barrier_fn))
    # Both overrides after _mock_fetch, so each source's own geojson wins.
    monkeypatch.setattr(interior_sidewalks, "fetch_interior_sidewalks", lambda **kwargs: interior_geojson)
    monkeypatch.setattr(park_trails, "fetch_park_trails", lambda **kwargs: trail_geojson)
    streets.fetch_streets(BBOX, "test-shared-barrier-fetch-tile")

    assert barrier_calls["count"] == 1


# ---- citywide auxiliary layers (FIXES.md item 6b) ---------------------------
# The six auxiliary queries are fetched once citywide and sliced per tile
# (see pipeline/fetch/citywide_layers.py for the measured why: every
# Overpass query costs ~25s regardless of result size, and the six were
# 78.4% of all fetch wait in the v19 refetch). The slice must be a drop-in
# replacement for the per-tile query it replaced -- same truncation
# semantics as ox.graph_from_bbox (boundary-inclusive node test, isolated
# nodes kept) -- and Overpass must be asked once per layer per process, not
# once per tile.


def test_aux_layer_slice_keeps_only_nodes_inside_the_tile_bbox(monkeypatch, tmp_path):
    _isolate_citywide_layers(monkeypatch, tmp_path)
    citywide = nx.MultiDiGraph()
    citywide.add_node("in_a", x=-73.95, y=40.05)
    citywide.add_node("in_b", x=-73.951, y=40.051)
    citywide.add_edge("in_a", "in_b")
    citywide.add_node("far_a", x=-73.99, y=40.60)  # another tile's data
    citywide.add_node("far_b", x=-73.991, y=40.601)
    citywide.add_edge("far_a", "far_b")
    citywide_layers._MEMO["cycleways"] = citywide

    sliced = streets._aux_layer_graph(BBOX, "test-slice-tile", "cycleways")

    assert set(sliced.nodes) == {"in_a", "in_b"}
    assert sliced.number_of_edges() == 1


def test_aux_layer_slice_is_boundary_inclusive_and_keeps_isolated_nodes(monkeypatch, tmp_path):
    # osmnx's own truncation admits boundary points (a shapely intersects
    # test) and leaves isolated nodes in place (a way crossing the bbox
    # edge keeps its inside nodes even when every neighbor is outside) --
    # today's composed tile graphs genuinely contain both cases, so the
    # slice must reproduce them, not "clean them up".
    _isolate_citywide_layers(monkeypatch, tmp_path)
    citywide = nx.MultiDiGraph()
    citywide.add_node("on_edge", x=BBOX.lon_min, y=BBOX.lat_min)
    citywide.add_node("inside_isolated", x=-73.95, y=40.05)
    citywide.add_node("outside", x=-73.95, y=40.2)
    citywide.add_edge("inside_isolated", "outside")
    citywide_layers._MEMO["barriers"] = citywide

    sliced = streets._aux_layer_graph(BBOX, "test-boundary-tile", "barriers")

    assert set(sliced.nodes) == {"on_edge", "inside_isolated"}
    assert sliced.number_of_edges() == 0


def test_aux_layer_slice_returns_none_when_nothing_falls_inside(monkeypatch, tmp_path):
    _isolate_citywide_layers(monkeypatch, tmp_path)
    citywide = nx.MultiDiGraph()
    citywide.add_node("far", x=-73.95, y=40.7)
    citywide_layers._MEMO["parking_aisles"] = citywide

    assert streets._aux_layer_graph(BBOX, "test-empty-slice-tile", "parking_aisles") is None


def test_aux_layers_ask_overpass_once_per_layer_per_process_not_per_tile(monkeypatch, tmp_path):
    # The entire point of item 6b: measured 714 minutes of Overpass wait
    # across the v19 refetch, 78.4% of it these six queries repeated per
    # tile.
    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=-73.95, y=40.05)

    cycleway_calls = {"count": 0}

    def cycleway_fn(**kwargs):
        cycleway_calls["count"] += 1
        graph = nx.MultiDiGraph()
        graph.add_node("c1", x=-73.951, y=40.051)
        return graph

    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph, cycleway_fn))
    streets.fetch_streets(BBOX, "test-layer-once-tile-a")
    streets.fetch_streets(BBOX, "test-layer-once-tile-b")

    assert cycleway_calls["count"] == 1


def test_citywide_layer_disk_cache_survives_a_new_process(monkeypatch, tmp_path):
    # The memo only lives per process; the graphml cache is what saves the
    # NEXT borough run (or a resumed one) from re-fetching every layer.
    # Round-trips through osmnx's real save/load, same reasoning as
    # _cache_roundtrip above.
    _isolate_citywide_layers(monkeypatch, tmp_path)
    fetches = {"count": 0}

    def fake_fetch(**kwargs):
        fetches["count"] += 1
        graph = nx.MultiDiGraph(crs="epsg:4326")
        graph.add_node(1111, x=-73.95, y=40.05)  # int id: osmnx casts on load
        return graph

    monkeypatch.setattr(citywide_layers.ox, "graph_from_bbox", fake_fetch)

    first = citywide_layers.citywide_layer("cycleways", "unused-filter")
    citywide_layers._MEMO.clear()  # simulate a new process
    second = citywide_layers.citywide_layer("cycleways", "unused-filter")

    assert fetches["count"] == 1
    assert 1111 in first.nodes
    assert 1111 in second.nodes


def test_citywide_layer_refetches_when_the_recorded_bbox_no_longer_matches(monkeypatch, tmp_path):
    # Same discipline as the per-tile cache's fetch_bbox guard: the
    # filename can't be trusted after a CITY_BBOX/grid redefinition (the
    # r17c14 lesson -- see streets._bbox_signature).
    _isolate_citywide_layers(monkeypatch, tmp_path)
    fetches = {"count": 0}

    def fake_fetch(**kwargs):
        fetches["count"] += 1
        graph = nx.MultiDiGraph(crs="epsg:4326")
        graph.add_node(1111, x=-73.95, y=40.05)
        return graph

    monkeypatch.setattr(citywide_layers.ox, "graph_from_bbox", fake_fetch)

    citywide_layers.citywide_layer("cycleways", "unused-filter")
    citywide_layers._MEMO.clear()
    monkeypatch.setattr(
        citywide_layers, "_layer_fetch_bbox",
        lambda: Bbox(lat_min=41.0, lat_max=41.5, lon_min=-74.0, lon_max=-73.5),
    )
    citywide_layers.citywide_layer("cycleways", "unused-filter")

    assert fetches["count"] == 2


def test_citywide_layer_caches_an_empty_layer_without_reasking(monkeypatch, tmp_path):
    # "The filter matched nothing citywide" is a real answer -- the marker
    # file keeps a fresh process from re-asking Overpass for it.
    _isolate_citywide_layers(monkeypatch, tmp_path)
    fetches = {"count": 0}

    def fake_fetch(**kwargs):
        fetches["count"] += 1
        raise ValueError("nothing matched citywide")

    monkeypatch.setattr(citywide_layers.ox, "graph_from_bbox", fake_fetch)

    first = citywide_layers.citywide_layer("foot_overrides", "unused-filter")
    citywide_layers._MEMO.clear()
    second = citywide_layers.citywide_layer("foot_overrides", "unused-filter")

    assert first is None
    assert second is None
    assert fetches["count"] == 1


# ---- raw-fetch cache split (FIXES.md item 11) ------------------------------
# The raw Overpass caches (per-tile WALK_FILTER snapshots + the citywide
# layers) key on raw_fetch_key() -- what changes what Overpass RETURNS --
# while the processed per-tile graph keeps GRAPH_CACHE_VERSION -- what
# changes what we BUILD from it. A processing-only version bump must
# rebuild locally from the raw snapshots; pulling fresh OSM is the
# explicit --refresh-raw, never a side effect. (The v20 refetch
# re-downloaded ~5h of byte-identical data for a purely-local weld pass.)


def test_raw_fetch_key_changes_with_filter_and_retained_tags(monkeypatch):
    base = citywide_layers.raw_fetch_key("filter-a")
    # Stable for identical inputs -- it's a cache key, not a nonce.
    assert citywide_layers.raw_fetch_key("filter-a") == base
    # A filter edit must invalidate (the old GRAPH_CACHE_VERSION keying's
    # one real job, preserved).
    assert citywide_layers.raw_fetch_key("filter-b") != base
    # So must a retained-way-tag change: v19 added `layer` to
    # useful_tags_way with no filter edit at all, and that changed every
    # fetched graph's content.
    monkeypatch.setattr(
        citywide_layers.ox.settings, "useful_tags_way",
        list(citywide_layers.ox.settings.useful_tags_way) + ["zzz_new_tag"],
    )
    assert citywide_layers.raw_fetch_key("filter-a") != base


def test_raw_fetch_key_changes_with_the_osmnx_version(monkeypatch):
    # osmnx's bbox-truncation semantics are baked into a saved snapshot;
    # an upgrade must invalidate rather than mix two versions' semantics
    # in one cache.
    base = citywide_layers.raw_fetch_key("filter-a")
    monkeypatch.setattr(citywide_layers.ox, "__version__", "0.0.0-test")
    assert citywide_layers.raw_fetch_key("filter-a") != base


def _isolate_raw_streets(monkeypatch, tmp_path):
    monkeypatch.setattr(streets, "RAW_STREETS_DIR", tmp_path / "streets_raw")
    monkeypatch.setattr(streets.time, "sleep", lambda seconds: None)


def _counting_raw_fetch(fetches):
    """A realistic small WALK_FILTER response: int OSM ids and a crs, so
    it survives osmnx's REAL save/load (these tests exercise the actual
    GraphML round trip, unlike the wiring tests' in-memory stand-ins)."""
    def fetch(**kwargs):
        fetches["count"] += 1
        graph = nx.MultiDiGraph(crs="epsg:4326")
        graph.add_node(1111, x=-73.95, y=40.05)
        graph.add_node(2222, x=-73.951, y=40.051)
        graph.add_edge(1111, 2222, osmid=555, length=100.0, highway="residential")
        return graph
    return fetch


def test_raw_walk_graph_downloads_once_then_reads_the_snapshot(monkeypatch, tmp_path):
    _isolate_raw_streets(monkeypatch, tmp_path)
    fetches = {"count": 0}
    monkeypatch.setattr(streets.ox, "graph_from_bbox", _counting_raw_fetch(fetches))

    first = streets._raw_walk_graph(BBOX, "rawtile")
    second = streets._raw_walk_graph(BBOX, "rawtile")

    assert fetches["count"] == 1
    assert set(first.nodes) == set(second.nodes) == {1111, 2222}
    # Identical in attributes AND types: the fresh build returns the
    # re-loaded snapshot (not the in-memory original), so a later rebuild
    # sees byte-for-byte the same input this build processed.
    assert dict(first.nodes(data=True)) == dict(second.nodes(data=True))
    assert list(first.edges(keys=True, data=True)) == list(second.edges(keys=True, data=True))


def test_raw_walk_graph_returns_the_reloaded_snapshot_not_the_in_memory_graph(monkeypatch, tmp_path):
    # The determinism guarantee is structural: processing always consumes
    # what the GraphML round trip produces, whether the data was just
    # downloaded or read back a month later.
    _isolate_raw_streets(monkeypatch, tmp_path)
    produced = {}

    def fetch(**kwargs):
        graph = nx.MultiDiGraph(crs="epsg:4326")
        graph.add_node(1111, x=-73.95, y=40.05)
        produced["graph"] = graph
        return graph

    monkeypatch.setattr(streets.ox, "graph_from_bbox", fetch)
    result = streets._raw_walk_graph(BBOX, "rawtile-roundtrip")

    assert result is not produced["graph"]
    assert 1111 in result.nodes


def test_raw_walk_graph_caches_an_empty_tile_without_reasking(monkeypatch, tmp_path):
    # Open-water tiles are real and common at the coastline; the marker
    # file keeps every later run from re-asking Overpass about them.
    _isolate_raw_streets(monkeypatch, tmp_path)
    fetches = {"count": 0}

    def fetch(**kwargs):
        fetches["count"] += 1
        raise InsufficientResponseError("No data elements in server response.")

    monkeypatch.setattr(streets.ox, "graph_from_bbox", fetch)

    assert streets._raw_walk_graph(BBOX, "watertile") is None
    assert streets._raw_walk_graph(BBOX, "watertile") is None
    assert fetches["count"] == 1


def test_raw_walk_graph_refetches_when_the_recorded_bbox_no_longer_matches(monkeypatch, tmp_path):
    # Same discipline as both existing caches (the r17c14 grid-shift
    # lesson): the filename alone can't be trusted across a grid
    # redefinition.
    _isolate_raw_streets(monkeypatch, tmp_path)
    fetches = {"count": 0}
    monkeypatch.setattr(streets.ox, "graph_from_bbox", _counting_raw_fetch(fetches))

    streets._raw_walk_graph(BBOX, "shifttile")
    shifted = Bbox(
        lat_min=BBOX.lat_min + config.TILE_SIZE_LAT_DEG,
        lat_max=BBOX.lat_max + config.TILE_SIZE_LAT_DEG,
        lon_min=BBOX.lon_min,
        lon_max=BBOX.lon_max,
    )
    streets._raw_walk_graph(shifted, "shifttile")

    assert fetches["count"] == 2


def test_raw_walk_graph_refresh_pulls_fresh_despite_a_valid_snapshot(monkeypatch, tmp_path):
    _isolate_raw_streets(monkeypatch, tmp_path)
    fetches = {"count": 0}
    monkeypatch.setattr(streets.ox, "graph_from_bbox", _counting_raw_fetch(fetches))

    streets._raw_walk_graph(BBOX, "freshtile")
    streets._raw_walk_graph(BBOX, "freshtile", refresh=True)

    assert fetches["count"] == 2


def test_fetch_streets_refresh_raw_ignores_a_valid_processed_cache(monkeypatch, tmp_path):
    # --refresh-raw means "pull fresh OSM": the processed cache was built
    # from the old raw data by definition, so trusting it would silently
    # hand back exactly what the flag exists to replace.
    result, fetches = _cache_roundtrip(
        monkeypatch, tmp_path, _cached_graph(), streets._bbox_signature(BBOX),
        refresh_raw=True,
    )
    assert fetches > 0, "a valid processed cache must not satisfy --refresh-raw"
    assert 1111 not in result.nodes


def test_citywide_layer_refresh_refetches_a_valid_cache_once_per_process(monkeypatch, tmp_path):
    # refresh=True must beat both the memo and the disk cache -- but only
    # once per process, or a --refresh-raw borough run would re-download
    # every layer per tile (the per-tile repetition item 6b removed).
    _isolate_citywide_layers(monkeypatch, tmp_path)
    fetches = {"count": 0}

    def fake_fetch(**kwargs):
        fetches["count"] += 1
        graph = nx.MultiDiGraph(crs="epsg:4326")
        graph.add_node(1111, x=-73.95, y=40.05)
        return graph

    monkeypatch.setattr(citywide_layers.ox, "graph_from_bbox", fake_fetch)

    citywide_layers.citywide_layer("cycleways", "unused-filter")
    assert fetches["count"] == 1

    # Simulate a fresh process holding yesterday's disk cache.
    citywide_layers._MEMO.clear()
    citywide_layers._FRESHENED.clear()
    citywide_layers.citywide_layer("cycleways", "unused-filter")
    assert fetches["count"] == 1  # disk cache honored without refresh

    citywide_layers.citywide_layer("cycleways", "unused-filter", refresh=True)
    assert fetches["count"] == 2  # refresh beats memo + disk
    citywide_layers.citywide_layer("cycleways", "unused-filter", refresh=True)
    assert fetches["count"] == 2  # ...but only once per process


def test_citywide_layer_cache_keys_on_the_filter_itself(monkeypatch, tmp_path):
    # A filter edit lands in the key, so it can never silently reuse a
    # layer fetched under the old definition -- while the old file just
    # stops matching (kept on disk, not clobbered), so reverting the
    # filter finds it again.
    _isolate_citywide_layers(monkeypatch, tmp_path)
    fetches = {"count": 0}

    def fake_fetch(**kwargs):
        fetches["count"] += 1
        graph = nx.MultiDiGraph(crs="epsg:4326")
        graph.add_node(1111, x=-73.95, y=40.05)
        return graph

    monkeypatch.setattr(citywide_layers.ox, "graph_from_bbox", fake_fetch)

    citywide_layers.citywide_layer("cycleways", "filter-one")
    assert fetches["count"] == 1

    citywide_layers._MEMO.clear()
    citywide_layers.citywide_layer("cycleways", "filter-two")
    assert fetches["count"] == 2  # new filter = new key = cache miss

    citywide_layers._MEMO.clear()
    citywide_layers.citywide_layer("cycleways", "filter-one")
    assert fetches["count"] == 2  # the original file still matches its key


# ---- closure zones (FIXES.md item 0b, v19) --------------------------------
# The imported synthetic layers must not re-add paths through a known
# construction closure (East River Park: OSM's mappers DELETED the paths,
# so no tag can infer the closure -- measured 2026-08-14, 0/32 ghost edges
# inside any landuse=construction polygon). Zones are curated polygons in
# pipeline/closure_zones.json; the clip runs in METRIC_CRS.


def _square_zone_m(lon, lat, half_m):
    """A test closure zone: a square centered on lon/lat, half_m meters
    to each side, in METRIC_CRS like the real loaded zones."""
    from shapely.geometry import Polygon

    x, y = streets._TO_METRIC_CRS(lon, lat)
    return Polygon([(x - half_m, y - half_m), (x + half_m, y - half_m),
                    (x + half_m, y + half_m), (x - half_m, y + half_m)])


def test_clip_closure_zones_drops_a_segment_fully_inside(monkeypatch):
    monkeypatch.setattr(streets, "_CLOSURE_ZONES_M", [_square_zone_m(-73.99, 40.7, 200.0)])
    inside = LineString([(-73.9901, 40.7), (-73.9899, 40.7)])  # ~17m, centered
    assert streets._clip_closure_zones(inside) == []


def test_clip_closure_zones_keeps_the_outside_remnants(monkeypatch):
    monkeypatch.setattr(streets, "_CLOSURE_ZONES_M", [_square_zone_m(-73.99, 40.7, 100.0)])
    # ~640m west-to-east straight through the 200m-wide zone
    crossing = LineString([(-73.9938, 40.7), (-73.9862, 40.7)])
    kept = streets._clip_closure_zones(crossing)
    assert len(kept) == 2
    for piece in kept:
        for lon, lat in piece.coords:
            x, _ = streets._TO_METRIC_CRS(lon, lat)
            zx, _ = streets._TO_METRIC_CRS(-73.99, 40.7)
            assert abs(x - zx) >= 99.0  # every kept point is outside the zone


def test_clip_closure_zones_leaves_far_segments_untouched(monkeypatch):
    monkeypatch.setattr(streets, "_CLOSURE_ZONES_M", [_square_zone_m(-73.99, 40.7, 100.0)])
    far = LineString([(-73.95, 40.72), (-73.949, 40.72)])
    assert streets._clip_closure_zones(far) == [far]


def test_clip_closure_zones_drops_boundary_slivers(monkeypatch):
    # A remnant shorter than CLOSURE_ZONE_MIN_REMNANT_M is digitization
    # noise at the polygon's edge, not a usable path.
    monkeypatch.setattr(streets, "_CLOSURE_ZONES_M", [_square_zone_m(-73.99, 40.7, 100.0)])
    # ends ~5m past the zone's western edge: remnant ~5m < 10m floor
    x_west_edge = -73.99 - 100.0 / (111320.0 * 0.7578)  # rough deg-per-m at 40.7
    barely_out = LineString([(x_west_edge - 0.00006, 40.7), (-73.99, 40.7)])
    kept = streets._clip_closure_zones(barely_out)
    assert kept == [] or all(
        streets.transform(streets._TO_METRIC_CRS, piece).length
        >= streets.CLOSURE_ZONE_MIN_REMNANT_M
        for piece in kept
    )


def test_real_closure_zones_file_covers_east_river_park():
    # The shipped file must contain the ESCR zone: the point below is one
    # of the 32 confirmed ghost-edge locations (FIXES item 0b's answer
    # key, data/audits/2026-08-13/erp_phantoms.json).
    from shapely.geometry import Point as _Point

    assert len(streets._CLOSURE_ZONES_M) >= 1
    ghost = _Point(streets._TO_METRIC_CRS(-73.972923, 40.723911))
    assert any(zone.contains(ghost) for zone in streets._CLOSURE_ZONES_M)


def test_interior_sidewalks_for_tile_clips_closure_zones(monkeypatch):
    monkeypatch.setattr(streets, "_CLOSURE_ZONES_M", [_square_zone_m(-73.99, 40.7, 100.0)])
    geojson = {"features": [
        {"geometry": {"type": "LineString",
                      "coordinates": [[-73.9901, 40.7], [-73.9899, 40.7]]}},  # inside
        {"geometry": {"type": "LineString",
                      "coordinates": [[-73.95, 40.72], [-73.949, 40.72]]}},   # far away
    ]}
    tile_bbox = Bbox(lat_min=40.6, lat_max=40.8, lon_min=-74.1, lon_max=-73.9)
    segments = streets._interior_sidewalks_for_tile(geojson, tile_bbox)
    assert segments == [[(-73.95, 40.72), (-73.949, 40.72)]]


# --- drawing-error welds (FIXES item 1, connection Batch A) ---------------
#
# _weld_drawing_error_components is pure graph surgery (no network), so
# these build MultiDiGraphs directly. Coordinates are real NYC lon/lat --
# the function measures in the metric CRS, so degrees must be honest.
# At lat 40.70, 0.000003 degrees of latitude is ~0.33m.

_STREET_Y = 40.70


def _weld_graph(scrap_lat_offset_deg: float, street_tags: dict | None = None,
                scrap_ids=(101, 102), street_len: float = 844.0):
    """A straight east-west street plus a 2-node scrap fragment whose
    near node hovers scrap_lat_offset_deg north of the street line."""
    g = nx.MultiDiGraph()
    g.graph["crs"] = "epsg:4326"
    g.add_node(1, x=-73.99, y=_STREET_Y)
    g.add_node(2, x=-73.98, y=_STREET_Y)
    g.add_edge(1, 2, osmid=555, length=street_len, **(street_tags or {}))
    near, far = scrap_ids
    g.add_node(near, x=-73.985, y=_STREET_Y + scrap_lat_offset_deg)
    g.add_node(far, x=-73.985, y=_STREET_Y + scrap_lat_offset_deg + 0.0001)
    g.add_edge(near, far, osmid=777, length=11.0)
    return g


def test_weld_connects_a_drawing_error_fragment():
    g = _weld_graph(scrap_lat_offset_deg=0.000003)  # ~0.33m gap
    g = streets._weld_drawing_error_components(g, None, "test")
    assert nx.number_connected_components(g.to_undirected(as_view=True)) == 1
    weld_edges = [(u, v, d) for u, v, d in g.edges(data=True) if d.get("weld")]
    assert len(weld_edges) == 1
    # the connector is synthetic: negative osmid, and it lands on a
    # freshly-minted negative split node (or directly on a street node)
    assert weld_edges[0][2]["osmid"] < 0


def test_weld_split_ids_continue_existing_negative_counters():
    g = _weld_graph(scrap_lat_offset_deg=0.000003)
    g.add_node(-1, x=-73.9899, y=_STREET_Y + 0.0002)
    g.add_edge(-1, 1, osmid=-1, length=22.0)
    g = streets._weld_drawing_error_components(g, None, "test")
    new_negative_nodes = [n for n in g.nodes if isinstance(n, int) and n < -1]
    assert new_negative_nodes, "split node should continue below the existing -1"
    negative_osmids = [d["osmid"] for _, _, d in g.edges(data=True)
                       if isinstance(d.get("osmid"), int) and d["osmid"] < 0]
    assert len(negative_osmids) == len(set(negative_osmids)), (
        "synthetic edge osmids must never collide")
    assert min(negative_osmids) < -1, (
        "connector osmid should continue below the existing -1")


def test_weld_leaves_a_real_gap_alone():
    g = _weld_graph(scrap_lat_offset_deg=0.00002)  # ~2.2m -- a real gap
    g = streets._weld_drawing_error_components(g, None, "test")
    assert nx.number_connected_components(g.to_undirected(as_view=True)) == 2
    assert not any(d.get("weld") for _, _, d in g.edges(data=True))


def test_weld_elevation_veto_blocks_ground_fragment_under_a_bridge():
    g = _weld_graph(scrap_lat_offset_deg=0.000003, street_tags={"bridge": "yes"})
    g = streets._weld_drawing_error_components(g, None, "test")
    assert nx.number_connected_components(g.to_undirected(as_view=True)) == 2


def test_weld_elevation_match_allows_bridge_fragment_onto_bridge():
    g = _weld_graph(scrap_lat_offset_deg=0.000003, street_tags={"bridge": "yes"})
    for u, v, k in list(g.edges(keys=True)):
        if g.edges[u, v, k].get("osmid") == 777:
            g.edges[u, v, k]["bridge"] = "yes"
    g = streets._weld_drawing_error_components(g, None, "test")
    assert nx.number_connected_components(g.to_undirected(as_view=True)) == 1


def test_weld_barrier_veto_blocks_a_fenced_gap():
    g = _weld_graph(scrap_lat_offset_deg=0.000003)
    barrier = nx.MultiDiGraph()
    barrier.graph["crs"] = "epsg:4326"
    barrier.add_node(9001, x=-73.9851, y=_STREET_Y + 0.0000015)
    barrier.add_node(9002, x=-73.9849, y=_STREET_Y + 0.0000015)
    barrier.add_edge(9001, 9002, osmid=888, length=17.0)
    g = streets._weld_drawing_error_components(g, barrier, "test")
    assert nx.number_connected_components(g.to_undirected(as_view=True)) == 2


def test_weld_skips_imported_only_fragments():
    # synthetic (negative-id) fragments are FIXES item 6's territory
    g = _weld_graph(scrap_lat_offset_deg=0.000003, scrap_ids=(-50, -51))
    g = streets._weld_drawing_error_components(g, None, "test")
    assert nx.number_connected_components(g.to_undirected(as_view=True)) == 2


def test_weld_never_merges_two_real_networks():
    # both components >= WELD_SCRAP_MAX_LEN_M: neither is a scrap, even
    # 0.33m apart -- a genuine sub-network deserves a verified fix
    g = _weld_graph(scrap_lat_offset_deg=0.000003, street_len=6000.0)
    for u, v, k in list(g.edges(keys=True)):
        if g.edges[u, v, k].get("osmid") == 777:
            g.edges[u, v, k]["length"] = 6000.0
    g = streets._weld_drawing_error_components(g, None, "test")
    assert nx.number_connected_components(g.to_undirected(as_view=True)) == 2


def test_weld_duplicate_parallel_way_welds_only_at_loose_ends():
    # a fragment drawn on top of the street (every vertex within 0.5m)
    # must weld at its two ENDS, not manufacture a rung at every vertex
    g = nx.MultiDiGraph()
    g.graph["crs"] = "epsg:4326"
    g.add_node(1, x=-73.99, y=_STREET_Y)
    g.add_node(2, x=-73.98, y=_STREET_Y)
    g.add_edge(1, 2, osmid=555, length=844.0)
    xs = [-73.987, -73.986, -73.985, -73.984, -73.983]
    for i, x in enumerate(xs):
        g.add_node(201 + i, x=x, y=_STREET_Y + 0.000003)
    for i in range(len(xs) - 1):
        g.add_edge(201 + i, 202 + i, osmid=778, length=84.0)
    g = streets._weld_drawing_error_components(g, None, "test")
    assert nx.number_connected_components(g.to_undirected(as_view=True)) == 1
    weld_edges = [d for _, _, d in g.edges(data=True) if d.get("weld")]
    assert len(weld_edges) == 2, f"expected end welds only, got {len(weld_edges)}"


def test_weld_mid_line_touch_gets_a_single_closest_weld():
    # fragment whose loose ends are FAR from the street but whose interior
    # brushes it: one weld at the closest node, nothing else
    g = nx.MultiDiGraph()
    g.graph["crs"] = "epsg:4326"
    g.add_node(1, x=-73.99, y=_STREET_Y)
    g.add_node(2, x=-73.98, y=_STREET_Y)
    g.add_edge(1, 2, osmid=555, length=844.0)
    g.add_node(301, x=-73.9853, y=_STREET_Y + 0.0002)   # far end
    g.add_node(302, x=-73.985, y=_STREET_Y + 0.000003)  # brushes the street
    g.add_node(303, x=-73.9847, y=_STREET_Y + 0.0002)   # far end
    g.add_edge(301, 302, osmid=779, length=25.0)
    g.add_edge(302, 303, osmid=779, length=25.0)
    g = streets._weld_drawing_error_components(g, None, "test")
    assert nx.number_connected_components(g.to_undirected(as_view=True)) == 1
    weld_edges = [d for _, _, d in g.edges(data=True) if d.get("weld")]
    assert len(weld_edges) == 1


# ---- fee-gated attraction grounds (FIXES.md item 1, v25) ------------------
# Zoos, the aquarium, and ticketed botanical gardens are destinations,
# not thoroughfares -- but untagged OSM footways pass WALK_FILTER and
# the imported layers sweep their interiors up via park polygons
# (measured 2026-08-20: ~137km across 11 sites). Zones are curated
# polygons in pipeline/fee_gated_zones.json; imported geometry is
# clipped (_clip_zone_polygons), edges fully inside a zone are removed
# from the composed graph (_drop_zone_interior_edges) with road-class
# and verified-public-name exemptions. Since v27 the same two passes
# also serve the restricted zones (exemption-free; tests further down).


def _test_fee_zone(lon, lat, half_m, name="test attraction"):
    """A fee-gated test zone in the loaded tuple shape: (name, lon/lat
    polygon, METRIC_CRS polygon), a square half_m meters to each side."""
    from shapely.geometry import Polygon

    x, y = streets._TO_METRIC_CRS(lon, lat)
    ring_m = [(x - half_m, y - half_m), (x + half_m, y - half_m),
              (x + half_m, y + half_m), (x - half_m, y + half_m)]
    ring = [streets._FROM_METRIC_CRS(px, py) for px, py in ring_m]
    return (name, Polygon(ring), Polygon(ring_m))


def test_clip_fee_gated_zones_drops_a_segment_fully_inside():
    zone = _test_fee_zone(-73.99, 40.7, 200.0)
    inside = LineString([(-73.9901, 40.7), (-73.9899, 40.7)])  # ~17m, centered
    assert streets._clip_zone_polygons(inside, zones=[zone]) == []


def test_clip_fee_gated_zones_keeps_the_outside_remnants():
    zone = _test_fee_zone(-73.99, 40.7, 100.0)
    # ~640m west-to-east straight through the 200m-wide zone
    crossing = LineString([(-73.9938, 40.7), (-73.9862, 40.7)])
    kept = streets._clip_zone_polygons(crossing, zones=[zone])
    assert len(kept) == 2
    zx, _ = streets._TO_METRIC_CRS(-73.99, 40.7)
    for piece in kept:
        for lon, lat in piece.coords:
            x, _ = streets._TO_METRIC_CRS(lon, lat)
            assert abs(x - zx) >= 99.0  # every kept point is outside the zone


def test_clip_fee_gated_zones_leaves_far_segments_untouched():
    zone = _test_fee_zone(-73.99, 40.7, 100.0)
    far = LineString([(-73.95, 40.72), (-73.949, 40.72)])
    assert streets._clip_zone_polygons(far, zones=[zone]) == [far]


def _fee_zone_graph():
    """Nodes 1,2 inside the test zone, node 3 well outside; every edge
    carries x/y-real attrs the drop pass reads."""
    g = nx.MultiDiGraph()
    g.graph["crs"] = "epsg:4326"
    g.add_node(1, x=-73.9901, y=40.7)
    g.add_node(2, x=-73.9899, y=40.7)
    g.add_node(3, x=-73.9938, y=40.7)
    return g


def test_drop_fee_gated_edges_removes_interior_paths_only():
    zone = _test_fee_zone(-73.99, 40.7, 200.0)
    g = _fee_zone_graph()
    g.add_edge(1, 2, highway="footway", length=17.0)          # inside: doomed
    g.add_edge(1, 3, highway="footway", length=310.0)         # crossing: stays
    g = streets._drop_zone_interior_edges(
        g, "test", [zone], streets.FEE_GATED_EXEMPT_HIGHWAYS,
        streets.FEE_GATED_EXEMPT_NAMES, "fee-gated exclusion")
    assert not g.has_edge(1, 2)
    assert g.has_edge(1, 3)
    assert 2 not in g.nodes  # stranded by the removal
    assert 1 in g.nodes and 3 in g.nodes


def test_drop_fee_gated_edges_exempts_road_classes_and_public_names():
    # The Bronx Zoo polygon covers a public primary road; the Central
    # Park Zoo polygon covers Wien Walk -- both verified 2026-08-20,
    # neither may be removed.
    zone = _test_fee_zone(-73.99, 40.7, 200.0)
    g = _fee_zone_graph()
    g.add_edge(1, 2, highway="primary", length=17.0)
    g.add_edge(1, 2, highway="footway", name="Wien Walk", length=17.0)
    g = streets._drop_zone_interior_edges(
        g, "test", [zone], streets.FEE_GATED_EXEMPT_HIGHWAYS,
        streets.FEE_GATED_EXEMPT_NAMES, "fee-gated exclusion")
    assert g.number_of_edges() == 2


def test_drop_fee_gated_edges_requires_both_ends_in_the_same_zone():
    # An edge threading BETWEEN two nearby zones (the public path between
    # the Queens Zoo's two rings) must survive: one endpoint in each zone
    # is not "inside" either.
    zone_a = _test_fee_zone(-73.99, 40.7, 100.0, name="zone a")
    zone_b = _test_fee_zone(-73.9938, 40.7, 100.0, name="zone b")
    g = _fee_zone_graph()
    g.add_edge(1, 3, highway="footway", length=310.0)  # node 1 in a, 3 in b
    g = streets._drop_zone_interior_edges(
        g, "test", [zone_a, zone_b], streets.FEE_GATED_EXEMPT_HIGHWAYS,
        streets.FEE_GATED_EXEMPT_NAMES, "fee-gated exclusion")
    assert g.has_edge(1, 3)


def test_real_fee_gated_zones_file_covers_the_verified_sites():
    # The shipped file must contain the 13 reviewed polygons (12 sites;
    # Queens Zoo is two rings). The probe point is the Norwood lead's
    # endpoint -- a synthetic node that sat INSIDE the Bronx Zoo, the
    # finding that exposed the whole class (2026-08-20).
    from shapely.geometry import Point as _Point

    zones = streets._FEE_GATED_ZONES
    assert len(zones) == 13
    names = {name for name, _poly, _poly_m in zones}
    for expected in ("Bronx Zoo", "New York Botanical Garden",
                     "Brooklyn Botanic Garden", "Central Park Zoo",
                     "New York Aquarium", "Wave Hill"):
        assert expected in names
    norwood_endpoint = _Point(-73.874487, 40.85237)
    assert any(poly.contains(norwood_endpoint) for _n, poly, _pm in zones)
    for _name, poly, poly_m in zones:
        assert poly.is_valid and poly_m.is_valid


def test_interior_sidewalks_for_tile_clips_fee_gated_zones(monkeypatch):
    monkeypatch.setattr(streets, "_FEE_GATED_ZONES",
                        [_test_fee_zone(-73.99, 40.7, 100.0)])
    monkeypatch.setattr(streets, "_CLOSURE_ZONES_M", [])
    geojson = {"features": [
        {"geometry": {"type": "LineString",
                      "coordinates": [[-73.9901, 40.7], [-73.9899, 40.7]]}},  # inside
        {"geometry": {"type": "LineString",
                      "coordinates": [[-73.95, 40.72], [-73.949, 40.72]]}},   # far
    ]}
    tile = Bbox(lat_min=40.6, lat_max=40.8, lon_min=-74.05, lon_max=-73.9)
    segments = streets._interior_sidewalks_for_tile(geojson, tile, None, "test")
    assert segments == [[(-73.95, 40.72), (-73.949, 40.72)]]


# ---- restricted operational grounds (FIXES.md item 1f, v27) ---------------
# Gated service areas -- JFK's fence line is the first entry. Same
# clip/drop machinery as fee zones but with NO exemption sets: the
# interior roads are untagged tertiary/unclassified and access=private
# parking aisles, exactly what the fee-zone exemptions would keep.


def test_drop_zone_interior_edges_without_exemptions_removes_roads_too():
    # Restricted semantics: inside the fence nothing is public. An
    # untagged tertiary -- which FEE_GATED_EXEMPT_HIGHWAYS keeps for fee
    # zones -- must go when the exemption sets are empty.
    zone = _test_fee_zone(-73.99, 40.7, 200.0, name="restricted test")
    g = _fee_zone_graph()
    g.add_edge(1, 2, highway="tertiary", length=17.0)
    g = streets._drop_zone_interior_edges(
        g, "test", [zone], frozenset(), frozenset(),
        "restricted-grounds exclusion")
    assert not g.has_edge(1, 2)


def test_real_restricted_zones_file_covers_the_airports():
    # JFK: interior probes are the 2026-08-21 dry-run's verified sites
    # (the cargo-complex hub every shortcut cluster shared, and the
    # Federal Circle rental area); exterior probes are the Brookville
    # and Howard Beach census fringe clusters -- ordinary city streets
    # that must stay routable. LGA (same day, user-requested sweep):
    # Terminal B and the Marine Air Terminal inside; the Flushing Bay
    # Promenade and the Ditmars/Planeview corner outside -- the public
    # walks along the fence must survive.
    from shapely.geometry import Point as _Point

    zones = {name: (poly, poly_m)
             for name, poly, poly_m in streets._RESTRICTED_ZONES}
    assert len(zones) == 2
    for poly, poly_m in zones.values():
        assert poly.is_valid and poly_m.is_valid

    jfk, _ = zones["John F. Kennedy International Airport"]
    assert jfk.contains(_Point(-73.795408, 40.652809))
    assert jfk.contains(_Point(-73.8025, 40.6608))
    assert not jfk.contains(_Point(-73.7445, 40.6765))
    assert not jfk.contains(_Point(-73.828, 40.637))

    lga, _ = zones["LaGuardia Airport"]
    assert lga.contains(_Point(-73.87453, 40.77281))
    assert lga.contains(_Point(-73.8857, 40.77307))
    assert not lga.contains(_Point(-73.85661, 40.76473))
    assert not lga.contains(_Point(-73.8874, 40.7657))


def test_clip_zone_polygons_default_includes_restricted_zones():
    # The DEFAULT zone list (no zones argument) must cover both files:
    # a segment inside JFK's cargo complex dies without any monkeypatch.
    inside_jfk = LineString([(-73.7955, 40.6527), (-73.7953, 40.6529)])
    assert streets._clip_zone_polygons(inside_jfk) == []


def test_interior_sidewalks_for_tile_clips_restricted_zones(monkeypatch):
    monkeypatch.setattr(streets, "_FEE_GATED_ZONES", [])
    monkeypatch.setattr(streets, "_RESTRICTED_ZONES",
                        [_test_fee_zone(-73.99, 40.7, 100.0)])
    monkeypatch.setattr(streets, "_CLOSURE_ZONES_M", [])
    geojson = {"features": [
        {"geometry": {"type": "LineString",
                      "coordinates": [[-73.9901, 40.7], [-73.9899, 40.7]]}},  # inside
        {"geometry": {"type": "LineString",
                      "coordinates": [[-73.95, 40.72], [-73.949, 40.72]]}},   # far
    ]}
    tile = Bbox(lat_min=40.6, lat_max=40.8, lon_min=-74.05, lon_max=-73.9)
    segments = streets._interior_sidewalks_for_tile(geojson, tile, None, "test")
    assert segments == [[(-73.95, 40.72), (-73.949, 40.72)]]


# ---------------------------------------------------------------------------
# T1 sidewalk connectors (v26): curated chains from
# pipeline/sidewalk_connectors.json are sliced out of the any_sidewalks
# layer by _connector_sidewalks() and composed before the single
# simplify pass. Rule + derivation: pipeline/derive_sidewalk_connectors.py
# and data/audits/2026-08-20/sidewalk_admit_survey_report.md.


def _connector_layer_graph():
    """A fake any_sidewalks tile slice: chain 101-102-103 plus an
    unrelated sidewalk edge 201-202."""
    g = nx.MultiDiGraph()
    g.graph["crs"] = "epsg:4326"
    for n, lon in ((101, -73.990), (102, -73.9899), (103, -73.9898),
                   (201, -73.95), (202, -73.9499)):
        g.add_node(n, x=lon, y=40.7)
    g.add_edge(101, 102, length=9.0)
    g.add_edge(102, 103, length=9.0)
    g.add_edge(201, 202, length=9.0)
    return g


def _pairs_for_chain(*chain):
    return {(a, b) if a <= b else (b, a)
            for a, b in zip(chain, chain[1:])}


def test_connector_sidewalks_keeps_only_listed_chain_edges():
    pairs = _pairs_for_chain("101", "102", "103")
    kept = streets._connector_sidewalks(_connector_layer_graph(), "test",
                                        pairs=pairs)
    assert kept is not None
    assert kept.has_edge(101, 102) and kept.has_edge(102, 103)
    assert not kept.has_edge(201, 202)
    assert 201 not in kept.nodes


def test_connector_sidewalks_partial_chain_survives_a_tile_boundary():
    # A tile slice holding only the middle of a chain contributes what it
    # has; the halves rejoin at shared OSM node ids when exports merge.
    pairs = _pairs_for_chain("100", "101", "102", "103", "104")
    g = _connector_layer_graph()  # only has 101-102-103 of the chain
    kept = streets._connector_sidewalks(g, "test", pairs=pairs)
    assert kept is not None
    assert kept.number_of_edges() == 2


def test_connector_sidewalks_none_for_empty_inputs():
    assert streets._connector_sidewalks(None, "test", pairs={("1", "2")}) is None
    assert streets._connector_sidewalks(_connector_layer_graph(), "test",
                                        pairs=set()) is None
    assert streets._connector_sidewalks(
        _connector_layer_graph(), "test",
        pairs=_pairs_for_chain("7", "8", "9")) is None


def test_load_sidewalk_connectors_normalizes_pair_order(tmp_path, monkeypatch):
    path = tmp_path / "sidewalk_connectors.json"
    path.write_text(json.dumps({
        "rule": {"chain_max_m": 100.0, "saving_min_m": 150.0},
        "connectors": [
            {"att_a": "9", "att_b": "5",
             "chain_nodes": ["9", "7", "5"], "chain_m": 20.0,
             "saving_m": 300, "lat": 40.7, "lon": -73.99},
        ],
    }))
    monkeypatch.setattr(streets, "SIDEWALK_CONNECTORS_PATH", path)
    pairs = streets._load_sidewalk_connectors()
    assert pairs == {("7", "9"), ("5", "7")}


def test_real_sidewalk_connectors_file_is_present_and_sane():
    # The derived file must ship with the v26 pipeline: the loader only
    # WARNS when it's missing (so bare checkouts still build), which
    # makes this guard the actual gate against silently building a
    # connector-less citywide graph. Bounds are deliberately loose --
    # the exact count moves with each re-derivation (REFETCH ritual).
    assert streets.SIDEWALK_CONNECTORS_PATH.exists(), (
        "pipeline/sidewalk_connectors.json missing -- run "
        "pipeline/derive_sidewalk_connectors.py --write")
    data = json.loads(streets.SIDEWALK_CONNECTORS_PATH.read_text())
    assert data["rule"] == {"chain_max_m": 100.0, "saving_min_m": 150.0}
    assert 1000 <= len(data["connectors"]) <= 3000
    for entry in data["connectors"][:50]:
        assert len(entry["chain_nodes"]) >= 2
        assert entry["chain_m"] <= 100.0 + 1e-6
        assert entry["saving_m"] >= 150
    assert len(streets._SIDEWALK_CONNECTOR_PAIRS) >= 1000
