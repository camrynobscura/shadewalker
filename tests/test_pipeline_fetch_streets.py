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

import re

import networkx as nx
import pytest
import requests
from osmnx._errors import InsufficientResponseError

from pipeline import config
from pipeline.config import Bbox
from pipeline.fetch import streets

BBOX = Bbox(lat_min=40.0, lat_max=40.1, lon_min=-74.0, lon_max=-73.9)


def _mock_fetch(monkeypatch, tmp_path, graph_from_bbox):
    monkeypatch.setattr(streets, "STREETS_DIR", tmp_path)
    monkeypatch.setattr(streets.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(streets.ox, "graph_from_bbox", graph_from_bbox)
    monkeypatch.setattr(streets.ox, "save_graphml", lambda graph, path: None)


def _by_filter(
    main_fn,
    cycleway_fn=None,
    access_override_fn=None,
    named_sidewalk_fn=None,
    any_sidewalk_fn=None,
    parking_aisle_fn=None,
):
    """Dispatch a graph_from_bbox mock by which of the six real queries
    fetch_streets makes -- WALK_FILTER (main), CYCLEWAY_FILTER (the
    shared-path union), FOOT_OVERRIDES_ACCESS_FILTER (the
    foot-designated-despite-access=no/private union), NAMED_SIDEWALK_FILTER
    (the named-park-path union), ANY_SIDEWALK_FILTER (every sidewalk,
    narrowed to park reach afterwards), or PARKING_AISLE_FILTER (every
    parking aisle, narrowed to through-paths afterwards). Defaults the five
    narrower queries to "nothing here" (ValueError, the common real case)
    unless a test supplies its own function for one."""
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
    cycleway_graph.add_node("c1", x=1.0, y=1.0)
    override_graph = nx.MultiDiGraph()
    override_graph.add_node("a1", x=2.0, y=2.0)
    named_sidewalk_graph = nx.MultiDiGraph()
    named_sidewalk_graph.add_node("n1", x=3.0, y=3.0)
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
    assert '["access"!~"private|no"]' in streets.WALK_FILTER


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
    assert '"foot"!~"no"' in streets.PARKING_AISLE_FILTER


def _cache_roundtrip(monkeypatch, tmp_path, cached_graph, recorded_bbox_signature):
    """Put a graph in the cache with a given recorded fetch_bbox, then call
    fetch_streets and report whether it re-fetched. Uses osmnx's real
    save/load so the attribute genuinely survives a GraphML round trip
    rather than being asserted against a mock's in-memory dict."""
    monkeypatch.setattr(streets, "STREETS_DIR", tmp_path)
    monkeypatch.setattr(streets.time, "sleep", lambda seconds: None)

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
        fresh = nx.MultiDiGraph()
        fresh.add_node(2222, x=0.0, y=0.0)  # int ids: osmnx casts them on load
        return fresh

    monkeypatch.setattr(streets.ox, "graph_from_bbox", _by_filter(record_fetch))
    result = streets.fetch_streets(BBOX, "cachetile")
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
    # Without this the guard above can never fire on a freshly written file.
    saved = {}

    def capture(graph, path):
        saved["fetch_bbox"] = graph.graph.get("fetch_bbox")

    main_graph = nx.MultiDiGraph()
    main_graph.add_node("m1", x=0.0, y=0.0)
    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph))
    monkeypatch.setattr(streets.ox, "save_graphml", capture)

    streets.fetch_streets(BBOX, "test-records-bbox-tile")

    assert saved["fetch_bbox"] == streets._bbox_signature(BBOX)


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

    saved = []
    _mock_fetch(monkeypatch, tmp_path, _by_filter(lambda **kwargs: main_graph))
    monkeypatch.setattr(streets.ox, "save_graphml", lambda graph, path: saved.append(path))

    streets.fetch_streets(BBOX, "test-cache-variant-tile", park_reach=_park_reach_around(-73.95, 40.05))
    streets.fetch_streets(BBOX, "test-cache-variant-tile")

    assert len(saved) == 2
    assert saved[0] != saved[1]


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
